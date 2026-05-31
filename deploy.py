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
  python deploy.py --service tracking --recreate   # also set desired_count from _service_specs (UpdateService only)
  python deploy.py --resume   # reuse RDS/Dynamo/S3 from connection.env; rebuild ECS (implies --skip-teardown)
  python deploy.py --skip-teardown --with-agent   # lab stack + agent on same ALB (.env.agent Bedrock keys)
  python deploy.py --service agent   # redeploy agent only (requires prior --with-agent deploy)

  Single-service redeploy uses connection.env (run a full deploy with --skip-teardown first).
  Ordering/tracking redeploy: DIJKFOOD_DB_PASSWORD is optional if the service already runs with
  DB_PASSWORD in ECS — the script reuses that value for the new task definition (no RDS changes).
  --resume: same ECS fallback after load_connection_env; DB_HOST is in connection.env.

Load testing is separate — after deploy, run e.g.:
  python -m simulator.orchestration.load_sim --base-url "http://<alb-dns>"
  python -m simulator.orchestration.load_test --base-url "http://<alb-dns>" ...
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
    configure_ecs_service_autoscaling,
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
    upload_routing_graph_seed_if_present,
)
from tools.agent_aws import load_agent_dotenv, restore_lab_credentials_for_deploy
from tools.agent_deploy import (
    AGENT_SERVICE_ID,
    agent_service_spec,
    build_agent_task_environment,
    require_bedrock_credentials,
)
from tools.agent_infra import (
    attach_agent_sessions_policy,
    create_agent_sessions_table,
    destroy_agent_sessions_table,
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


def _db_password_from_ecs_service(
    ecs, *, cluster: str, service_name: str
) -> str:
    """
    Read DB_PASSWORD from the service's current task definition so redeploy/register
    can rotate the image without DIJKFOOD_DB_PASSWORD in .env (unchanged RDS password).
    """
    try:
        svcs = ecs.describe_services(cluster=cluster, services=[service_name]).get(
            "services"
        ) or []
        if not svcs or svcs[0].get("status") == "INACTIVE":
            return ""
        td_arn = (svcs[0].get("taskDefinition") or "").strip()
        if not td_arn:
            return ""
        td = ecs.describe_task_definition(taskDefinition=td_arn).get(
            "taskDefinition"
        ) or {}
        for c in td.get("containerDefinitions") or []:
            for pair in c.get("environment") or []:
                if pair.get("name") == "DB_PASSWORD":
                    v = (pair.get("value") or "").strip()
                    if v:
                        return v
    except ClientError:
        return ""
    return ""


def _snapshot_connection_env(
    state: DeploymentState,
    base_url: str = "",
    *,
    region: str | None = None,
    quiet: bool = True,
) -> None:
    """Persist connection.env so teardown-only works if the deploy process stops mid-way."""
    write_connection_env(state, base_url, region or REGION, quiet=quiet)


def _refresh_rds_endpoint(rds, state: DeploymentState) -> str:
    if not state.rds_instance_id:
        raise ValueError("connection.env: RDS_INSTANCE_ID is required to reuse RDS")
    resp = rds.describe_db_instances(DBInstanceIdentifier=state.rds_instance_id)
    inst = resp["DBInstances"][0]
    ep = inst["Endpoint"]["Address"]
    state.rds_endpoint = ep
    return ep


def _hydrate_dynamo_arns(ddb, state: DeploymentState) -> None:
    if state.dynamo_order_logs_table:
        t = ddb.describe_table(TableName=state.dynamo_order_logs_table)["Table"]
        state.dynamo_order_logs_arn = t["TableArn"]
    if state.dynamo_courier_positions_table:
        t = ddb.describe_table(TableName=state.dynamo_courier_positions_table)["Table"]
        state.dynamo_courier_positions_arn = t["TableArn"]
    if state.dynamo_routes_table:
        t = ddb.describe_table(TableName=state.dynamo_routes_table)["Table"]
        state.dynamo_routes_arn = t["TableArn"]
    if state.dynamo_agent_sessions_table:
        t = ddb.describe_table(TableName=state.dynamo_agent_sessions_table)["Table"]
        state.dynamo_agent_sessions_arn = t["TableArn"]


BASE_SERVICE_IDS = ("ordering", "tracking", "routing")
SERVICE_IDS = BASE_SERVICE_IDS + (AGENT_SERVICE_ID,)


def _service_specs(project_root: Path, *, with_agent: bool = False) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = [
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
            "autoscaling": {
                "enabled": True,
                "min_capacity": 1,
                "max_capacity": 12,
                "cpu_target": 50.0,
                "memory_target": 50.0,
                "scale_in_cooldown": 120,
                "scale_out_cooldown": 45,
            },
        },
        {
            "id": "tracking",
            "ecr_suffix": "tracking",
            "docker_context": project_root,
            "dockerfile": project_root / "services" / "tracking" / "Dockerfile",
            "path_pattern": "/tracking*",
            "cpu": "256",
            "memory": "1024",
            "desired_count": 1,
            "full_stack_env": True,
            "autoscaling": {
                "enabled": True,
                "min_capacity": 1,
                "max_capacity": 24,
                "cpu_target": 50.0,
                "memory_target": 50.0,
                "scale_in_cooldown": 120,
                "scale_out_cooldown": 45,
            },
        },
        {
            "id": "routing",
            "ecr_suffix": "routing",
            "docker_context": project_root / "services" / "routing",
            "dockerfile": project_root / "services" / "routing" / "Dockerfile",
            "path_pattern": "/routing*",
            "cpu": "4096",
            "memory": "16384",
            "desired_count": 1,
            "full_stack_env": False,
            "health_check_grace_seconds": 600,
            "autoscaling": {
                "enabled": True,
                "min_capacity": 1,
                "max_capacity": 24,
                "cpu_target": 50.0,
                "memory_target": 50.0,
                "scale_in_cooldown": 180,
                "scale_out_cooldown": 60,
            },
        },
    ]
    if with_agent:
        specs.append(agent_service_spec(project_root))
    return specs


