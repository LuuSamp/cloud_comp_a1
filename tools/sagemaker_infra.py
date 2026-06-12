"""SageMaker endpoint deploy/teardown for DijkFood predictive layer."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from tools.state import DeploymentState

DEFAULT_DELIVERY_ENDPOINT = "dijkfood-delivery-endpoint"
DEFAULT_DEMAND_MODEL = "dijkfood-demand-model"
DEFAULT_ANOMALY_MODEL = "dijkfood-anomaly-model"
SKLEARN_FRAMEWORK_VERSION = "1.4-2"
SCRIPTS_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "ml" / "sagemaker_scripts"

_ACTIVE_TRAINING = frozenset({"InProgress", "Stopping", "StoppingScheduled"})
_ACTIVE_TRANSFORM = frozenset({"InProgress", "Stopping", "StoppingScheduled"})


def unique_job_name(base: str) -> str:
    """SageMaker job names must be unique; keep base readable with a UTC timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe = base[:28].rstrip("-")
    return f"{safe}-{ts}"


def _wait_training_stopped(sm, job_name: str, *, timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = sm.describe_training_job(TrainingJobName=job_name)["TrainingJobStatus"]
        if status not in _ACTIVE_TRAINING:
            return
        time.sleep(5)
    print(f"  [SageMaker] WARNING: {job_name} still active after stop request")


def cleanup_training_jobs(sm, *, name_contains: str) -> int:
    """Stop and delete prior training jobs matching name_contains (frees lab job quota)."""
    deleted = 0
    paginator = sm.get_paginator("list_training_jobs")
    for page in paginator.paginate(NameContains=name_contains):
        for job in page.get("TrainingJobSummaries", []):
            name = job["TrainingJobName"]
            status = job.get("TrainingJobStatus", "")
            if status in _ACTIVE_TRAINING:
                try:
                    sm.stop_training_job(TrainingJobName=name)
                    print(f"  [SageMaker] Stopping training job {name}")
                    _wait_training_stopped(sm, name)
                except ClientError as exc:
                    print(f"  [SageMaker] stop {name}: {exc.response['Error']['Code']}")
            try:
                sm.delete_training_job(TrainingJobName=name)
                print(f"  [SageMaker] Deleted training job {name}")
                deleted += 1
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code not in {"ValidationException", "ResourceNotFound"}:
                    print(f"  [SageMaker] delete training {name}: {code}")
    return deleted


def cleanup_transform_jobs(sm, *, name_contains: str) -> int:
    """Stop in-progress batch transform jobs matching name_contains.

    SageMaker does not expose DeleteTransformJob; completed jobs are retained
    but incur no charge. New runs use unique_job_name() to avoid name clashes.
    """
    stopped = 0
    paginator = sm.get_paginator("list_transform_jobs")
    for page in paginator.paginate(NameContains=name_contains):
        for job in page.get("TransformJobSummaries", []):
            name = job["TransformJobName"]
            status = job.get("TransformJobStatus", "")
            if status not in _ACTIVE_TRANSFORM:
                continue
            try:
                sm.stop_transform_job(TransformJobName=name)
                print(f"  [SageMaker] Stopping transform job {name}")
                stopped += 1
            except ClientError as exc:
                print(f"  [SageMaker] stop transform {name}: {exc.response['Error']['Code']}")
    return stopped


def cleanup_s3_artifacts(s3, bucket: str, *, hint: str, prefix: str = "ml/artifacts/") -> int:
    """Delete S3 objects for prior job outputs (hint matches training job name fragment)."""
    if not bucket or not hint:
        return 0
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if hint in key:
                keys.append(key)
    if not keys:
        return 0
    deleted = 0
    for i in range(0, len(keys), 1000):
        chunk = [{"Key": k} for k in keys[i : i + 1000]]
        s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
        deleted += len(chunk)
    print(f"  [S3] Deleted {deleted} artifact object(s) matching {hint!r}")
    return deleted


def cleanup_before_training(
    *,
    region: str,
    bucket: str,
    job_hint: str,
) -> None:
    """Remove prior training jobs and their S3 artifacts for one model family."""
    sm = boto3.client("sagemaker", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    print(f"[SageMaker] Cleaning up prior jobs for {job_hint!r}...")
    cleanup_training_jobs(sm, name_contains=job_hint)
    cleanup_s3_artifacts(s3, bucket, hint=job_hint)


def cleanup_s3_prefix(s3, bucket: str, prefix: str) -> int:
    """Delete all objects under an S3 prefix."""
    if not bucket or not prefix:
        return 0
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if key:
                keys.append(key)
    if not keys:
        return 0
    deleted = 0
    for i in range(0, len(keys), 1000):
        chunk = [{"Key": k} for k in keys[i : i + 1000]]
        s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
        deleted += len(chunk)
    print(f"  [S3] Deleted {deleted} object(s) under s3://{bucket}/{prefix}")
    return deleted


def cleanup_before_batch_transform(
    *,
    region: str,
    job_hint: str,
    bucket: str | None = None,
    output_prefix: str | None = None,
) -> None:
    sm = boto3.client("sagemaker", region_name=region)
    print(f"[SageMaker] Cleaning up prior transform jobs for {job_hint!r}...")
    cleanup_transform_jobs(sm, name_contains=job_hint)
    if bucket and output_prefix:
        s3 = boto3.client("s3", region_name=region)
        cleanup_s3_prefix(s3, bucket, output_prefix)


def deploy_delivery_endpoint(
    *,
    model_data: str,
    role_arn: str,
    region: str,
    endpoint_name: str = DEFAULT_DELIVERY_ENDPOINT,
) -> str:
    import sagemaker
    from sagemaker.sklearn import SKLearnModel

    session = sagemaker.Session(boto_session=boto3.Session(region_name=region))
    model = SKLearnModel(
        model_data=model_data,
        role=role_arn,
        entry_point="train_delivery.py",
        source_dir=str(SCRIPTS_DIR),
        framework_version=SKLEARN_FRAMEWORK_VERSION,
        py_version="py3",
        sagemaker_session=session,
    )
    print(f"  [SageMaker] Deploying endpoint {endpoint_name} (ml.t3.medium)...")
    predictor = model.deploy(
        initial_instance_count=1,
        instance_type="ml.t3.medium",
        endpoint_name=endpoint_name,
        wait=True,
    )
    print(f"  [SageMaker] Endpoint {predictor.endpoint_name} InService")
    return predictor.endpoint_name


def deploy_batch_models(
    *,
    demand_model_data: str,
    anomaly_model_data: str,
    role_arn: str,
    region: str,
    state: DeploymentState,
) -> None:
    """Record model artifact URIs; batch transform run via ml/batch_predict.py."""
    state.sagemaker_demand_model_name = demand_model_data
    state.sagemaker_anomaly_model_name = anomaly_model_data
    print(f"  [SageMaker] Demand model artifact: {demand_model_data}")
    print(f"  [SageMaker] Anomaly model artifact: {anomaly_model_data}")


def run_batch_transform(
    *,
    model_data: str,
    input_s3: str,
    output_s3: str,
    role_arn: str,
    region: str,
    job_name: str | None = None,
    kind: str = "demand",
    cleanup_previous: bool = True,
) -> str:
    import sagemaker
    from sagemaker.sklearn import SKLearnModel

    base = job_name or f"dijkfood-{kind}-batch"
    if cleanup_previous:
        bucket = output_s3.replace("s3://", "").split("/", 1)[0] if output_s3.startswith("s3://") else ""
        output_prefix = ""
        if bucket and "/" in output_s3.replace("s3://", "", 1):
            output_prefix = output_s3.replace("s3://", "").split("/", 1)[1]
            if output_prefix and not output_prefix.endswith("/"):
                output_prefix += "/"
        cleanup_before_batch_transform(
            region=region,
            job_hint=base,
            bucket=bucket or None,
            output_prefix=output_prefix or None,
        )
    final_job_name = unique_job_name(base)

    entry = "inference_demand.py" if kind == "demand" else "inference_anomaly.py"
    session = sagemaker.Session(boto_session=boto3.Session(region_name=region))
    model = SKLearnModel(
        model_data=model_data,
        role=role_arn,
        entry_point=entry,
        source_dir=str(SCRIPTS_DIR),
        framework_version=SKLEARN_FRAMEWORK_VERSION,
        py_version="py3",
        sagemaker_session=session,
    )
    transformer = model.transformer(
        instance_count=1,
        instance_type="ml.m5.large",
        output_path=output_s3,
        assemble_with="Line",
        strategy="SingleRecord",
    )
    print(f"  [SageMaker] Batch transform job {final_job_name}")
    transformer.transform(
        input_s3,
        job_name=final_job_name,
        content_type="text/csv",
        split_type="None",
        wait=True,
    )
    return output_s3


def destroy_sagemaker_endpoints(sm, state: DeploymentState) -> None:
    name = state.sagemaker_delivery_endpoint or DEFAULT_DELIVERY_ENDPOINT
    try:
        sm.describe_endpoint(EndpointName=name)
        sm.delete_endpoint(EndpointName=name)
        print(f"  [teardown] Deleted SageMaker endpoint {name}")
        for _ in range(30):
            try:
                sm.describe_endpoint(EndpointName=name)
                time.sleep(5)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ValidationException":
                    break
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationException":
            print(f"  [teardown] SageMaker endpoint {name}: {exc.response['Error']['Code']}")
    state.sagemaker_delivery_endpoint = None
