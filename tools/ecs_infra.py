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

from tools.state import DeploymentState, EcsServiceRecord

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


def ensure_ecr_repository(ecr, repository_name: str) -> None:
    try:
        ecr.create_repository(
            repositoryName=repository_name,
            imageTagMutability="MUTABLE",
            tags=[{"Key": "Project", "Value": TAG_PROJECT}],
        )
        print(f"  [ECR] Repository {repository_name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
            raise
        print(f"  [ECR] Repository {repository_name} exists")


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
    docker_context: Path,
    dockerfile: Path,
    image_tag: str = "latest",
) -> str:
    cli = resolve_container_cli()
    ecr_client = boto3.client("ecr", region_name=region)
    registry = docker_login_ecr(ecr_client, region)
    uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repo_name}:{image_tag}"
    subprocess.run(
        [
            cli,
            "build",
            "-f",
            str(dockerfile),
            "-t",
            uri,
            str(docker_context),
        ],
        check=True,
    )
    subprocess.run([cli, "push", uri], check=True)
    print(f"  [ECR] Pushed {uri}")
    return uri


def create_alb_only(
    elbv2,
    subnet_ids: list[str],
    alb_sg_id: str,
    suffix: str,
    state: DeploymentState,
) -> tuple[str, str]:
    alb_name = f"dfalb{suffix}"[:32]
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
    state.alb_arn = alb_arn
    return alb_arn, dns


def create_target_group(
    elbv2,
    vpc_id: str,
    suffix: str,
    service_id: str,
    *,
    health_check_path: str | None = None,
) -> str:
    tg_name = f"dftg{suffix}-{service_id}"[:32]
    if health_check_path is not None:
        health_path = health_check_path
    elif service_id == "routing":
        health_path = "/routing/ready"
    elif service_id == "agent-ui":
        health_path = "/health"
    elif service_id == "agent":
        health_path = "/agent/health"
    else:
        health_path = "/health"
    interval_s = 60 if service_id == "routing" else 30
    healthy_n = 2
    unhealthy_n = 10 if service_id == "routing" else 5
    tg = elbv2.create_target_group(
        Name=tg_name,
        Protocol="HTTP",
        Port=CONTAINER_PORT,
        VpcId=vpc_id,
        TargetType="ip",
        HealthCheckEnabled=True,
        HealthCheckProtocol="HTTP",
        HealthCheckPath=health_path,
        HealthCheckIntervalSeconds=interval_s,
        HealthyThresholdCount=healthy_n,
        UnhealthyThresholdCount=unhealthy_n,
        Tags=_tags(suffix),
    )
    return tg["TargetGroups"][0]["TargetGroupArn"]


def register_fargate_task_definition(
    ecs,
    *,
    task_family: str,
    image_uri: str,
    execution_role_arn: str,
    task_role_arn: str,
    log_group: str,
    region: str,
    environment: list[dict[str, str]],
    cpu: str = "256",
    memory: str = "512",
    container_name: str = "api",
    log_stream_prefix: str = "api",
) -> str:
    """Register a new Fargate task revision; return task definition ARN."""
    td = ecs.register_task_definition(
        family=task_family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=cpu,
        memory=memory,
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
                "environment": environment,
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": log_stream_prefix,
                    },
                },
            }
        ],
    )
    arn = td["taskDefinition"]["taskDefinitionArn"]
    print(f"  [ECS] Registered {arn}")
    return arn


def ecs_service_exists(ecs, cluster: str, service_name: str) -> bool:
    d = ecs.describe_services(cluster=cluster, services=[service_name])
    for s in d.get("services") or []:
        if s.get("serviceName") == service_name and s.get("status") != "INACTIVE":
            return True
    return False


