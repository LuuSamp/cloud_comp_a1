"""
Deploy analytics dashboard (Streamlit) to ECS behind the existing ALB.

Uses connection.env from `python deploy.py --skip-teardown` (or --resume) and
publishes the dashboard at:
  {BASE_URL}/dashboard/

Examples:
  python deploy_dashboard.py
  python deploy_dashboard.py --desired-count 2
  python deploy_dashboard.py --teardown
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import boto3
from botocore.exceptions import ClientError

from tools.connection_env import (
    CONNECTION_ENV_PATH,
    load_connection_env,
    write_connection_env,
)
from tools.ecs_infra import (
    build_and_push_image,
    create_ecs_service,
    create_target_group,
    ecs_service_exists,
    ensure_ecr_repository,
    ensure_log_group,
    register_fargate_task_definition,
    resolve_container_cli,
    update_ecs_service_task_definition,
    wait_for_service_stable,
)
from tools.rds_infra import get_default_subnet_ids, get_default_vpc_id

SERVICE_ID = "dashboard"
PRIMARY_RULE_PRIORITY = 6
PATH_PATTERN = "/dashboard*"
EXTRA_PATH_RULES: list[tuple[int, str]] = [
    (7, "/_stcore/*"),
    (8, "/static/*"),
    (9, "/favicon.png"),
    (10, "/manifest.json"),
]
HEALTH_CHECK_PATH = "/_stcore/health"


def _matching_rule_arn(elbv2, listener_arn: str, path_pattern: str) -> str | None:
    paginator = elbv2.get_paginator("describe_rules")
    for page in paginator.paginate(ListenerArn=listener_arn):
        for rule in page.get("Rules", []):
            for cond in rule.get("Conditions", []):
                if cond.get("Field") != "path-pattern":
                    continue
                values = cond.get("Values") or []
                if path_pattern in values:
                    return rule.get("RuleArn")
    return None


def _priority_rule_arn(elbv2, listener_arn: str, priority: int) -> str | None:
    paginator = elbv2.get_paginator("describe_rules")
    for page in paginator.paginate(ListenerArn=listener_arn):
        for rule in page.get("Rules", []):
            if str(rule.get("Priority")) == str(priority):
                return rule.get("RuleArn")
    return None


def _upsert_listener_rule(
    elbv2,
    *,
    listener_arn: str,
    target_group_arn: str,
    path_pattern: str,
    priority: int,
) -> str:
    actions = [{"Type": "forward", "TargetGroupArn": target_group_arn}]
    conditions = [{"Field": "path-pattern", "Values": [path_pattern]}]

    by_path = _matching_rule_arn(elbv2, listener_arn, path_pattern)
    if by_path:
        elbv2.modify_rule(RuleArn=by_path, Conditions=conditions, Actions=actions)
        print(f"  [ALB] Updated rule {path_pattern}")
        return by_path

    by_priority = _priority_rule_arn(elbv2, listener_arn, priority)
    if by_priority:
        elbv2.modify_rule(RuleArn=by_priority, Conditions=conditions, Actions=actions)
        print(f"  [ALB] Updated rule at priority {priority} -> {path_pattern}")
        return by_priority

    created = elbv2.create_rule(
        ListenerArn=listener_arn,
        Priority=priority,
        Conditions=conditions,
        Actions=actions,
    )
    rule_arn = created["Rules"][0]["RuleArn"]
    print(f"  [ALB] Created rule {path_pattern} (priority {priority})")
    return rule_arn


def _find_service_record(state) -> object | None:
    return next((r for r in state.ecs_services if r.service_id == SERVICE_ID), None)


def _teardown_dashboard(*, ecs, elbv2, state, suffix: str) -> None:
    service_name = f"dijkfood-{SERVICE_ID}-{suffix}"
    cluster = state.cluster_name
    if cluster and ecs_service_exists(ecs, cluster, service_name):
        ecs.update_service(cluster=cluster, service=service_name, desiredCount=0)
        ecs.delete_service(cluster=cluster, service=service_name, force=True)
        print(f"  [ECS] Deleted service {service_name}")

    if state.listener_arn:
        patterns: list[tuple[int, str]] = [(PRIMARY_RULE_PRIORITY, PATH_PATTERN)] + EXTRA_PATH_RULES
        for prio, pattern in patterns:
            rule_arn = _matching_rule_arn(elbv2, state.listener_arn, pattern)
            if not rule_arn:
                rule_arn = _priority_rule_arn(elbv2, state.listener_arn, prio)
            if not rule_arn:
                continue
            try:
                elbv2.delete_rule(RuleArn=rule_arn)
                print(f"  [ALB] Deleted listener rule {pattern}")
            except ClientError as exc:
                print(f"  [ALB] Rule delete ({pattern}): {exc.response['Error']['Code']}")

    rec = _find_service_record(state)
    if rec and rec.target_group_arn:
        try:
            elbv2.delete_target_group(TargetGroupArn=rec.target_group_arn)
            print("  [ALB] Deleted target group")
        except ClientError as exc:
            print(f"  [ALB] TG delete: {exc.response['Error']['Code']}")

    state.ecs_services = [r for r in state.ecs_services if r.service_id != SERVICE_ID]


def deploy_dashboard(*, desired_count: int = 1, teardown: bool = False) -> None:
    try:
        state, region, base_url = load_connection_env()
    except (FileNotFoundError, ValueError) as e:
        print(f"[dashboard] ERROR: {e}")
        raise SystemExit(1) from e

    if not state.cluster_name:
        print("[dashboard] ERROR: ECS_CLUSTER_NAME missing in connection.env")
        raise SystemExit(1)
    if not state.listener_arn:
        print("[dashboard] ERROR: LISTENER_ARN missing in connection.env")
        raise SystemExit(1)
    if not state.ecs_task_sg_id:
        print("[dashboard] ERROR: ECS_TASK_SECURITY_GROUP_ID missing in connection.env")
        raise SystemExit(1)
    if not state.execution_role_arn or not state.task_role_arn:
        print(
            "[dashboard] ERROR: EXECUTION_ROLE_ARN / TASK_ROLE_ARN missing in connection.env"
        )
        raise SystemExit(1)

    suffix = state.suffix
    session = boto3.Session(region_name=region)
    sts = session.client("sts")
    ecs = session.client("ecs")
    ecr = session.client("ecr")
    logs = session.client("logs")
    elbv2 = session.client("elbv2")
    account_id = sts.get_caller_identity()["Account"]

    bu = (base_url or "").strip().rstrip("/")
    if not bu:
        print("[dashboard] ERROR: BASE_URL empty in connection.env")
        raise SystemExit(1)
    if not bu.startswith("http"):
        bu = f"http://{bu}"

    if teardown:
        print(f"[dashboard] --- Teardown (suffix={suffix}) ---")
        _teardown_dashboard(ecs=ecs, elbv2=elbv2, state=state, suffix=suffix)
        write_connection_env(state, bu, region, quiet=True)
        print(f"[dashboard] Wrote {CONNECTION_ENV_PATH}")
        print("[dashboard] Teardown complete.")
        return

    try:
        resolve_container_cli()
    except FileNotFoundError as e:
        print(f"[dashboard] ERROR: {e}")
        raise SystemExit(1) from e

    ec2 = session.client("ec2")
    vpc_id = get_default_vpc_id(ec2)
    subnet_ids = get_default_subnet_ids(ec2, vpc_id)

    repo_name = f"dijkfood-{SERVICE_ID}-{suffix}"
    service_name = f"dijkfood-{SERVICE_ID}-{suffix}"
    family = f"dijkfood-{SERVICE_ID}-{suffix}"
    log_group = f"/ecs/dijkfood-{SERVICE_ID}-{suffix}"

    print(f"[dashboard] --- Deploy Dashboard -> {bu}/dashboard/ (cluster={state.cluster_name}) ---")

    rec = _find_service_record(state)
    tg_arn = rec.target_group_arn if rec else ""
    if not tg_arn:
        tg_arn = create_target_group(
            elbv2,
            vpc_id,
            suffix,
            SERVICE_ID,
            health_check_path=HEALTH_CHECK_PATH,
        )

    _upsert_listener_rule(
        elbv2,
        listener_arn=state.listener_arn,
        target_group_arn=tg_arn,
        path_pattern=PATH_PATTERN,
        priority=PRIMARY_RULE_PRIORITY,
    )
    for prio, pattern in EXTRA_PATH_RULES:
        _upsert_listener_rule(
            elbv2,
            listener_arn=state.listener_arn,
            target_group_arn=tg_arn,
            path_pattern=pattern,
            priority=prio,
        )

    ensure_ecr_repository(ecr, repo_name)
    image_uri = build_and_push_image(
        region=region,
        account_id=account_id,
        repo_name=repo_name,
        docker_context=_PROJECT_ROOT,
        dockerfile=_PROJECT_ROOT / "dashboard" / "Dockerfile",
    )
    ensure_log_group(logs, log_group)

    env = [
        {"name": "AWS_REGION", "value": region},
        {"name": "AWS_DEFAULT_REGION", "value": region},
        {"name": "SERVICE_ID", "value": SERVICE_ID},
        {"name": "DATALAKE_S3_BUCKET", "value": os.environ.get("DATALAKE_S3_BUCKET", "")},
        {"name": "DATALAKE_EVENTS_PREFIX", "value": os.environ.get("DATALAKE_EVENTS_PREFIX", "events/")},
        {"name": "ROUTING_GRAPH_S3_BUCKET", "value": state.routing_graph_s3_bucket or ""},
        {"name": "ATHENA_DB", "value": os.environ.get("ATHENA_DB", "")},
    ]

    if ecs_service_exists(ecs, state.cluster_name, service_name):
        rev = register_fargate_task_definition(
            ecs,
            task_family=family,
            image_uri=image_uri,
            execution_role_arn=state.execution_role_arn,
            task_role_arn=state.task_role_arn,
            log_group=log_group,
            region=region,
            environment=env,
            cpu="512",
            memory="1024",
        )
        update_ecs_service_task_definition(
            ecs,
            cluster=state.cluster_name,
            service=service_name,
            task_definition_arn=rev,
        )
        ecs.update_service(
            cluster=state.cluster_name,
            service=service_name,
            desiredCount=desired_count,
            forceNewDeployment=True,
        )
        print(f"  [ECS] Rolled {service_name} (desiredCount={desired_count})")
    else:
        create_ecs_service(
            ecs,
            cluster_name=state.cluster_name,
            service_name=service_name,
            service_id=SERVICE_ID,
            task_family=family,
            image_uri=image_uri,
            execution_role_arn=state.execution_role_arn,
            task_role_arn=state.task_role_arn,
            log_group=log_group,
            region=region,
            subnet_ids=subnet_ids,
            task_sg_id=state.ecs_task_sg_id,
            target_group_arn=tg_arn,
            suffix=suffix,
            state=state,
            environment=env,
            cpu="512",
            memory="1024",
            desired_count=desired_count,
            create_cluster_if_needed=False,
        )

    wait_for_service_stable(ecs, state.cluster_name, service_name)
    write_connection_env(state, bu, region, quiet=True)
    print(f"[dashboard] Wrote {CONNECTION_ENV_PATH}")
    print(f"[dashboard] Ready: {bu}/dashboard/")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy analytics dashboard to existing DijkFood ALB")
    parser.add_argument(
        "--teardown",
        action="store_true",
        help=f"Remove ECS service, ALB rule ({PATH_PATTERN}), and target group",
    )
    parser.add_argument(
        "--desired-count",
        type=int,
        default=1,
        help="Fargate task count (default 1)",
    )
    args = parser.parse_args()
    if not args.teardown and args.desired_count < 1:
        parser.error("--desired-count must be >= 1 unless --teardown")
    deploy_dashboard(desired_count=args.desired_count, teardown=args.teardown)


if __name__ == "__main__":
    main()
