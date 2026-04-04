"""
DijkFood — automated AWS deploy, load test, and teardown.

Prerequisites:
  - AWS credentials in ~/.aws/credentials (default boto3 chain)
  - Docker installed locally (for ECR image build/push)
  - pip install -r requirements-deploy.txt

Optional env:
  DIJKFOOD_DB_PASSWORD — RDS master password (generated if unset)

Usage:
  python deploy.py
  python deploy.py --skip-teardown    # leave resources for debugging
  python deploy.py --sim-rate 20 --sim-duration 30
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import boto3
from botocore.exceptions import ClientError

from tools.ecs_infra import (
    build_and_push_image,
    create_alb,
    create_ecr_repo,
    create_ecs_service,
    create_alb_security_group,
    create_ecs_task_security_group,
    destroy_ecs_stack,
    ensure_execution_role,
    ensure_log_group,
    ensure_task_role,
    wait_for_service_stable,
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


def _run_load_simulator(
    project_root: Path,
    base_url: str,
    rate: float,
    duration: float,
    workers: int,
) -> int:
    cmd = [
        sys.executable,
        "-m",
        "simulator.load_sim",
        "--base-url",
        base_url,
        "--rate",
        str(rate),
        "--duration",
        str(duration),
        "--workers",
        str(workers),
    ]
    print(f"[deploy] Running: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(project_root), check=False)
    return r.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="DijkFood full deploy pipeline")
    parser.add_argument(
        "--skip-teardown",
        action="store_true",
        help="Do not destroy AWS resources after run (debug only)",
    )
    parser.add_argument("--sim-rate", type=float, default=10.0)
    parser.add_argument("--sim-duration", type=float, default=20.0)
    parser.add_argument("--sim-workers", type=int, default=2)
    args = parser.parse_args()

    project_root = _PROJECT_ROOT

    suffix = secrets.token_hex(3)
    password = _db_password()
    state = DeploymentState(suffix=suffix)

    session = boto3.Session(region_name=REGION)
    ec2 = session.client("ec2")
    rds = session.client("rds")
    sts = session.client("sts")
    elbv2 = session.client("elbv2")
    ecs = session.client("ecs")
    ecr = session.client("ecr")
    iam = session.client("iam")
    logs = session.client("logs")

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
        run_schema_bootstrap(
            endpoint,
            5432,
            DB_NAME,
            DB_MASTER_USER,
            password,
        )

        print("[deploy] --- Networking / ALB / ECS ---")
        alb_sg = create_alb_security_group(ec2, vpc_id, suffix, state)
        task_sg = create_ecs_task_security_group(ec2, vpc_id, alb_sg, suffix, state)
        allow_rds_from_ecs_tasks(ec2, rds_sg, task_sg)

        exec_arn = ensure_execution_role(iam, suffix, state)
        task_arn = ensure_task_role(iam, suffix, state)
        time.sleep(10)

        log_group = f"/ecs/dijkfood-{suffix}"
        ensure_log_group(logs, log_group)
        state.log_group_name = log_group

        repo = create_ecr_repo(ecr, suffix, state)
        image_uri = build_and_push_image(
            region=REGION,
            account_id=account_id,
            repo_name=repo,
            project_root=project_root,
        )

        _alb_arn, _ln, _tg, dns = create_alb(
            elbv2, vpc_id, subnet_ids, alb_sg, suffix, state
        )
        alb_dns = dns

        cluster = f"dijkfood-{suffix}"
        service = f"dijkfood-svc-{suffix}"
        family = f"dijkfood-api-{suffix}"

        create_ecs_service(
            ecs,
            cluster_name=cluster,
            service_name=service,
            task_family=family,
            image_uri=image_uri,
            execution_role_arn=exec_arn,
            task_role_arn=task_arn,
            log_group=log_group,
            region=REGION,
            subnet_ids=subnet_ids,
            task_sg_id=task_sg,
            target_group_arn=state.target_group_arn or "",
            db_host=endpoint,
            db_port="5432",
            db_name=DB_NAME,
            db_user=DB_MASTER_USER,
            db_password=password,
            suffix=suffix,
            state=state,
        )
        wait_for_service_stable(ecs, cluster, service)

        base_url = f"http://{alb_dns}"
        print(f"[deploy] --- Load simulator → {base_url} ---")
        exit_code = _run_load_simulator(
            project_root,
            base_url,
            args.sim_rate,
            args.sim_duration,
            args.sim_workers,
        )
        if exit_code != 0:
            raise RuntimeError(f"Load simulator exited with {exit_code}")

    except Exception as e:
        print(f"[deploy] ERROR: {e}")
        if exit_code == 0:
            exit_code = 1
    finally:
        if args.skip_teardown:
            print("[deploy] --skip-teardown: leaving AWS resources running.")
            if alb_dns:
                print(f"[deploy] ALB DNS: {alb_dns}")
            sys.exit(exit_code)
        print("[deploy] --- Teardown ---")
        try:
            destroy_ecs_stack(
                ecs, elbv2, ecr, logs, iam, ec2, state, rds_sg_id=state.rds_sg_id
            )
        except ClientError as e:
            print(f"[deploy] Teardown ECS stack: {e}")
        try:
            destroy_rds(rds, ec2, state)
        except ClientError as e:
            print(f"[deploy] Teardown RDS: {e}")
        print("[deploy] Done.")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