def update_ecs_service_task_definition(
    ecs,
    *,
    cluster: str,
    service: str,
    task_definition_arn: str,
    force_new_deployment: bool = True,
    health_check_grace_period_seconds: int | None = None,
    desired_count: int | None = None,
) -> None:
    kwargs: dict[str, object] = {
        "cluster": cluster,
        "service": service,
        "taskDefinition": task_definition_arn,
        "forceNewDeployment": force_new_deployment,
        "deploymentConfiguration": {
            "maximumPercent": 200,
            "minimumHealthyPercent": 100,
        },
    }
    if health_check_grace_period_seconds is not None:
        kwargs["healthCheckGracePeriodSeconds"] = health_check_grace_period_seconds
    if desired_count is not None:
        kwargs["desiredCount"] = int(desired_count)
    ecs.update_service(**kwargs)
    parts = ["task definition"]
    if desired_count is not None:
        parts.append(f"desiredCount={int(desired_count)}")
    print(f"  [ECS] Updated service {service} ({', '.join(parts)})")


def configure_ecs_service_autoscaling(
    app_autoscaling,
    *,
    cluster_name: str,
    service_name: str,
    min_capacity: int,
    max_capacity: int,
    cpu_target: float,
    memory_target: float,
    scale_in_cooldown: int = 120,
    scale_out_cooldown: int = 45,
) -> None:
    """Configure ECS desired-count target tracking for CPU and memory."""
    resource_id = f"service/{cluster_name}/{service_name}"

    app_autoscaling.register_scalable_target(
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        MinCapacity=int(min_capacity),
        MaxCapacity=int(max_capacity),
    )

    app_autoscaling.put_scaling_policy(
        PolicyName=f"{service_name}-cpu-target-tracking",
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": float(cpu_target),
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "ECSServiceAverageCPUUtilization"
            },
            "ScaleInCooldown": int(scale_in_cooldown),
            "ScaleOutCooldown": int(scale_out_cooldown),
        },
    )

    app_autoscaling.put_scaling_policy(
        PolicyName=f"{service_name}-memory-target-tracking",
        ServiceNamespace="ecs",
        ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": float(memory_target),
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "ECSServiceAverageMemoryUtilization"
            },
            "ScaleInCooldown": int(scale_in_cooldown),
            "ScaleOutCooldown": int(scale_out_cooldown),
        },
    )


def create_listener_and_rules(
    elbv2,
    *,
    alb_arn: str,
    default_target_group_arn: str,
    path_forward_rules: list[tuple[int, str, str]],
    state: DeploymentState,
) -> str:
    listener = elbv2.create_listener(
        LoadBalancerArn=alb_arn,
        Protocol="HTTP",
        Port=80,
        DefaultActions=[
            {"Type": "forward", "TargetGroupArn": default_target_group_arn}
        ],
    )
    listener_arn = listener["Listeners"][0]["ListenerArn"]
    for priority, path_pattern, tg_arn in path_forward_rules:
        elbv2.create_rule(
            ListenerArn=listener_arn,
            Priority=priority,
            Conditions=[
                {"Field": "path-pattern", "Values": [path_pattern]},
            ],
            Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )
        print(f"  [ALB] Rule {path_pattern} -> {tg_arn[:60]}...")
    state.listener_arn = listener_arn
    return listener_arn


