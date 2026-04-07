"""
DijkFood — automated AWS deploy and teardown (multi-service ECS + ALB).

Prerequisites:
  - AWS credentials in ~/.aws/credentials (default boto3 chain)
  - Docker (or Podman) on PATH for ECR build/push; optional DOCKER_CMD in .env
  - pip install -r requirements-deploy.txt

Optional env:
  Copy env.example → .env and set ACCOUNT_ID / role ARNs for your learner lab.
  DIJKFOOD_DB_PASSWORD — RDS master password (generated if unset)
  EXECUTION_ROLE_ARN, TASK_ROLE_ARN — lab ECS roles (skip iam:CreateRole; no Dynamo inline
    attach on external task role; teardown never deletes IAM roles)
  Project-root .env is loaded at startup (before AWS_REGION is read).

Usage:
  python deploy.py
  python deploy.py --skip-teardown    # writes connection.env after success
  python deploy.py --teardown-only    # destroy from connection.env
  python deploy.py --service ordering   # rebuild/push one service from connection.env

  Single-service redeploy uses connection.env (run a full deploy with --skip-teardown first).
  For ordering, set DIJKFOOD_DB_PASSWORD in .env; DB_HOST is stored in connection.env.

Load testing is separate — after deploy, run e.g.:
  python -m simulator.load_sim --base-url "http://<alb-dns>"
  python -m simulator.load_test --base-url "http://<alb-dns>" ...
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

import boto3
from botocore.exceptions import ClientError

from tools.connection_env import (
    CONNECTION_ENV_PATH,
    load_connection_env,
    write_connection_env,
)
from tools.dynamodb_infra import (
    attach_dynamo_policy_to_task_role,
    create_dynamodb_tables,
    destroy_dynamodb_tables,
)
from tools.ecs_infra import (
    build_and_push_image,
    create_alb_only,
    create_alb_security_group,
    create_ecs_service,
    create_ecs_task_security_group,
    create_listener_and_rules,
    create_target_group,
    destroy_ecs_stack,
    ensure_ecr_repository,
    ensure_execution_role,
    ensure_log_group,
    ensure_task_role,
    ecs_service_exists,
    register_fargate_task_definition,
    resolve_container_cli,
    update_ecs_service_task_definition,
    wait_for_service_stable,
)
from tools.s3_routing_graph_infra import (
    attach_routing_graph_s3_policy,
    create_routing_graph_bucket,
    destroy_routing_graph_bucket,
)
from tools.rds_infra import (
    allow_rds_from_ecs_tasks,
    create_db_subnet_group,
    create_rds_instance,
    create_rds_security_group,
    destroy_rds,
    get_default_subnet_ids,
    get_default_vpc_id,
    run_schema_bootstrap,
)
from tools.state import DeploymentState

# =============================================================================
# CONFIG
# =============================================================================

REGION = os.environ.get("AWS_REGION", "us-east-1")
DB_NAME = "dijkfood"
DB_MASTER_USER = "dijkfood_admin"


def _db_password() -> str:
    p = os.environ.get("DIJKFOOD_DB_PASSWORD")
    if p:
        return p
    return secrets.token_urlsafe(24)


def _snapshot_connection_env(
    state: DeploymentState, base_url: str = "", *, quiet: bool = True
) -> None:
    """Persist connection.env so teardown-only works if the deploy process stops mid-way."""
    write_connection_env(state, base_url, REGION, quiet=quiet)


SERVICE_IDS = ("ordering", "tracking", "routing")


def _service_specs(project_root: Path) -> list[dict[str, object]]:
    return [
        {
            "id": "ordering",
            "ecr_suffix": "ordering",
            "docker_context": project_root,
            "dockerfile": project_root / "services" / "ordering" / "Dockerfile",
            "path_pattern": "default",
            "cpu": "256",
            "memory": "512",
            "desired_count": 1,
            "full_stack_env": True,
        },
        {
            "id": "tracking",
            "ecr_suffix": "tracking",
            "docker_context": project_root / "services" / "tracking",
            "dockerfile": project_root / "services" / "tracking" / "Dockerfile",
            "path_pattern": "/tracking*",
            "cpu": "256",
            "memory": "512",
            "desired_count": 1,
            "full_stack_env": False,
        },
        {
            "id": "routing",
            "ecr_suffix": "routing",
            "docker_context": project_root / "services" / "routing",
            "dockerfile": project_root / "services" / "routing" / "Dockerfile",
            "path_pattern": "/routing*",
            "cpu": "512",
            "memory": "2048",
            "desired_count": 1,
            "full_stack_env": False,
            "health_check_grace_seconds": 600,
        },
    ]


def _spec_for_id(specs: list[dict[str, object]], service_id: str) -> dict[str, object]:
    for s in specs:
        if str(s["id"]) == service_id:
            return s
    raise KeyError(service_id)


def redeploy_single_service(project_root: Path, service_id: str) -> None:
    """Build/push one image and roll ECS to a new task definition using connection.env."""
    try:
        state, region, base_url = load_connection_env()
    except (FileNotFoundError, ValueError) as e:
        print(f"[deploy] ERROR: {e}")
        raise SystemExit(1) from e

    if not state.cluster_name:
        print("[deploy] ERROR: connection.env missing ECS_CLUSTER_NAME")
        raise SystemExit(1)
    rec = next((r for r in state.ecs_services if r.service_id == service_id), None)
    if not rec:
        print(
            f"[deploy] ERROR: no ECS service record for {service_id!r} in ECS_SERVICES_JSON"
        )
        raise SystemExit(1)
    exec_arn = state.execution_role_arn or (os.environ.get("EXECUTION_ROLE_ARN") or "").strip()
    task_arn = state.task_role_arn or (os.environ.get("TASK_ROLE_ARN") or "").strip()
    if not exec_arn or not task_arn:
        print(
            "[deploy] ERROR: missing execution/task role ARNs in connection.env "
            "(EXECUTION_ROLE_ARN / TASK_ROLE_ARN)"
        )
        raise SystemExit(1)

    try:
        resolve_container_cli()
    except FileNotFoundError as e:
        print(f"[deploy] ERROR: {e}")
        raise SystemExit(1) from e

    specs = _service_specs(project_root)
    try:
        spec = _spec_for_id(specs, service_id)
    except KeyError:
        print(f"[deploy] ERROR: unknown service {service_id!r}")
        raise SystemExit(1) from None

    session = boto3.Session(region_name=region)
    sts = session.client("sts")
    ecs = session.client("ecs")
    ecr = session.client("ecr")
    logs = session.client("logs")
    account_id = sts.get_caller_identity()["Account"]

    alb_base = (base_url or "").rstrip("/")
    password = (os.environ.get("DIJKFOOD_DB_PASSWORD") or "").strip()

    def _ordering_environment_redeploy() -> list[dict[str, str]]:
        db_host = (state.rds_endpoint or "").strip()
        if not db_host and state.rds_instance_id:
            rds_client = session.client("rds")
            inst = rds_client.describe_db_instances(
                DBInstanceIdentifier=state.rds_instance_id
            )["DBInstances"][0]
            db_host = (inst.get("Endpoint") or {}).get("Address") or ""
            if db_host:
                state.rds_endpoint = db_host
        if not db_host:
            print(
                "[deploy] ERROR: DB_HOST missing in connection.env and could not resolve "
                "from RDS_INSTANCE_ID; re-run a full deploy with --skip-teardown or set DB_HOST."
            )
            raise SystemExit(1)
        if not password:
            print(
                "[deploy] ERROR: ordering redeploy requires DIJKFOOD_DB_PASSWORD in .env"
            )
            raise SystemExit(1)
        return [
            {"name": "DB_HOST", "value": db_host},
            {"name": "DB_PORT", "value": "5432"},
            {"name": "DB_NAME", "value": DB_NAME},
            {"name": "DB_USER", "value": DB_MASTER_USER},
            {"name": "DB_PASSWORD", "value": password},
            {"name": "AWS_REGION", "value": region},
            {"name": "AWS_DEFAULT_REGION", "value": region},
            {"name": "ROUTING_BASE_URL", "value": alb_base},
            {
                "name": "DYNAMODB_ORDER_LOGS_TABLE",
                "value": state.dynamo_order_logs_table or "",
            },
            {
                "name": "DYNAMODB_COURIER_POSITIONS_TABLE",
                "value": state.dynamo_courier_positions_table or "",
            },
        ]

    def _routing_environment_redeploy() -> list[dict[str, str]]:
        env = [
            {"name": "AWS_REGION", "value": region},
            {"name": "AWS_DEFAULT_REGION", "value": region},
            {"name": "SERVICE_ID", "value": "routing"},
            {"name": "OSMNX_PLACE", "value": "São Paulo, SP, Brazil"},
            {"name": "ROUTING_NETWORK_TYPE", "value": "drive"},
        ]
        if state.routing_graph_s3_bucket:
            env.append(
                {
                    "name": "ROUTING_GRAPH_S3_BUCKET",
                    "value": state.routing_graph_s3_bucket,
                }
            )
        return env

    def _stub_environment_redeploy(label: str) -> list[dict[str, str]]:
        return [
            {"name": "AWS_REGION", "value": region},
            {"name": "AWS_DEFAULT_REGION", "value": region},
            {"name": "SERVICE_ID", "value": label},
        ]

    if spec["full_stack_env"]:
        env = _ordering_environment_redeploy()
    elif service_id == "routing":
        env = _routing_environment_redeploy()
    else:
        env = _stub_environment_redeploy(service_id)

    print(f"[deploy] --- Single-service redeploy: {service_id} (cluster={state.cluster_name}) ---")
    ensure_ecr_repository(ecr, rec.ecr_repo_name)
    image_uri = build_and_push_image(
        region=region,
        account_id=account_id,
        repo_name=rec.ecr_repo_name,
        docker_context=spec["docker_context"],  # type: ignore[arg-type]
        dockerfile=spec["dockerfile"],  # type: ignore[arg-type]
    )
    ensure_log_group(logs, rec.log_group_name)
    grace = int(spec.get("health_check_grace_seconds", 120))
    td_arn = register_fargate_task_definition(
        ecs,
        task_family=rec.task_definition_family,
        image_uri=image_uri,
        execution_role_arn=exec_arn,
        task_role_arn=task_arn,
        log_group=rec.log_group_name,
        region=region,
        environment=env,
        cpu=str(spec["cpu"]),
        memory=str(spec["memory"]),
    )
    update_ecs_service_task_definition(
        ecs,
        cluster=state.cluster_name,
        service=rec.service_name,
        task_definition_arn=td_arn,
        health_check_grace_period_seconds=grace,
    )
    wait_for_service_stable(ecs, state.cluster_name, rec.service_name)
    write_connection_env(state, base_url, region, quiet=False)
    print(f"[deploy] {service_id} redeploy complete. BASE_URL={base_url or '(unset)'}")


def run_teardown(
    *,
    ec2,
    rds,
    elbv2,
    ecs,
    ecr,
    logs,
    ddb,
    s3,
    state: DeploymentState,
) -> None:
    print("[deploy] --- Teardown ---")
    try:
        destroy_ecs_stack(
            ecs, elbv2, ecr, logs, ec2, state, rds_sg_id=state.rds_sg_id
        )
    except ClientError as e:
        print(f"[deploy] Teardown ECS stack: {e}")
    try:
        destroy_routing_graph_bucket(s3, state)
    except ClientError as e:
        print(f"[deploy] Teardown routing graph S3: {e}")
    try:
        destroy_dynamodb_tables(ddb, state)
    except ClientError as e:
        print(f"[deploy] Teardown DynamoDB: {e}")
    try:
        destroy_rds(rds, ec2, state)
    except ClientError as e:
        print(f"[deploy] Teardown RDS: {e}")
    print("[deploy] Teardown done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="DijkFood deploy / teardown")
    parser.add_argument(
        "--teardown-only",
        action="store_true",
        help="Destroy AWS resources described in connection.env",
    )
    parser.add_argument(
        "--skip-teardown",
        action="store_true",
        help="After a successful full deploy, keep resources and write connection.env",
    )
    parser.add_argument(
        "--keep-connection-env",
        action="store_true",
        help="With --teardown-only, do not delete connection.env after teardown",
    )
    parser.add_argument(
        "--service",
        metavar="ID",
        choices=list(SERVICE_IDS),
        help="Redeploy one microservice (ordering|tracking|routing) using connection.env",
    )
    args = parser.parse_args()

    project_root = _PROJECT_ROOT

    if args.teardown_only and args.service:
        parser.error("cannot combine --teardown-only and --service")

    if args.service:
        redeploy_single_service(project_root, args.service)
        return

    if args.teardown_only:
        try:
            state, region, _base = load_connection_env()
        except (FileNotFoundError, ValueError) as e:
            print(f"[deploy] ERROR: {e}")
            sys.exit(1)
        session = boto3.Session(region_name=region)
        run_teardown(
            ec2=session.client("ec2"),
            rds=session.client("rds"),
            elbv2=session.client("elbv2"),
            ecs=session.client("ecs"),
            ecr=session.client("ecr"),
            logs=session.client("logs"),
            ddb=session.client("dynamodb"),
            s3=session.client("s3"),
            state=state,
        )
        if not args.keep_connection_env and CONNECTION_ENV_PATH.is_file():
            CONNECTION_ENV_PATH.unlink()
            print(f"[deploy] Removed {CONNECTION_ENV_PATH}")
        sys.exit(0)

    suffix = secrets.token_hex(3)
    password = _db_password()
    state = DeploymentState(suffix=suffix)

    try:
        resolve_container_cli()
    except FileNotFoundError as e:
        print(f"[deploy] ERROR: {e}")
        sys.exit(1)

    session = boto3.Session(region_name=REGION)
    ec2 = session.client("ec2")
    rds = session.client("rds")
    sts = session.client("sts")
    elbv2 = session.client("elbv2")
    ecs = session.client("ecs")
    ecr = session.client("ecr")
    iam = session.client("iam")
    logs = session.client("logs")
    ddb = session.client("dynamodb")
    s3 = session.client("s3")

    account_id = sts.get_caller_identity()["Account"]
    db_instance_id = f"dijkfood-db-{suffix}"

    exit_code = 1
    alb_dns: str | None = None

    try:
        print(f"[deploy] DeploymentId={suffix} region={REGION}")
        vpc_id = get_default_vpc_id(ec2)
        subnet_ids = get_default_subnet_ids(ec2, vpc_id)

        print("[deploy] --- RDS ---")
        rds_sg = create_rds_security_group(ec2, vpc_id, suffix, state)
        create_db_subnet_group(rds, subnet_ids, suffix, state)
        _snapshot_connection_env(state)

        endpoint = create_rds_instance(
            rds,
            instance_id=db_instance_id,
            subnet_group=state.db_subnet_group or "",
            sg_id=rds_sg,
            db_name=DB_NAME,
            master_user=DB_MASTER_USER,
            master_password=password,
            suffix=suffix,
            state=state,
        )
        state.rds_endpoint = endpoint
        run_schema_bootstrap(
            endpoint,
            5432,
            DB_NAME,
            DB_MASTER_USER,
            password,
        )
        _snapshot_connection_env(state)

        print("[deploy] --- DynamoDB ---")
        create_dynamodb_tables(ddb, suffix, state)
        _snapshot_connection_env(state)

        print("[deploy] --- S3 (routing graph cache) ---")
        create_routing_graph_bucket(s3, suffix, REGION, state)
        _snapshot_connection_env(state)

        print("[deploy] --- Networking / ALB / ECS ---")
        alb_sg = create_alb_security_group(ec2, vpc_id, suffix, state)
        task_sg = create_ecs_task_security_group(ec2, vpc_id, alb_sg, suffix, state)
        allow_rds_from_ecs_tasks(ec2, rds_sg, task_sg)

        exec_arn = ensure_execution_role(iam, suffix, state)
        task_arn = ensure_task_role(iam, suffix, state)
        task_role_name = f"dijkfood-ecs-task-{suffix}"
        using_external_task_role = bool((os.environ.get("TASK_ROLE_ARN") or "").strip())
        if (
            not using_external_task_role
            and state.dynamo_order_logs_arn
            and state.dynamo_courier_positions_arn
        ):
            attach_dynamo_policy_to_task_role(
                iam,
                task_role_name,
                state.dynamo_order_logs_arn,
                state.dynamo_courier_positions_arn,
            )
        if (
            not using_external_task_role
            and state.routing_graph_s3_bucket
        ):
            attach_routing_graph_s3_policy(
                iam,
                task_role_name,
                state.routing_graph_s3_bucket,
            )
        if using_external_task_role and state.routing_graph_s3_bucket:
            b = state.routing_graph_s3_bucket
            print(
                "[deploy] TASK_ROLE_ARN set: add s3:GetObject, s3:PutObject, s3:HeadObject "
                f"on arn:aws:s3:::{b}/* to your lab task role for routing graph cache."
            )
        time.sleep(10)

        specs = _service_specs(project_root)
        tg_by_id: dict[str, str] = {}
        for spec in specs:
            sid = str(spec["id"])
            tg_by_id[sid] = create_target_group(elbv2, vpc_id, suffix, sid)
        _snapshot_connection_env(state)

        alb_arn, alb_dns = create_alb_only(
            elbv2, subnet_ids, alb_sg, suffix, state
        )
        path_rules: list[tuple[int, str, str]] = []
        priority = 10
        for spec in specs:
            pat = str(spec["path_pattern"])
            if pat != "default":
                sid = str(spec["id"])
                path_rules.append((priority, pat, tg_by_id[sid]))
                priority += 10
        create_listener_and_rules(
            elbv2,
            alb_arn=alb_arn,
            default_target_group_arn=tg_by_id["ordering"],
            path_forward_rules=path_rules,
            state=state,
        )
        _snapshot_connection_env(state, f"http://{alb_dns}")

        cluster = f"dijkfood-{suffix}"
        alb_base_url = f"http://{alb_dns}"

        def _ordering_environment() -> list[dict[str, str]]:
            return [
                {"name": "DB_HOST", "value": endpoint},
                {"name": "DB_PORT", "value": "5432"},
                {"name": "DB_NAME", "value": DB_NAME},
                {"name": "DB_USER", "value": DB_MASTER_USER},
                {"name": "DB_PASSWORD", "value": password},
                {"name": "AWS_REGION", "value": REGION},
                {"name": "AWS_DEFAULT_REGION", "value": REGION},
                {"name": "ROUTING_BASE_URL", "value": alb_base_url},
                {
                    "name": "DYNAMODB_ORDER_LOGS_TABLE",
                    "value": state.dynamo_order_logs_table or "",
                },
                {
                    "name": "DYNAMODB_COURIER_POSITIONS_TABLE",
                    "value": state.dynamo_courier_positions_table or "",
                },
            ]

        def _routing_environment() -> list[dict[str, str]]:
            env = [
                {"name": "AWS_REGION", "value": REGION},
                {"name": "AWS_DEFAULT_REGION", "value": REGION},
                {"name": "SERVICE_ID", "value": "routing"},
                {"name": "OSMNX_PLACE", "value": "São Paulo, SP, Brazil"},
                {"name": "ROUTING_NETWORK_TYPE", "value": "drive"},
            ]
            if state.routing_graph_s3_bucket:
                env.append(
                    {
                        "name": "ROUTING_GRAPH_S3_BUCKET",
                        "value": state.routing_graph_s3_bucket,
                    }
                )
            return env

        def _stub_environment(label: str) -> list[dict[str, str]]:
            return [
                {"name": "AWS_REGION", "value": REGION},
                {"name": "AWS_DEFAULT_REGION", "value": REGION},
                {"name": "SERVICE_ID", "value": label},
            ]

        for i, spec in enumerate(specs):
            svc_id = str(spec["id"])
            repo_name = f"dijkfood-{spec['ecr_suffix']}-{suffix}"
            ensure_ecr_repository(ecr, repo_name)
            image_uri = build_and_push_image(
                region=REGION,
                account_id=account_id,
                repo_name=repo_name,
                docker_context=spec["docker_context"],  # type: ignore[arg-type]
                dockerfile=spec["dockerfile"],  # type: ignore[arg-type]
            )
            log_grp = f"/ecs/dijkfood-{svc_id}-{suffix}"
            ensure_log_group(logs, log_grp)
            if spec["full_stack_env"]:
                env = _ordering_environment()
            elif svc_id == "routing":
                env = _routing_environment()
            else:
                env = _stub_environment(svc_id)
            svc_name = f"dijkfood-{svc_id}-{suffix}"
            family = f"dijkfood-{svc_id}-{suffix}"
            create_ecs_service(
                ecs,
                cluster_name=cluster,
                service_name=svc_name,
                service_id=svc_id,
                task_family=family,
                image_uri=image_uri,
                execution_role_arn=exec_arn,
                task_role_arn=task_arn,
                log_group=log_grp,
                region=REGION,
                subnet_ids=subnet_ids,
                task_sg_id=task_sg,
                target_group_arn=tg_by_id[svc_id],
                suffix=suffix,
                state=state,
                environment=env,
                cpu=str(spec["cpu"]),
                memory=str(spec["memory"]),
                desired_count=int(spec["desired_count"]),
                create_cluster_if_needed=(i == 0),
                health_check_grace_period_seconds=int(
                    spec.get("health_check_grace_seconds", 120)
                ),
            )
            wait_for_service_stable(ecs, cluster, svc_name)
            _snapshot_connection_env(state, f"http://{alb_dns}")

        print(
            f"[deploy] All ECS services stable. ALB: http://{alb_dns}\n"
            "[deploy] Run load tests manually, e.g. "
            f"python -m simulator.load_sim --base-url http://{alb_dns}"
        )
        exit_code = 0

    except Exception as e:
        print(f"[deploy] ERROR: {e}")
        if exit_code == 0:
            exit_code = 1
    finally:
        if args.skip_teardown:
            print("[deploy] --skip-teardown: leaving AWS resources running.")
            if alb_dns:
                print(f"[deploy] ALB DNS: {alb_dns}")
            # Always refresh snapshot (deploy may have failed after partial provision).
            write_connection_env(
                state,
                f"http://{alb_dns}" if alb_dns else "",
                REGION,
                quiet=False,
            )
            sys.exit(exit_code)
        if exit_code != 0:
            write_connection_env(
                state,
                f"http://{alb_dns}" if alb_dns else "",
                REGION,
                quiet=False,
            )
            print(
                "[deploy] connection.env updated for recovery; "
                "run python deploy.py --teardown-only if cleanup is incomplete."
            )
        run_teardown(
            ec2=ec2,
            rds=rds,
            elbv2=elbv2,
            ecs=ecs,
            ecr=ecr,
            logs=logs,
            ddb=ddb,
            s3=s3,
            state=state,
        )
        print("[deploy] Done.")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