def _agent_deployed(state: DeploymentState) -> bool:
    return any(r.service_id == AGENT_SERVICE_ID for r in state.ecs_services)


def _ensure_agent_dynamodb_and_iam(
    ddb,
    iam,
    suffix: str,
    state: DeploymentState,
    *,
    skip_create: bool,
    using_external_task_role: bool,
) -> None:
    if skip_create and state.dynamo_agent_sessions_table:
        print("[deploy] --- DynamoDB (agent sessions, resume) ---")
        _hydrate_dynamo_arns(ddb, state)
    elif not skip_create:
        print("[deploy] --- DynamoDB (agent sessions) ---")
        create_agent_sessions_table(ddb, suffix, state)
    task_role_name = f"dijkfood-ecs-task-{suffix}"
    if (
        not using_external_task_role
        and state.dynamo_agent_sessions_arn
    ):
        attach_agent_sessions_policy(
            iam, task_role_name, state.dynamo_agent_sessions_arn
        )


def _add_agent_listener_rule(
    elbv2,
    *,
    listener_arn: str,
    target_group_arn: str,
    priority: int = 30,
) -> None:
    elbv2.create_rule(
        ListenerArn=listener_arn,
        Priority=priority,
        Conditions=[{"Field": "path-pattern", "Values": ["/agent*"]}],
        Actions=[{"Type": "forward", "TargetGroupArn": target_group_arn}],
    )
    print(f"  [ALB] Rule /agent* (priority {priority})")


