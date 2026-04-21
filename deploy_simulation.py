"""
Deploy and control simulator ECS services independently.

Examples:
  python deploy_simulation.py --service customer --action deploy
  python deploy_simulation.py --service food_place --action stop
  python deploy_simulation.py --service courier --action start --desired-count 2
  python deploy_simulation.py --service customer --action shutdown
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from tools.connection_env import load_connection_env
from tools.ecs_infra import (
    build_and_push_image,
    ecs_service_exists,
    ensure_ecr_repository,
    ensure_log_group,
    register_fargate_task_definition,
    resolve_container_cli,
    wait_for_service_stable,
)
from tools.rds_infra import get_default_subnet_ids, get_default_vpc_id

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

@dataclass(frozen=True)
class SimulatorServiceSpec:
    module: str
    docker_context: Path
    dockerfile: Path
    cpu: str
    memory: str
    desired_count: int


SIM_SPECS: Final[dict[str, SimulatorServiceSpec]] = {
    "customer": SimulatorServiceSpec(
        module="simulator.services.customer_simulation_service",
        docker_context=PROJECT_ROOT,
        dockerfile=PROJECT_ROOT / "services" / "customer_simulation_service" / "Dockerfile",
        cpu="256",
        memory="512",
        desired_count=1,
    ),
    "food_place": SimulatorServiceSpec(
        module="simulator.services.food_place_simulation_service",
        docker_context=PROJECT_ROOT,
        dockerfile=PROJECT_ROOT / "services" / "food_place_simulation_service" / "Dockerfile",
        cpu="256",
        memory="512",
        desired_count=1,
    ),
    "courier": SimulatorServiceSpec(
        module="simulator.services.courier_simulation_service",
        docker_context=PROJECT_ROOT,
        dockerfile=PROJECT_ROOT / "services" / "courier_simulation_service" / "Dockerfile",
        cpu="256",
        memory="512",
        desired_count=1,
    ),
}


def _service_names(suffix: str, service_id: str) -> tuple[str, str, str, str]:
    repo = f"dijkfood-sim-{service_id}-{suffix}"
    service_name = f"dijkfood-sim-{service_id}-{suffix}"
    family = f"dijkfood-sim-{service_id}-{suffix}"
    log_group = f"/ecs/dijkfood-sim-{service_id}-{suffix}"
    return repo, service_name, family, log_group


def _base_env(base_url: str, tracking_base_url: str, service_id: str) -> list[dict[str, str]]:
    return [
        {"name": "BASE_URL", "value": base_url.rstrip("/")},
        {"name": "TRACKING_BASE_URL", "value": tracking_base_url.rstrip("/")},
        {"name": "SIM_SERVICE_ID", "value": service_id},
    ]


def _upsert_worker_service(
    ecs,
    *,
    cluster: str,
    service_name: str,
    task_definition_arn: str,
    subnet_ids: list[str],
    task_sg_id: str,
    desired_count: int,
) -> None:
    if ecs_service_exists(ecs, cluster, service_name):
        ecs.update_service(
            cluster=cluster,
            service=service_name,
            taskDefinition=task_definition_arn,
            desiredCount=desired_count,
            forceNewDeployment=True,
        )
        print(f"[deploy_sim] Updated {service_name} (desiredCount={desired_count})")
        return

    ecs.create_service(
        cluster=cluster,
        serviceName=service_name,
        taskDefinition=task_definition_arn,
        desiredCount=desired_count,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnet_ids,
                "securityGroups": [task_sg_id],
                "assignPublicIp": "ENABLED",
            }
        },
    )
    print(f"[deploy_sim] Created {service_name} (desiredCount={desired_count})")


def _describe_service(ecs, cluster: str, service_name: str) -> None:
    d = ecs.describe_services(cluster=cluster, services=[service_name])
    if not d.get("services"):
        print(f"[deploy_sim] {service_name}: not found")
        return
    s = d["services"][0]
    print(
        f"[deploy_sim] {service_name}: status={s.get('status')} "
        f"desired={s.get('desiredCount')} running={s.get('runningCount')} "
        f"pending={s.get('pendingCount')}"
    )


def _run_status(ecs, *, cluster: str, suffix: str, service_ids: list[str]) -> None:
    for service_id in service_ids:
        _, service_name, _, _ = _service_names(suffix, service_id)
        _describe_service(ecs, cluster, service_name)


def _run_shutdown(ecs, *, cluster: str, suffix: str, service_ids: list[str]) -> None:
    for service_id in service_ids:
        _, service_name, _, _ = _service_names(suffix, service_id)
        if ecs_service_exists(ecs, cluster, service_name):
            ecs.delete_service(cluster=cluster, service=service_name, force=True)
            print(f"[deploy_sim] Deleted service {service_name}")
        else:
            print(f"[deploy_sim] Service {service_name} already absent")


def _run_stop_or_start(
    ecs,
    *,
    cluster: str,
    suffix: str,
    service_ids: list[str],
    action: str,
    desired_count_override: int | None,
) -> None:
    for service_id in service_ids:
        spec = SIM_SPECS[service_id]
        _, service_name, _, _ = _service_names(suffix, service_id)
        if not ecs_service_exists(ecs, cluster, service_name):
            raise SystemExit(f"[deploy_sim] ERROR: service {service_name} not found (deploy first)")
        desired_count = (
            int(desired_count_override)
            if desired_count_override is not None
            else int(spec.desired_count)
        )
        new_desired = 0 if action == "stop" else max(1, desired_count)
        ecs.update_service(cluster=cluster, service=service_name, desiredCount=new_desired)
        print(f"[deploy_sim] {action} -> desiredCount={new_desired} for {service_name}")
        wait_for_service_stable(ecs, cluster, service_name)


def _run_deploy(
    *,
    ecs,
    ecr,
    logs,
    region: str,
    account_id: str,
    cluster: str,
    suffix: str,
    service_ids: list[str],
    desired_count_override: int | None,
    cpu_override: str | None,
    memory_override: str | None,
    base_url: str,
    tracking_base_url: str,
    exec_arn: str,
    task_arn: str,
    subnet_ids: list[str],
    task_sg_id: str,
) -> None:
    for service_id in service_ids:
        spec = SIM_SPECS[service_id]
        desired_count = (
            int(desired_count_override)
            if desired_count_override is not None
            else int(spec.desired_count)
        )
        cpu = str(cpu_override or spec.cpu)
        memory = str(memory_override or spec.memory)
        repo, service_name, family, log_group = _service_names(suffix, service_id)

        ensure_ecr_repository(ecr, repo)
        image_uri = build_and_push_image(
            region=region,
            account_id=account_id,
            repo_name=repo,
            docker_context=spec.docker_context,
            dockerfile=spec.dockerfile,
        )
        ensure_log_group(logs, log_group)
        td = register_fargate_task_definition(
            ecs,
            task_family=family,
            image_uri=image_uri,
            execution_role_arn=exec_arn,
            task_role_arn=task_arn,
            log_group=log_group,
            region=region,
            environment=_base_env(base_url, tracking_base_url, service_id),
            cpu=cpu,
            memory=memory,
        )
        _upsert_worker_service(
            ecs,
            cluster=cluster,
            service_name=service_name,
            task_definition_arn=td,
            subnet_ids=subnet_ids,
            task_sg_id=task_sg_id,
            desired_count=max(0, desired_count),
        )
        wait_for_service_stable(ecs, cluster, service_name)
        _describe_service(ecs, cluster, service_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy/control simulation ECS services")
    parser.add_argument("--service", required=True, choices=sorted(SIM_SPECS.keys()) + ["all"])
    parser.add_argument(
        "--action",
        default="deploy",
        choices=["deploy", "stop", "start", "shutdown", "status"],
        help="deploy/redeploy, stop (desired=0), start (desired>0), shutdown (delete service), status",
    )
    parser.add_argument("--desired-count", type=int, default=None)
    parser.add_argument("--cpu", default=None, help="Task CPU override (e.g. 256, 512, 1024)")
    parser.add_argument("--memory", default=None, help="Task memory override (e.g. 512, 1024, 2048)")
    parser.add_argument("--base-url", default=(os.environ.get("BASE_URL") or "").strip())
    parser.add_argument("--tracking-base-url", default=(os.environ.get("TRACKING_BASE_URL") or "").strip())
    args = parser.parse_args()

    state, region, base_url_conn = load_connection_env()
    cluster = (state.cluster_name or "").strip()
    if not cluster:
        raise SystemExit("[deploy_sim] ERROR: connection.env missing ECS_CLUSTER_NAME")
    if not state.ecs_task_sg_id:
        raise SystemExit("[deploy_sim] ERROR: connection.env missing ECS_TASK_SECURITY_GROUP_ID")

    base_url = (args.base_url or base_url_conn or "").strip()
    if not base_url:
        raise SystemExit("[deploy_sim] ERROR: BASE_URL is required (arg, env, or connection.env)")
    tracking_base_url = (args.tracking_base_url or f"{base_url.rstrip('/')}/tracking").strip()

    session = boto3.Session(region_name=region)
    ecs = session.client("ecs")
    ec2 = session.client("ec2")
    sts = session.client("sts")
    ecr = session.client("ecr")
    logs = session.client("logs")
    account_id = sts.get_caller_identity()["Account"]

    suffix = state.suffix
    service_ids = sorted(SIM_SPECS.keys()) if args.service == "all" else [args.service]

    if args.action == "status":
        _run_status(ecs, cluster=cluster, suffix=suffix, service_ids=service_ids)
        return

    if args.action == "shutdown":
        _run_shutdown(ecs, cluster=cluster, suffix=suffix, service_ids=service_ids)
        return

    if args.action in ("stop", "start"):
        _run_stop_or_start(
            ecs,
            cluster=cluster,
            suffix=suffix,
            service_ids=service_ids,
            action=args.action,
            desired_count_override=args.desired_count,
        )
        return

    resolve_container_cli()
    vpc_id = get_default_vpc_id(ec2)
    subnet_ids = get_default_subnet_ids(ec2, vpc_id)

    exec_arn = state.execution_role_arn or (os.environ.get("EXECUTION_ROLE_ARN") or "").strip()
    task_arn = state.task_role_arn or (os.environ.get("TASK_ROLE_ARN") or "").strip()
    if not exec_arn or not task_arn:
        raise SystemExit("[deploy_sim] ERROR: missing execution/task role ARNs in env/connection.env")
    _run_deploy(
        ecs=ecs,
        ecr=ecr,
        logs=logs,
        region=region,
        account_id=account_id,
        cluster=cluster,
        suffix=suffix,
        service_ids=service_ids,
        desired_count_override=args.desired_count,
        cpu_override=args.cpu,
        memory_override=args.memory,
        base_url=base_url,
        tracking_base_url=tracking_base_url,
        exec_arn=exec_arn,
        task_arn=task_arn,
        subnet_ids=subnet_ids,
        task_sg_id=state.ecs_task_sg_id,
    )


if __name__ == "__main__":
    main()
