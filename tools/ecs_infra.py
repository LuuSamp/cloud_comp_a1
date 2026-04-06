"""
ECR, IAM, CloudWatch Logs, ALB, ECS Fargate service.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from tools.state import DeploymentState

TAG_PROJECT = "dijkfood-a1"
CONTAINER_PORT = 8000


def resolve_container_cli() -> str:
    """Return absolute path to docker or podman (DOCKER_CMD), or raise FileNotFoundError."""
    name = (os.environ.get("DOCKER_CMD") or "docker").strip() or "docker"
    resolved = shutil.which(name)
    if not resolved:
        raise FileNotFoundError(
            f"{name!r} not found on PATH. Install Docker Engine and ensure it is on PATH, "
            "or set DOCKER_CMD to another CLI (e.g. podman) in .env."
        )
    return resolved


def _tags(suffix: str) -> list[dict[str, str]]:
    return [
        {"Key": "Project", "Value": TAG_PROJECT},
        {"Key": "DeploymentId", "Value": suffix},
    ]


def create_alb_security_group(ec2, vpc_id: str, suffix: str, state: DeploymentState) -> str:
    name = f"dijkfood-alb-sg-{suffix}"
    try:
        r = ec2.create_security_group(
            GroupName=name,
            Description="DijkFood ALB HTTP",
            VpcId=vpc_id,
            TagSpecifications=[
                {"ResourceType": "security-group", "Tags": _tags(suffix)}
            ],
        )
        sg_id = r["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )
        print(f"  [ALB SG] {sg_id}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise
        existing = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}]
        )
        sg_id = existing["SecurityGroups"][0]["GroupId"]
        print(f"  [ALB SG] Reusing {sg_id}")
    state.alb_sg_id = sg_id
    state.note_sg(sg_id)
    return sg_id


def create_ecs_task_security_group(ec2, vpc_id: str, alb_sg_id: str, suffix: str, state: DeploymentState) -> str:
    name = f"dijkfood-ecs-sg-{suffix}"
    try:
        r = ec2.create_security_group(
            GroupName=name,
            Description="DijkFood ECS tasks",
            VpcId=vpc_id,
            TagSpecifications=[
                {"ResourceType": "security-group", "Tags": _tags(suffix)}
            ],
        )
        sg_id = r["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": CONTAINER_PORT,
                    "ToPort": CONTAINER_PORT,
                    "UserIdGroupPairs": [{"GroupId": alb_sg_id}],
                }
            ],
        )
        print(f"  [ECS SG] {sg_id} (ingress {CONTAINER_PORT} from ALB)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise
        existing = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}]
        )
        sg_id = existing["SecurityGroups"][0]["GroupId"]
        print(f"  [ECS SG] Reusing {sg_id}")
    state.ecs_task_sg_id = sg_id
    state.note_sg(sg_id)
    return sg_id


def ensure_execution_role(iam, suffix: str, state: DeploymentState) -> str:
    override = (os.environ.get("EXECUTION_ROLE_ARN") or "").strip()
    if override:
        state.execution_role_arn = override
        print("  [IAM] Using EXECUTION_ROLE_ARN from environment (skip CreateRole)")
        return override

    name = f"dijkfood-ecs-exec-{suffix}"
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        r = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[{"Key": "Project", "Value": TAG_PROJECT}],
        )
        arn = r["Role"]["Arn"]
        iam.attach_role_policy(
            RoleName=name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        )
        print(f"  [IAM] Execution role {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  [IAM] Reusing execution role {name}")
    state.execution_role_arn = arn
    return arn


def ensure_task_role(iam, suffix: str, state: DeploymentState) -> str:
    override = (os.environ.get("TASK_ROLE_ARN") or "").strip()
    if override:
        state.task_role_arn = override
        print("  [IAM] Using TASK_ROLE_ARN from environment (skip CreateRole)")
        return override

    name = f"dijkfood-ecs-task-{suffix}"
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        r = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[{"Key": "Project", "Value": TAG_PROJECT}],
        )
        arn = r["Role"]["Arn"]
        print(f"  [IAM] Task role {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  [IAM] Reusing task role {name}")
    state.task_role_arn = arn
    return arn


def ensure_log_group(logs, name: str) -> None:
    try:
        logs.create_log_group(logGroupName=name, tags={"Project": TAG_PROJECT})
        print(f"  [Logs] Created {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceAlreadyExistsException":
            print(f"  [Logs] Using existing {name}")
        else:
            raise


def create_ecr_repo(ecr, suffix: str, state: DeploymentState) -> str:
    name = f"dijkfood-api-{suffix}"
    try:
        ecr.create_repository(
            repositoryName=name,
            imageTagMutability="MUTABLE",
            tags=[{"Key": "Project", "Value": TAG_PROJECT}],
        )
        print(f"  [ECR] Repository {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
            raise
        print(f"  [ECR] Repository {name} exists")
    state.ecr_repo_name = name
    return name


def docker_login_ecr(ecr_client, region: str) -> str:
    cli = resolve_container_cli()
    tok = ecr_client.get_authorization_token()
    data = tok["authorizationData"][0]
    raw = base64.b64decode(data["authorizationToken"]).decode()
    _user, password = raw.split(":", 1)
    registry = data["proxyEndpoint"].replace("https://", "")
    subprocess.run(
        [cli, "login", "--username", "AWS", "--password-stdin", registry],
        input=(password + "\n").encode(),
        check=True,
        capture_output=True,
    )
    return registry


def build_and_push_image(
    *,
    region: str,
    account_id: str,
    repo_name: str,
    project_root: Path,
    image_tag: str = "latest",
) -> str:
    cli = resolve_container_cli()
    ecr_client = boto3.client("ecr", region_name=region)
    registry = docker_login_ecr(ecr_client, region)
    uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repo_name}:{image_tag}"
    dockerfile_dir = project_root / "app"
    subprocess.run(
        [
            cli,
            "build",
            "-t",
            uri,
            str(dockerfile_dir),
        ],
        check=True,
    )
    subprocess.run([cli, "push", uri], check=True)
    print(f"  [ECR] Pushed {uri}")
    return uri


def create_alb(
    elbv2,
    vpc_id: str,
    subnet_ids: list[str],
    alb_sg_id: str,
    suffix: str,
    state: DeploymentState,
) -> tuple[str, str, str, str]:
    alb_name = f"dfalb{suffix}"[:32]
    tg_name = f"dftg{suffix}"[:32]
    waf = elbv2.create_load_balancer(
        Name=alb_name,
        Subnets=subnet_ids,
        SecurityGroups=[alb_sg_id],
        Scheme="internet-facing",
        Type="application",
        IpAddressType="ipv4",
        Tags=_tags(suffix),
    )
    alb_arn = waf["LoadBalancers"][0]["LoadBalancerArn"]
    dns = waf["LoadBalancers"][0]["DNSName"]
    print(f"  [ALB] {dns}")

    tg = elbv2.create_target_group(
        Name=tg_name,
        Protocol="HTTP",
        Port=CONTAINER_PORT,
        VpcId=vpc_id,
        TargetType="ip",
        HealthCheckEnabled=True,
        HealthCheckProtocol="HTTP",
        HealthCheckPath="/health",
        HealthCheckIntervalSeconds=30,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=5,
        Tags=_tags(suffix),
    )
    tg_arn = tg["TargetGroups"][0]["TargetGroupArn"]

    listener = elbv2.create_listener(
        LoadBalancerArn=alb_arn,
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
    )
    listener_arn = listener["Listeners"][0]["ListenerArn"]
    state.alb_arn = alb_arn
    state.listener_arn = listener_arn
    state.target_group_arn = tg_arn
    return alb_arn, listener_arn, tg_arn, dns


def create_ecs_service(
    ecs,
    *,
    cluster_name: str,
    service_name: str,
    task_family: str,
    image_uri: str,
    execution_role_arn: str,
    task_role_arn: str,
    log_group: str,
    region: str,
    subnet_ids: list[str],
    task_sg_id: str,
    target_group_arn: str,
    db_host: str,
    db_port: str,
    db_name: str,
    db_user: str,
    db_password: str,
    dynamo_order_logs_table: str,
    dynamo_courier_positions_table: str,
    suffix: str,
    state: DeploymentState,
) -> None:
    try:
        ecs.create_cluster(
            clusterName=cluster_name,
            tags=[{"key": "Project", "value": TAG_PROJECT}],
        )
        print(f"  [ECS] Cluster {cluster_name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ClusterAlreadyExistsException":
            raise
        print(f"  [ECS] Cluster {cluster_name} exists")

    container_name = "api"
    td = ecs.register_task_definition(
        family=task_family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="256",
        memory="512",
        executionRoleArn=execution_role_arn,
        taskRoleArn=task_role_arn,
        containerDefinitions=[
            {
                "name": container_name,
                "image": image_uri,
                "essential": True,
                "portMappings": [
                    {"containerPort": CONTAINER_PORT, "protocol": "tcp"}
                ],
                "environment": [
                    {"name": "DB_HOST", "value": db_host},
                    {"name": "DB_PORT", "value": db_port},
                    {"name": "DB_NAME", "value": db_name},
                    {"name": "DB_USER", "value": db_user},
                    {"name": "DB_PASSWORD", "value": db_password},
                    {"name": "AWS_REGION", "value": region},
                    {"name": "AWS_DEFAULT_REGION", "value": region},
                    {
                        "name": "DYNAMODB_ORDER_LOGS_TABLE",
                        "value": dynamo_order_logs_table,
                    },
                    {
                        "name": "DYNAMODB_COURIER_POSITIONS_TABLE",
                        "value": dynamo_courier_positions_table,
                    },
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "api",
                    },
                },
            }
        ],
    )
    rev = td["taskDefinition"]["taskDefinitionArn"]
    print(f"  [ECS] Registered {rev}")
    state.task_definition_family = task_family

    ecs.create_service(
        cluster=cluster_name,
        serviceName=service_name,
        taskDefinition=rev,
        desiredCount=1,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnet_ids,
                "securityGroups": [task_sg_id],
                "assignPublicIp": "ENABLED",
            }
        },
        loadBalancers=[
            {
                "targetGroupArn": target_group_arn,
                "containerName": container_name,
                "containerPort": CONTAINER_PORT,
            }
        ],
        healthCheckGracePeriodSeconds=120,
        tags=[
            {"key": "Project", "value": TAG_PROJECT},
            {"key": "DeploymentId", "value": suffix},
        ],
    )
    print(f"  [ECS] Service {service_name}")
    state.cluster_name = cluster_name
    state.service_name = service_name


def wait_for_service_stable(ecs, cluster: str, service: str, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        d = ecs.describe_services(cluster=cluster, services=[service])
        if not d["services"]:
            raise RuntimeError("Service not found")
        s = d["services"][0]
        running = s.get("runningCount", 0)
        desired = s.get("desiredCount", 0)
        pending = s.get("pendingCount", 0)
        print(f"  [ECS] desired={desired} running={running} pending={pending}")
        if running >= desired and desired > 0 and pending == 0:
            # extra wait for ALB health
            time.sleep(15)
            return
        time.sleep(15)
    raise TimeoutError("ECS service did not stabilize in time")


def revoke_rds_access_from_ecs(ec2, rds_sg_id: str | None, ecs_task_sg_id: str | None) -> None:
    if not rds_sg_id or not ecs_task_sg_id:
        return
    try:
        ec2.revoke_security_group_ingress(
            GroupId=rds_sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 5432,
                    "ToPort": 5432,
                    "UserIdGroupPairs": [{"GroupId": ecs_task_sg_id}],
                }
            ],
        )
    except ClientError:
        pass


def destroy_ecs_stack(
    ecs,
    elbv2,
    ecr,
    logs,
    ec2,
    state: DeploymentState,
    rds_sg_id: str | None,
) -> None:
    revoke_rds_access_from_ecs(ec2, rds_sg_id, state.ecs_task_sg_id)

    if state.cluster_name and state.service_name:
        try:
            ecs.update_service(
                cluster=state.cluster_name,
                service=state.service_name,
                desiredCount=0,
            )
            for _ in range(24):
                d = ecs.describe_services(
                    cluster=state.cluster_name,
                    services=[state.service_name],
                )
                if d["services"] and d["services"][0].get("runningCount", 0) == 0:
                    break
                time.sleep(10)
            ecs.delete_service(
                cluster=state.cluster_name,
                service=state.service_name,
                force=True,
            )
            print(f"  [teardown] Deleted ECS service {state.service_name}")
        except ClientError as exc:
            print(f"  [teardown] ECS service: {exc.response['Error']['Code']}")
        state.service_name = None

    if state.cluster_name:
        try:
            ecs.delete_cluster(cluster=state.cluster_name)
            print(f"  [teardown] Deleted cluster {state.cluster_name}")
        except ClientError as exc:
            print(f"  [teardown] Cluster: {exc.response['Error']['Code']}")
        state.cluster_name = None

    if state.listener_arn:
        try:
            elbv2.delete_listener(ListenerArn=state.listener_arn)
        except ClientError:
            pass
        state.listener_arn = None

    if state.alb_arn:
        try:
            elbv2.delete_load_balancer(LoadBalancerArn=state.alb_arn)
            time.sleep(10)
        except ClientError as exc:
            print(f"  [teardown] ALB: {exc.response['Error']['Code']}")
        state.alb_arn = None

    if state.target_group_arn:
        try:
            elbv2.delete_target_group(TargetGroupArn=state.target_group_arn)
        except ClientError as exc:
            print(f"  [teardown] TG: {exc.response['Error']['Code']}")
        state.target_group_arn = None

    if state.ecr_repo_name:
        try:
            ids: list[dict[str, str]] = []
            paginator = ecr.get_paginator("list_images")
            for page in paginator.paginate(repositoryName=state.ecr_repo_name):
                ids.extend(page.get("imageIds", []))
            if ids:
                for i in range(0, len(ids), 100):
                    chunk = ids[i : i + 100]
                    ecr.batch_delete_image(
                        repositoryName=state.ecr_repo_name, imageIds=chunk
                    )
            ecr.delete_repository(repositoryName=state.ecr_repo_name, force=True)
            print(f"  [teardown] Deleted ECR {state.ecr_repo_name}")
        except ClientError as exc:
            print(f"  [teardown] ECR: {exc.response['Error']['Code']}")
        state.ecr_repo_name = None

    if state.log_group_name:
        try:
            logs.delete_log_group(logGroupName=state.log_group_name)
        except ClientError:
            pass
        state.log_group_name = None

    if state.ecs_task_sg_id:
        try:
            ec2.delete_security_group(GroupId=state.ecs_task_sg_id)
        except ClientError as exc:
            print(f"  [teardown] ECS SG: {exc.response['Error']['Code']}")
        state.ecs_task_sg_id = None

    if state.alb_sg_id:
        try:
            ec2.delete_security_group(GroupId=state.alb_sg_id)
        except ClientError as exc:
            print(f"  [teardown] ALB SG: {exc.response['Error']['Code']}")
        state.alb_sg_id = None
