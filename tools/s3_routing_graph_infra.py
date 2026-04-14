"""
S3 bucket for cached OSMnx routing graphs (GraphML) per deployment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from botocore.exceptions import ClientError

from tools.state import DeploymentState

TAG_PROJECT = "dijkfood-a1"
ROUTING_GRAPH_S3_POLICY_NAME = "DijkFoodRoutingGraphS3"

# Tools root.
_TOOLS_ROOT = Path(__file__).resolve()


# ---------------------------------------------------------------------------
# Optional: local GraphML uploaded during full deploy / --resume after the
# routing graph bucket exists. Edit ROUTING_GRAPH_SEED_SOURCE to point at
# another file or set to None to disable seeding.
#
# SEED_GRAPH_PLACE and SEED_GRAPH_NETWORK_TYPE must match the S3 key that
# services/routing/graph_service.py uses: graph_s3_object_key(place, network_type)
# with the same place and network as OSMNX_PLACE / ROUTING_NETWORK_TYPE on ECS.
# ---------------------------------------------------------------------------

# the graph file should be in tools/graphs/são_paulo_sp_brazil__drive.graphml
ROUTING_GRAPH_SEED_SOURCE = _TOOLS_ROOT / "graphs" / "são_paulo_sp_brazil__drive.graphml"
SEED_GRAPH_PLACE = "São Paulo, SP, Brazil"
SEED_GRAPH_NETWORK_TYPE = "drive"

_MAX_SLUG_LEN = 180


def graph_s3_object_key_for_seed(place: str, network_type: str) -> str:
    """
    Same naming as services/routing/graph_service.graph_s3_object_key — keep logic aligned.
    """
    raw = f"{place}__{network_type}".lower()
    slug = re.sub(r"[^\w]+", "_", raw, flags=re.UNICODE)
    slug = slug.strip("_")
    if len(slug) > _MAX_SLUG_LEN:
        slug = slug[:_MAX_SLUG_LEN].rstrip("_")
    if not slug:
        slug = "graph"
    return f"graphs/{slug}.graphml"


def upload_routing_graph_seed_if_present(s3, bucket: str) -> None:
    """If ROUTING_GRAPH_SEED_SOURCE exists on disk, upload to the routing graph key in S3."""
    if not bucket or not bucket.strip():
        return
    src = ROUTING_GRAPH_SEED_SOURCE
    if src is None:
        return
    path = Path(src)
    if not path.is_file():
        print(f"  [S3] Graph seed not found at {path} (skip upload)")
        return
    key = graph_s3_object_key_for_seed(SEED_GRAPH_PLACE, SEED_GRAPH_NETWORK_TYPE)
    try:
        s3.upload_file(str(path), bucket, key)
        print(f"  [S3] Seeded routing graph s3://{bucket}/{key} from {path.name}")
    except ClientError as exc:
        print(
            f"  [S3] WARNING: could not upload graph seed to s3://{bucket}/{key}: {exc}"
        )


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