def _provision_agent_on_existing_alb(
    *,
    project_root: Path,
    ecs,
    elbv2,
    ecr,
    logs,
    app_autoscaling,
    deploy_region: str,
    account_id: str,
    suffix: str,
    state: DeploymentState,
    alb_base_url: str,
    subnet_ids: list[str],
    task_sg: str,
    exec_arn: str,
    task_arn: str,
    vpc_id: str,
) -> None:
    """Add agent TG + listener rule + ECS service to an existing lab ALB."""
    if _agent_deployed(state):
        print("[deploy] Agent ECS service already in connection.env; skip incremental provision")
        return
    if not state.listener_arn:
        raise RuntimeError("Incremental agent deploy requires LISTENER_ARN in connection.env")

    spec = agent_service_spec(project_root)
    tg_arn = create_target_group(elbv2, vpc_id, suffix, AGENT_SERVICE_ID)
    _add_agent_listener_rule(
        elbv2,
        listener_arn=state.listener_arn,
        target_group_arn=tg_arn,
    )

    repo_name = f"dijkfood-{spec['ecr_suffix']}-{suffix}"
    ensure_ecr_repository(ecr, repo_name)
    image_uri = build_and_push_image(
        region=deploy_region,
        account_id=account_id,
        repo_name=repo_name,
        docker_context=spec["docker_context"],  # type: ignore[arg-type]
        dockerfile=spec["dockerfile"],  # type: ignore[arg-type]
    )
    log_grp = f"/ecs/dijkfood-{AGENT_SERVICE_ID}-{suffix}"
    ensure_log_group(logs, log_grp)
    env = build_agent_task_environment(
        region=deploy_region,
        alb_base_url=alb_base_url,
        sessions_table=state.dynamo_agent_sessions_table or "",
        project_root=project_root,
    )
    cluster = state.cluster_name or f"dijkfood-{suffix}"
    svc_name = f"dijkfood-{AGENT_SERVICE_ID}-{suffix}"
    create_ecs_service(
        ecs,
        cluster_name=cluster,
        service_name=svc_name,
        service_id=AGENT_SERVICE_ID,
        task_family=f"dijkfood-{AGENT_SERVICE_ID}-{suffix}",
        image_uri=image_uri,
        execution_role_arn=exec_arn,
        task_role_arn=task_arn,
        log_group=log_grp,
        region=deploy_region,
        subnet_ids=subnet_ids,
        task_sg_id=task_sg,
        target_group_arn=tg_arn,
        suffix=suffix,
        state=state,
        environment=env,
        cpu=str(spec["cpu"]),
        memory=str(spec["memory"]),
        desired_count=int(spec["desired_count"]),
        create_cluster_if_needed=False,
    )
    _configure_service_autoscaling_if_enabled(
        app_autoscaling,
        cluster_name=cluster,
        service_name=svc_name,
        spec=spec,
    )
    wait_for_service_stable(ecs, cluster, svc_name)
    print(f"[deploy] Agent ready at {alb_base_url}/agent/v1/chat")


def _configure_service_autoscaling_if_enabled(
    app_autoscaling,
    *,
    cluster_name: str,
    service_name: str,
    spec: dict[str, object],
) -> None:
    autoscaling = spec.get("autoscaling")
    if not isinstance(autoscaling, dict):
        return
    if not bool(autoscaling.get("enabled", False)):
        return
    configure_ecs_service_autoscaling(
        app_autoscaling,
        cluster_name=cluster_name,
        service_name=service_name,
        min_capacity=int(autoscaling["min_capacity"]),
        max_capacity=int(autoscaling["max_capacity"]),
        cpu_target=float(autoscaling["cpu_target"]),
        memory_target=float(autoscaling["memory_target"]),
        scale_in_cooldown=int(autoscaling.get("scale_in_cooldown", 120)),
        scale_out_cooldown=int(autoscaling.get("scale_out_cooldown", 45)),
    )
    print(
        "  [ASG] Autoscaling configured for "
        f"{service_name} "
        f"(min={int(autoscaling['min_capacity'])}, "
        f"max={int(autoscaling['max_capacity'])}, "
        f"cpu_target={float(autoscaling['cpu_target'])}, "
        f"memory_target={float(autoscaling['memory_target'])})"
    )


def _spec_for_id(specs: list[dict[str, object]], service_id: str) -> dict[str, object]:
    for s in specs:
        if str(s["id"]) == service_id:
            return s
    raise KeyError(service_id)


