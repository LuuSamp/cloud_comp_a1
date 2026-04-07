"""
S3 bucket for cached OSMnx routing graphs (GraphML) per deployment.
"""

from __future__ import annotations

import json

from botocore.exceptions import ClientError

from tools.state import DeploymentState

TAG_PROJECT = "dijkfood-a1"
ROUTING_GRAPH_S3_POLICY_NAME = "DijkFoodRoutingGraphS3"


def routing_graph_bucket_name(suffix: str) -> str:
    return f"dijkfood-routing-graph-{suffix}".lower()


def create_routing_graph_bucket(s3, suffix: str, region: str, state: DeploymentState) -> str:
    name = routing_graph_bucket_name(suffix)
    kwargs: dict[str, object] = {"Bucket": name}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        s3.create_bucket(**kwargs)
        print(f"  [S3] Bucket {name}")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
        print(f"  [S3] Bucket {name} exists or already owned")
    try:
        s3.put_bucket_tagging(
            Bucket=name,
            Tagging={
                "TagSet": [
                    {"Key": "Project", "Value": TAG_PROJECT},
                    {"Key": "DeploymentId", "Value": suffix},
                ]
            },
        )
    except ClientError:
        pass
    state.routing_graph_s3_bucket = name
    return name


def attach_routing_graph_s3_policy(iam, task_role_name: str, bucket_name: str) -> None:
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:HeadObject",
                ],
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
            }
        ],
    }
    iam.put_role_policy(
        RoleName=task_role_name,
        PolicyName=ROUTING_GRAPH_S3_POLICY_NAME,
        PolicyDocument=json.dumps(doc),
    )
    print(f"  [IAM] Attached {ROUTING_GRAPH_S3_POLICY_NAME} for s3://{bucket_name}/")


def destroy_routing_graph_bucket(s3, state: DeploymentState) -> None:
    name = state.routing_graph_s3_bucket
    if not name:
        return
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=name):
            versions = page.get("Versions") or []
            markers = page.get("DeleteMarkers") or []
            to_delete: list[dict[str, str]] = []
            for v in versions:
                to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
            for m in markers:
                to_delete.append({"Key": m["Key"], "VersionId": m["VersionId"]})
            for i in range(0, len(to_delete), 1000):
                chunk = to_delete[i : i + 1000]
                if chunk:
                    s3.delete_objects(Bucket=name, Delete={"Objects": chunk})
        paginator2 = s3.get_paginator("list_objects_v2")
        for page in paginator2.paginate(Bucket=name):
            contents = page.get("Contents") or []
            keys = [{"Key": o["Key"]} for o in contents]
            for i in range(0, len(keys), 1000):
                chunk = keys[i : i + 1000]
                if chunk:
                    s3.delete_objects(Bucket=name, Delete={"Objects": chunk})
        s3.delete_bucket(Bucket=name)
        print(f"  [teardown] Deleted S3 bucket {name}")
    except ClientError as exc:
        print(f"  [teardown] S3 bucket {name}: {exc.response['Error']['Code']}")
    state.routing_graph_s3_bucket = None