def create_ecs_service(
    ecs,
    *,
    cluster_name: str,
    service_name: str,
    service_id: str,
    task_family: str,
    image_uri: str,
    execution_role_arn: str,
    task_role_arn: str,
    log_group: str,
    region: str,
    subnet_ids: list[str],
    task_sg_id: str,
    target_group_arn: str,
    suffix: str,
    state: DeploymentState,
    environment: list[dict[str, str]],
    cpu: str = "256",
    memory: str = "512",
    desired_count: int = 1,
    container_name: str = "api",
    log_stream_prefix: str = "api",
    create_cluster_if_needed: bool = True,
    health_check_grace_period_seconds: int = 120,
) -> EcsServiceRecord:
    if create_cluster_if_needed:
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
    state.cluster_name = cluster_name

    rev = register_fargate_task_definition(
        ecs,
        task_family=task_family,
        image_uri=image_uri,
        execution_role_arn=execution_role_arn,
        task_role_arn=task_role_arn,
        log_group=log_group,
        region=region,
        environment=environment,
        cpu=cpu,
        memory=memory,
        container_name=container_name,
        log_stream_prefix=log_stream_prefix,
    )

    if ecs_service_exists(ecs, cluster_name, service_name):
        print(f"  [ECS] Service {service_name} already exists; rolling new task definition")
        update_ecs_service_task_definition(
            ecs,
            cluster=cluster_name,
            service=service_name,
            task_definition_arn=rev,
            health_check_grace_period_seconds=health_check_grace_period_seconds,
        )
    else:
        ecs.create_service(
            cluster=cluster_name,
            serviceName=service_name,
            taskDefinition=rev,
            desiredCount=desired_count,
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
            healthCheckGracePeriodSeconds=health_check_grace_period_seconds,
            deploymentConfiguration={
                "maximumPercent": 200,
                "minimumHealthyPercent": 100,
            },
            tags=[
                {"key": "Project", "value": TAG_PROJECT},
                {"key": "DeploymentId", "value": suffix},
            ],
        )
        print(f"  [ECS] Service {service_name}")

    repo_base = image_uri.split("/")[-1].rsplit(":", 1)[0]
    record = EcsServiceRecord(
        service_id=service_id,
        service_name=service_name,
        ecr_repo_name=repo_base,
        task_definition_family=task_family,
        target_group_arn=target_group_arn,
        log_group_name=log_group,
    )
    state.ecs_services.append(record)
    return record


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
        if desired == 0 and running == 0 and pending == 0:
            return
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


def _delete_ecs_service(ecs, cluster: str, service_name: str) -> None:
    try:
        ecs.update_service(
            cluster=cluster,
            service=service_name,
            desiredCount=0,
        )
        for _ in range(24):
            d = ecs.describe_services(cluster=cluster, services=[service_name])
            if d["services"] and d["services"][0].get("runningCount", 0) == 0:
                break
            time.sleep(10)
        ecs.delete_service(cluster=cluster, service=service_name, force=True)
        print(f"  [teardown] Deleted ECS service {service_name}")
    except ClientError as exc:
        print(f"  [teardown] ECS service {service_name}: {exc.response['Error']['Code']}")


def _delete_ecr_repo(ecr, repo_name: str) -> None:
    try:
        ids: list[dict[str, str]] = []
        paginator = ecr.get_paginator("list_images")
        for page in paginator.paginate(repositoryName=repo_name):
            ids.extend(page.get("imageIds", []))
        if ids:
            for i in range(0, len(ids), 100):
                chunk = ids[i : i + 100]
                ecr.batch_delete_image(repositoryName=repo_name, imageIds=chunk)
        ecr.delete_repository(repositoryName=repo_name, force=True)
        print(f"  [teardown] Deleted ECR {repo_name}")
    except ClientError as exc:
        print(f"  [teardown] ECR {repo_name}: {exc.response['Error']['Code']}")


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

    records = list(state.ecs_services)
    cluster = state.cluster_name

    if cluster:
        for rec in records:
            _delete_ecs_service(ecs, cluster, rec.service_name)
        try:
            ecs.delete_cluster(cluster=cluster)
            print(f"  [teardown] Deleted cluster {cluster}")
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

    for rec in records:
        if rec.target_group_arn:
            try:
                elbv2.delete_target_group(TargetGroupArn=rec.target_group_arn)
            except ClientError as exc:
                print(f"  [teardown] TG: {exc.response['Error']['Code']}")
        if rec.ecr_repo_name:
            _delete_ecr_repo(ecr, rec.ecr_repo_name)
        if rec.log_group_name:
            try:
                logs.delete_log_group(logGroupName=rec.log_group_name)
            except ClientError:
                pass

    state.ecs_services.clear()

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