def redeploy_single_service(
    project_root: Path, service_id: str, *, apply_service_spec: bool = False
) -> None:
    """
    Build/push one image and roll ECS to a new task definition using connection.env.
    CPU/memory always come from _service_specs in the new task definition.
    If apply_service_spec True (--recreate), desired_count is set in the same UpdateService
    call (no ECS service deletion or replace).
    """
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

    if service_id == AGENT_SERVICE_ID:
        load_agent_dotenv(project_root)
        restore_lab_credentials_for_deploy(project_root)
        require_bedrock_credentials(project_root)

    specs = _service_specs(project_root, with_agent=(service_id == AGENT_SERVICE_ID))
    try:
        spec = _spec_for_id(specs, service_id)
    except KeyError:
        print(f"[deploy] ERROR: unknown service {service_id!r}")
        raise SystemExit(1) from None

    session = boto3.Session(region_name=region)
    sts = session.client("sts")
    ecs = session.client("ecs")
    app_autoscaling = session.client("application-autoscaling")
    ecr = session.client("ecr")
    logs = session.client("logs")
    account_id = sts.get_caller_identity()["Account"]

    alb_base = (base_url or "").rstrip("/")
    password = (os.environ.get("DIJKFOOD_DB_PASSWORD") or "").strip()

    def _ordering_environment_redeploy() -> list[dict[str, str]]:
        nonlocal password
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
            password = _db_password_from_ecs_service(
                ecs,
                cluster=state.cluster_name,
                service_name=rec.service_name,
            )
        if not password:
            print(
                "[deploy] ERROR: no DB password for redeploy: set DIJKFOOD_DB_PASSWORD in .env, "
                "or ensure this service's current ECS task has DB_PASSWORD (from a prior deploy)."
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
            {
                "name": "DYNAMODB_ROUTES_TABLE",
                "value": state.dynamo_routes_table or "",
            },
        ]

    def _routing_environment_redeploy() -> list[dict[str, str]]:
        env = [
            {"name": "AWS_REGION", "value": region},
            {"name": "AWS_DEFAULT_REGION", "value": region},
            {"name": "SERVICE_ID", "value": "routing"},
            {"name": "OSMNX_PLACE", "value": "São Paulo, SP, Brazil"},
            {"name": "ROUTING_NETWORK_TYPE", "value": "drive"},
            {"name": "UVICORN_WORKERS", "value": "4"},
        ]
        if state.routing_graph_s3_bucket:
            env.append(
                {
                    "name": "ROUTING_GRAPH_S3_BUCKET",
                    "value": state.routing_graph_s3_bucket,
                }
            )
        if state.dynamo_routes_table:
            env.append(
                {
                    "name": "DYNAMODB_ROUTES_TABLE",
                    "value": state.dynamo_routes_table,
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
    elif service_id == AGENT_SERVICE_ID:
        if not state.dynamo_agent_sessions_table:
            print("[deploy] ERROR: DYNAMO_AGENT_SESSIONS_TABLE missing; deploy with --with-agent first")
            raise SystemExit(1)
        env = build_agent_task_environment(
            region=region,
            alb_base_url=alb_base,
            sessions_table=state.dynamo_agent_sessions_table or "",
            project_root=project_root,
        )
    else:
        env = _stub_environment_redeploy(service_id)

    print(f"[deploy] --- Single-service redeploy: {service_id} (cluster={state.cluster_name}) ---")
    if service_id == "routing" and state.routing_graph_s3_bucket:
        upload_routing_graph_seed_if_present(
            session.client("s3"), state.routing_graph_s3_bucket
        )
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
    if apply_service_spec:
        dc = int(spec["desired_count"])
        print(
            f"[deploy] --recreate: desired_count={dc} from _service_specs "
            "(cpu/memory already in new task revision; single UpdateService)"
        )
    update_ecs_service_task_definition(
        ecs,
        cluster=state.cluster_name,
        service=rec.service_name,
        task_definition_arn=td_arn,
        health_check_grace_period_seconds=grace,
        desired_count=(int(spec["desired_count"]) if apply_service_spec else None),
    )
    _configure_service_autoscaling_if_enabled(
        app_autoscaling,
        cluster_name=state.cluster_name,
        service_name=rec.service_name,
        spec=spec,
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
        destroy_agent_sessions_table(ddb, state)
    except ClientError as e:
        print(f"[deploy] Teardown agent sessions DynamoDB: {e}")
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
    parser.add_argument(
        "--recreate",
        action="store_true",
        help=(
            "With --service: apply desired_count from deploy.py _service_specs via the same "
            "ECS UpdateService as the new task definition (no service teardown). CPU and memory "
            "from _service_specs are already applied on every --service run as a new task revision."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse deployment id and existing RDS/Dynamo/S3 from connection.env when present "
            "(no second instance/tables/bucket). Rebuilds and rolls all ECS services. "
            "Implies --skip-teardown. DB password: DIJKFOOD_DB_PASSWORD or reuse DB_PASSWORD "
            "from an existing ordering/tracking ECS task definition."
        ),
    )
    parser.add_argument(
        "--with-agent",
        action="store_true",
        help=(
            "Deploy conversational agent on the lab ALB (4th ECS service). "
            "Requires .env.agent with Bedrock API keys (BEDROCK_* injected on task; no Bedrock-account infra)."
        ),
    )
    args = parser.parse_args()

    project_root = _PROJECT_ROOT

    if args.teardown_only and args.service:
        parser.error("cannot combine --teardown-only and --service")
    if args.teardown_only and args.resume:
        parser.error("cannot combine --teardown-only and --resume")
    if args.service and args.resume:
        parser.error("cannot combine --service and --resume")
    if args.recreate and not args.service:
        parser.error("--recreate requires --service")
    if args.with_agent and args.teardown_only:
        parser.error("cannot combine --with-agent and --teardown-only")
    if args.with_agent and args.service:
        parser.error("cannot combine --with-agent and --service")
    if args.resume:
        args.skip_teardown = True

    if args.with_agent:
        load_agent_dotenv(project_root)
        restore_lab_credentials_for_deploy(project_root)
        try:
            require_bedrock_credentials(project_root)
        except RuntimeError as e:
            print(f"[deploy] ERROR: {e}")
            sys.exit(1)

    if args.service:
        if args.service == AGENT_SERVICE_ID:
            load_agent_dotenv(project_root)
            restore_lab_credentials_for_deploy(project_root)
            require_bedrock_credentials(project_root)
        redeploy_single_service(
            project_root, args.service, apply_service_spec=args.recreate
        )
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

    deploy_region = REGION
    base_url_from_conn = ""

    if args.resume:
        if not CONNECTION_ENV_PATH.is_file():
            print(f"[deploy] ERROR: --resume requires {CONNECTION_ENV_PATH}")
            sys.exit(1)
        try:
            state, deploy_region, base_url_from_conn = load_connection_env()
        except (FileNotFoundError, ValueError) as e:
            print(f"[deploy] ERROR: {e}")
            sys.exit(1)
        resume_pw = (os.environ.get("DIJKFOOD_DB_PASSWORD") or "").strip()
        if not resume_pw and state.cluster_name:
            ecs_probe = boto3.Session(region_name=deploy_region).client("ecs")
            for sid in ("ordering", "tracking"):
                rec = next(
                    (r for r in state.ecs_services if r.service_id == sid), None
                )
                if rec is None:
                    continue
                resume_pw = _db_password_from_ecs_service(
                    ecs_probe,
                    cluster=state.cluster_name,
                    service_name=rec.service_name,
                )
                if resume_pw:
                    print(
                        f"[deploy] --resume: using DB_PASSWORD from ECS task ({sid}); "
                        "set DIJKFOOD_DB_PASSWORD to use a different value."
                    )
                    break
        if not resume_pw:
            print(
                "[deploy] ERROR: --resume needs the RDS password for schema bootstrap: "
                "set DIJKFOOD_DB_PASSWORD in .env, or ensure ordering/tracking still has "
                "DB_PASSWORD on its current task definition."
            )
            sys.exit(1)
        suffix = state.suffix
        password = resume_pw
    else:
        suffix = secrets.token_hex(3)
        password = _db_password()
        state = DeploymentState(suffix=suffix)

    try:
        resolve_container_cli()
    except FileNotFoundError as e:
        print(f"[deploy] ERROR: {e}")
        sys.exit(1)

    session = boto3.Session(region_name=deploy_region)
    ec2 = session.client("ec2")
    rds = session.client("rds")
    sts = session.client("sts")
    elbv2 = session.client("elbv2")
    ecs = session.client("ecs")
    app_autoscaling = session.client("application-autoscaling")
    ecr = session.client("ecr")
    iam = session.client("iam")
    logs = session.client("logs")
    ddb = session.client("dynamodb")
    s3 = session.client("s3")

    account_id = sts.get_caller_identity()["Account"]

    exit_code = 1
    alb_dns: str | None = None

    try:
        skip_rds = args.resume and bool(state.rds_instance_id)
        skip_dynamo = args.resume and bool(
            state.dynamo_order_logs_table
            and state.dynamo_courier_positions_table
            and state.dynamo_routes_table
        )
        skip_agent_dynamo = args.resume and bool(state.dynamo_agent_sessions_table)
        skip_s3 = args.resume and bool(state.routing_graph_s3_bucket)
        saved_ecs_records = list(state.ecs_services)
        ecs_rebuild_only = (
            args.resume
            and bool(state.cluster_name)
            and len(saved_ecs_records) > 0
        )

        print(f"[deploy] DeploymentId={suffix} region={deploy_region}")
        if args.resume:
            print(
                "[deploy] --resume: "
                f"RDS={'skip' if skip_rds else 'create'}, "
                f"DynamoDB={'skip' if skip_dynamo else 'create'}, "
                f"S3={'skip' if skip_s3 else 'create'}, "
                f"ECS={'rebuild-only' if ecs_rebuild_only else 'full'}"
            )
        vpc_id = get_default_vpc_id(ec2)
        subnet_ids = get_default_subnet_ids(ec2, vpc_id)

        if skip_rds:
            print("[deploy] --- RDS (resume: existing instance) ---")
            if not state.rds_sg_id:
                raise RuntimeError(
                    "Resume requires RDS_SECURITY_GROUP_ID in connection.env"
                )
            rds_sg = state.rds_sg_id
            endpoint = _refresh_rds_endpoint(rds, state)
            _snapshot_connection_env(state, region=deploy_region)
        else:
            print("[deploy] --- RDS ---")
            rds_sg = create_rds_security_group(ec2, vpc_id, suffix, state)
            create_db_subnet_group(rds, subnet_ids, suffix, state)
            _snapshot_connection_env(state, region=deploy_region)

            db_instance_id = f"dijkfood-db-{suffix}"
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
            _snapshot_connection_env(state, region=deploy_region)

        run_schema_bootstrap(
            endpoint,
            5432,
            DB_NAME,
            DB_MASTER_USER,
            password,
        )
        _snapshot_connection_env(state, region=deploy_region)

        if skip_dynamo:
            print("[deploy] --- DynamoDB (resume: existing tables) ---")
            _hydrate_dynamo_arns(ddb, state)
        else:
            print("[deploy] --- DynamoDB ---")
            create_dynamodb_tables(ddb, suffix, state)
        _snapshot_connection_env(state, region=deploy_region)

        if skip_s3:
            print("[deploy] --- S3 (resume: existing bucket) ---")
            if not state.routing_graph_s3_bucket:
                raise RuntimeError(
                    "connection.env promised S3 reuse but ROUTING_GRAPH_S3_BUCKET is empty"
                )
        else:
            print("[deploy] --- S3 (routing graph cache) ---")
            create_routing_graph_bucket(s3, suffix, deploy_region, state)
        upload_routing_graph_seed_if_present(s3, state.routing_graph_s3_bucket or "")
        _snapshot_connection_env(state, region=deploy_region)

        exec_arn = ensure_execution_role(iam, suffix, state)
        task_arn = ensure_task_role(iam, suffix, state)
        task_role_name = f"dijkfood-ecs-task-{suffix}"
        using_external_task_role = bool((os.environ.get("TASK_ROLE_ARN") or "").strip())
        if (
            not using_external_task_role
            and state.dynamo_order_logs_arn
            and state.dynamo_courier_positions_arn
            and state.dynamo_routes_arn
        ):
            attach_dynamo_policy_to_task_role(
                iam,
                task_role_name,
                state.dynamo_order_logs_arn,
                state.dynamo_courier_positions_arn,
                state.dynamo_routes_arn,
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
        if args.with_agent:
            _ensure_agent_dynamodb_and_iam(
                ddb,
                iam,
                suffix,
                state,
                skip_create=skip_agent_dynamo,
                using_external_task_role=using_external_task_role,
            )
            if using_external_task_role:
                print(
                    "[deploy] TASK_ROLE_ARN set: add DynamoDB on the agent sessions table "
                    "and bedrock:Converse for your model to that role (Bedrock uses BEDROCK_* keys on the task)."
                )
            _snapshot_connection_env(state, region=deploy_region)

        specs = _service_specs(project_root, with_agent=args.with_agent)

        def _ordering_env(alb_base_url: str) -> list[dict[str, str]]:
            return [
                {"name": "DB_HOST", "value": endpoint},
                {"name": "DB_PORT", "value": "5432"},
                {"name": "DB_NAME", "value": DB_NAME},
                {"name": "DB_USER", "value": DB_MASTER_USER},
                {"name": "DB_PASSWORD", "value": password},
                {"name": "AWS_REGION", "value": deploy_region},
                {"name": "AWS_DEFAULT_REGION", "value": deploy_region},
                {"name": "ROUTING_BASE_URL", "value": alb_base_url},
                {
                    "name": "DYNAMODB_ORDER_LOGS_TABLE",
                    "value": state.dynamo_order_logs_table or "",
                },
                {
                    "name": "DYNAMODB_COURIER_POSITIONS_TABLE",
                    "value": state.dynamo_courier_positions_table or "",
                },
            {
                "name": "DYNAMODB_ROUTES_TABLE",
                "value": state.dynamo_routes_table or "",
            },
            ]

        def _routing_env() -> list[dict[str, str]]:
            env = [
                {"name": "AWS_REGION", "value": deploy_region},
                {"name": "AWS_DEFAULT_REGION", "value": deploy_region},
                {"name": "SERVICE_ID", "value": "routing"},
                {"name": "OSMNX_PLACE", "value": "São Paulo, SP, Brazil"},
                {"name": "ROUTING_NETWORK_TYPE", "value": "drive"},
                {"name": "UVICORN_WORKERS", "value": "4"},
            ]
            if state.routing_graph_s3_bucket:
                env.append(
                    {
                        "name": "ROUTING_GRAPH_S3_BUCKET",
                        "value": state.routing_graph_s3_bucket,
                    }
                )
            if state.dynamo_routes_table:
                env.append(
                    {
                        "name": "DYNAMODB_ROUTES_TABLE",
                        "value": state.dynamo_routes_table,
                    }
                )
            return env

        def _stub_env(label: str) -> list[dict[str, str]]:
            return [
                {"name": "AWS_REGION", "value": deploy_region},
                {"name": "AWS_DEFAULT_REGION", "value": deploy_region},
                {"name": "SERVICE_ID", "value": label},
            ]

        if ecs_rebuild_only:
            print("[deploy] --- ECS (resume: rebuild images; ALB unchanged) ---")
            if not state.ecs_task_sg_id:
                raise RuntimeError(
                    "Resume ECS rebuild requires ECS_TASK_SECURITY_GROUP_ID in connection.env"
                )
            bu = (base_url_from_conn or "").strip().rstrip("/")
            if not bu:
                raise RuntimeError(
                    "Resume ECS rebuild requires BASE_URL in connection.env (e.g. http://your-alb-dns)"
                )
            alb_base_url = bu
            if not alb_base_url.startswith("http"):
                alb_base_url = f"http://{alb_base_url}"
            alb_dns = (
                alb_base_url.removeprefix("http://")
                .removeprefix("https://")
                .split("/")[0]
            )

            task_sg = state.ecs_task_sg_id
            allow_rds_from_ecs_tasks(ec2, rds_sg, task_sg)

            rec_by_id = {r.service_id: r for r in saved_ecs_records}
            if len(rec_by_id) != len(saved_ecs_records):
                raise RuntimeError("Duplicate service_id entries in ECS_SERVICES_JSON")
            state.ecs_services = []
            cluster = state.cluster_name
            assert cluster is not None

            for spec in specs:
                svc_id = str(spec["id"])
                rec = rec_by_id.get(svc_id)
                if rec is None:
                    if svc_id == AGENT_SERVICE_ID and args.with_agent:
                        continue
                    raise RuntimeError(
                        f"--resume: missing service_id {svc_id!r} in ECS_SERVICES_JSON"
                    )
                repo_name = f"dijkfood-{spec['ecr_suffix']}-{suffix}"
                ensure_ecr_repository(ecr, repo_name)
                image_uri = build_and_push_image(
                    region=deploy_region,
                    account_id=account_id,
                    repo_name=repo_name,
                    docker_context=spec["docker_context"],  # type: ignore[arg-type]
                    dockerfile=spec["dockerfile"],  # type: ignore[arg-type]
                )
                ensure_log_group(logs, rec.log_group_name)
                if spec["full_stack_env"]:
                    env = _ordering_env(alb_base_url)
                elif svc_id == "routing":
                    env = _routing_env()
                elif svc_id == AGENT_SERVICE_ID:
                    env = build_agent_task_environment(
                        region=deploy_region,
                        alb_base_url=alb_base_url,
                        sessions_table=state.dynamo_agent_sessions_table or "",
                        project_root=project_root,
                    )
                else:
                    env = _stub_env(svc_id)
                create_ecs_service(
                    ecs,
                    cluster_name=cluster,
                    service_name=rec.service_name,
                    service_id=svc_id,
                    task_family=rec.task_definition_family,
                    image_uri=image_uri,
                    execution_role_arn=exec_arn,
                    task_role_arn=task_arn,
                    log_group=rec.log_group_name,
                    region=deploy_region,
                    subnet_ids=subnet_ids,
                    task_sg_id=task_sg,
                    target_group_arn=rec.target_group_arn,
                    suffix=suffix,
                    state=state,
                    environment=env,
                    cpu=str(spec["cpu"]),
                    memory=str(spec["memory"]),
                    desired_count=int(spec["desired_count"]),
                    create_cluster_if_needed=False,
                    health_check_grace_period_seconds=int(
                        spec.get("health_check_grace_seconds", 120)
                    ),
                )
                _configure_service_autoscaling_if_enabled(
                    app_autoscaling,
                    cluster_name=cluster,
                    service_name=rec.service_name,
                    spec=spec,
                )
                wait_for_service_stable(ecs, cluster, rec.service_name)
                _snapshot_connection_env(state, alb_base_url, region=deploy_region)

            if args.with_agent and not _agent_deployed(state):
                _provision_agent_on_existing_alb(
                    project_root=project_root,
                    ecs=ecs,
                    elbv2=elbv2,
                    ecr=ecr,
                    logs=logs,
                    app_autoscaling=app_autoscaling,
                    deploy_region=deploy_region,
                    account_id=account_id,
                    suffix=suffix,
                    state=state,
                    alb_base_url=alb_base_url,
                    subnet_ids=subnet_ids,
                    task_sg=task_sg,
                    exec_arn=exec_arn,
                    task_arn=task_arn,
                    vpc_id=vpc_id,
                )
                _snapshot_connection_env(state, alb_base_url, region=deploy_region)

            agent_line = ""
            if args.with_agent:
                agent_line = f"\n  Agent: {alb_base_url}/agent/v1/chat"
            print(
                f"[deploy] All ECS services stable. ALB: {alb_base_url}{agent_line}\n"
                "[deploy] Run load tests manually, e.g. "
                f"python -m simulator.orchestration.load_sim --base-url {alb_base_url}"
            )
        else:
            print("[deploy] --- Networking / ALB / ECS ---")
            alb_sg = create_alb_security_group(ec2, vpc_id, suffix, state)
            task_sg = create_ecs_task_security_group(ec2, vpc_id, alb_sg, suffix, state)
            allow_rds_from_ecs_tasks(ec2, rds_sg, task_sg)

            tg_by_id: dict[str, str] = {}
            for spec in specs:
                sid = str(spec["id"])
                tg_by_id[sid] = create_target_group(elbv2, vpc_id, suffix, sid)
            _snapshot_connection_env(state, region=deploy_region)

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
            _snapshot_connection_env(state, f"http://{alb_dns}", region=deploy_region)

            cluster = f"dijkfood-{suffix}"
            alb_base_url = f"http://{alb_dns}"

            for i, spec in enumerate(specs):
                svc_id = str(spec["id"])
                repo_name = f"dijkfood-{spec['ecr_suffix']}-{suffix}"
                ensure_ecr_repository(ecr, repo_name)
                image_uri = build_and_push_image(
                    region=deploy_region,
                    account_id=account_id,
                    repo_name=repo_name,
                    docker_context=spec["docker_context"],  # type: ignore[arg-type]
                    dockerfile=spec["dockerfile"],  # type: ignore[arg-type]
                )
                log_grp = f"/ecs/dijkfood-{svc_id}-{suffix}"
                ensure_log_group(logs, log_grp)
                if spec["full_stack_env"]:
                    env = _ordering_env(alb_base_url)
                elif svc_id == "routing":
                    env = _routing_env()
                elif svc_id == AGENT_SERVICE_ID:
                    env = build_agent_task_environment(
                        region=deploy_region,
                        alb_base_url=alb_base_url,
                        sessions_table=state.dynamo_agent_sessions_table or "",
                        project_root=project_root,
                    )
                else:
                    env = _stub_env(svc_id)
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
                    region=deploy_region,
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
                _configure_service_autoscaling_if_enabled(
                    app_autoscaling,
                    cluster_name=cluster,
                    service_name=svc_name,
                    spec=spec,
                )
                wait_for_service_stable(ecs, cluster, svc_name)
                _snapshot_connection_env(
                    state, f"http://{alb_dns}", region=deploy_region
                )

            agent_line = ""
            if args.with_agent:
                agent_line = f"\n  Agent: http://{alb_dns}/agent/v1/chat"
            print(
                f"[deploy] All ECS services stable. ALB: http://{alb_dns}{agent_line}\n"
                "[deploy] Run load tests manually, e.g. "
                f"python -m simulator.orchestration.load_sim --base-url http://{alb_dns}"
            )
        exit_code = 0

    except Exception as e:
        print(f"[deploy] ERROR: {e}")
        if exit_code == 0:
            exit_code = 1
    finally:
        def _snapshot_base_url() -> str:
            if alb_dns:
                return f"http://{alb_dns}"
            raw = (base_url_from_conn or "").strip().rstrip("/")
            if raw:
                return raw if raw.startswith("http") else f"http://{raw}"
            return ""

        if args.skip_teardown:
            print("[deploy] --skip-teardown: leaving AWS resources running.")
            if alb_dns:
                print(f"[deploy] ALB DNS: {alb_dns}")
            # Always refresh snapshot (deploy may have failed after partial provision).
            write_connection_env(
                state,
                _snapshot_base_url(),
                deploy_region,
                quiet=False,
            )
            sys.exit(exit_code)
        if exit_code != 0:
            write_connection_env(
                state,
                _snapshot_base_url(),
                deploy_region,
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
