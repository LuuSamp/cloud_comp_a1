"""S3 datalake bucket for analytics events and ML artifacts."""

from __future__ import annotations

from botocore.exceptions import ClientError

from tools.state import DeploymentState

TAG_PROJECT = "dijkfood-a1"


def datalake_bucket_name(suffix: str) -> str:
    return f"dijkfood-datalake-{suffix}".lower()


def create_datalake_bucket(s3, suffix: str, region: str, state: DeploymentState) -> str:
    name = datalake_bucket_name(suffix)
    kwargs: dict[str, object] = {"Bucket": name}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        s3.create_bucket(**kwargs)
        print(f"  [S3] Datalake bucket {name}")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
        print(f"  [S3] Datalake bucket {name} exists or already owned")
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
    state.datalake_s3_bucket = name
    return name


def destroy_datalake_bucket(s3, state: DeploymentState) -> None:
    name = state.datalake_s3_bucket
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
        print(f"  [teardown] Deleted datalake S3 bucket {name}")
    except ClientError as exc:
        print(f"  [teardown] Datalake S3 bucket {name}: {exc.response['Error']['Code']}")
    state.datalake_s3_bucket = None
