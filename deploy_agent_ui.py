"""
Deploy the agent-ui static chat app to ECS behind the existing ALB.

Uses connection.env from `python deploy.py --with-agent`. Serves the UI at:
  {BASE_URL}/ui/

The UI calls the agent API on the same ALB origin (/agent/...), so no CORS setup is required.

Examples:
  python deploy_agent_ui.py
  python deploy_agent_ui.py --teardown
  python deploy_agent_ui.py --desired-count 2
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

LISTENER_RULE_PRIORITY = 5
PATH_PATTERN = "/ui*"
SERVICE_ID = "agent-ui"


def _rule_exists(elbv2, listener_arn: str, priority: int) -> str | None:
    paginator = elbv2.get_paginator("describe_rules")
    for page in paginator.paginate(ListenerArn=listener_arn):
        for rule in page.get("Rules", []):
            if str(rule.get("Priority")) == str(priority):
                return rule["RuleArn"]
    return None


def _upsert_listener_rule(
    elbv2,
    *,
    listener_arn: str,
    target_group_arn: str,
    priority: int = LISTENER_RULE_PRIORITY,
    path_pattern: str = PATH_PATTERN,
    existing_rule_arn: str | None,
) -> str:
    actions = [{"Type": "forward", "TargetGroupArn": target_group_arn}]
    conditions = [{"Field": "path-pattern", "Values": [path_pattern]}]
    if existing_rule_arn:
        elbv2.modify_rule(
            RuleArn=existing_rule_arn,
            Conditions=conditions,
            Actions=actions,
        )
        print(f"  [ALB] Updated rule {path_pattern} (priority {priority})")
        return existing_rule_arn
    existing_at_priority = _rule_exists(elbv2, listener_arn, priority)
    if existing_at_priority:
        elbv2.modify_rule(
            RuleArn=existing_at_priority,
            Conditions=conditions,
            Actions=actions,
        )
        print(f"  [ALB] Updated rule at priority {priority} -> {path_pattern}")
        return existing_at_priority
    created = elbv2.create_rule(
        ListenerArn=listener_arn,
        Priority=priority,
        Conditions=conditions,
        Actions=actions,
    )
    rule_arn = created["Rules"][0]["RuleArn"]
    print(f"  [ALB] Created rule {path_pattern} (priority {priority})")
    return rule_arn


def _teardown_agent_ui(
    *,
    ecs,
    elbv2,
    region: str,
    state,
    suffix: str,
) -> None:
    service_name = f"dijkfood-{SERVICE_ID}-{suffix}"
    cluster = state.cluster_name
    if cluster and ecs_service_exists(ecs, cluster, service_name):
        ecs.update_service(cluster=cluster, service=service_name, desiredCount=0)
        ecs.delete_service(cluster=cluster, service=service_name, force=True)
        print(f"  [ECS] Deleted service {service_name}")

    rule_arn = (state.agent_ui_listener_rule_arn or "").strip()
    if rule_arn:
        try:
            elbv2.delete_rule(RuleArn=rule_arn)
            print(f"  [ALB] Deleted listener rule {rule_arn}")
        except ClientError as exc:
            print(f"  [ALB] Rule delete: {exc.response['Error']['Code']}")

    tg_arn = (state.agent_ui_target_group_arn or "").strip()
    if tg_arn:
        try:
            elbv2.delete_target_group(TargetGroupArn=tg_arn)
            print(f"  [ALB] Deleted target group")
        except ClientError as exc:
            print(f"  [ALB] TG delete: {exc.response['Error']['Code']}")

    state.agent_ui_listener_rule_arn = None
    state.agent_ui_target_group_arn = None
    state.agent_ui_url = None


def deploy_agent_ui(*, desired_count: int = 1, teardown: bool = False) -> None:
    try:
        state, region, base_url = load_connection_env()
    except (FileNotFoundError, ValueError) as e:
        print(f"[agent-ui] ERROR: {e}")
        raise SystemExit(1) from e

    if not state.cluster_name:
        print("[agent-ui] ERROR: ECS_CLUSTER_NAME missing in connection.env")
        raise SystemExit(1)
    if not state.listener_arn:
        print("[agent-ui] ERROR: LISTENER_ARN missing in connection.env")
        raise SystemExit(1)
    if not state.ecs_task_sg_id:
        print("[agent-ui] ERROR: ECS_TASK_SECURITY_GROUP_ID missing in connection.env")
        raise SystemExit(1)
    if not state.execution_role_arn or not state.task_role_arn:
        print(
            "[agent-ui] ERROR: EXECUTION_ROLE_ARN / TASK_ROLE_ARN missing "
            "in connection.env"
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

    if teardown:
        print(f"[agent-ui] --- Teardown (suffix={suffix}) ---")
        _teardown_agent_ui(
            ecs=ecs, elbv2=elbv2, region=region, state=state, suffix=suffix
        )
        write_connection_env(state, base_url, region, quiet=True)
        print(f"[agent-ui] Wrote {CONNECTION_ENV_PATH}")
        print("[agent-ui] Teardown complete.")
        return

    try:
        resolve_container_cli()
    except FileNotFoundError as e:
        print(f"[agent-ui] ERROR: {e}")
        raise SystemExit(1) from e

    bu = (base_url or "").strip().rstrip("/")
    if not bu:
        print("[agent-ui] ERROR: BASE_URL empty in connection.env")
        raise SystemExit(1)
    if not bu.startswith("http"):
        bu = f"http://{bu}"

    ec2 = session.client("ec2")
    vpc_id = get_default_vpc_id(ec2)
    subnet_ids = get_default_subnet_ids(ec2, vpc_id)

    repo_name = f"dijkfood-{SERVICE_ID}-{suffix}"
    service_name = f"dijkfood-{SERVICE_ID}-{suffix}"
    family = f"dijkfood-{SERVICE_ID}-{suffix}"
    log_group = f"/ecs/dijkfood-{SERVICE_ID}-{suffix}"

    print(f"[agent-ui] --- Deploy UI -> {bu}/ui/ (cluster={state.cluster_name}) ---")

    tg_arn = (state.agent_ui_target_group_arn or "").strip()
    if not tg_arn:
        tg_arn = create_target_group(
            elbv2,
            vpc_id,
            suffix,
            SERVICE_ID,
            health_check_path="/health",
        )
    state.agent_ui_target_group_arn = tg_arn

    rule_arn = (state.agent_ui_listener_rule_arn or "").strip() or None
    rule_arn = _upsert_listener_rule(
        elbv2,
        listener_arn=state.listener_arn,
        target_group_arn=tg_arn,
        existing_rule_arn=rule_arn,
    )
    state.agent_ui_listener_rule_arn = rule_arn

    ui_url = f"{bu}/ui/"
    state.agent_ui_url = ui_url

    ensure_ecr_repository(ecr, repo_name)
    image_uri = build_and_push_image(
        region=region,
        account_id=account_id,
        repo_name=repo_name,
        docker_context=_PROJECT_ROOT,
        dockerfile=_PROJECT_ROOT / "agent-ui" / "Dockerfile",
    )
    ensure_log_group(logs, log_group)

    env = [
        {"name": "AWS_REGION", "value": region},
        {"name": "SERVICE_ID", "value": SERVICE_ID},
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
            cpu="256",
            memory="512",
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
            cpu="256",
            memory="512",
            desired_count=desired_count,
            create_cluster_if_needed=False,
        )

    wait_for_service_stable(ecs, state.cluster_name, service_name)
    write_connection_env(state, bu, region, quiet=True)
    print(f"[agent-ui] Wrote {CONNECTION_ENV_PATH}")
    print(f"[agent-ui] Ready: {ui_url}")
    print(f"[agent-ui] Agent API (same origin): {bu}/agent/v1/chat")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy agent-ui to existing DijkFood ALB")
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
    deploy_agent_ui(desired_count=args.desired_count, teardown=args.teardown)


if __name__ == "__main__":
    main()
