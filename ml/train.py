"""Train SageMaker sklearn models for delivery, demand, and anomaly."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import boto3
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent / "sagemaker_scripts"
SKLEARN_FRAMEWORK_VERSION = "1.4-2"


def _load_env() -> None:
    load_dotenv(ROOT / ".env", override=False)
    if (ROOT / "connection.env").is_file():
        load_dotenv(ROOT / "connection.env", override=True)


def _role_arn() -> str:
    role = (os.environ.get("TASK_ROLE_ARN") or os.environ.get("SAGEMAKER_ROLE_ARN") or "").strip()
    if not role:
        sts = boto3.client("sts")
        account = sts.get_caller_identity()["Account"]
        role = f"arn:aws:iam::{account}:role/LabRole"
    return role


def _train_one(
    *,
    name: str,
    entry_point: str,
    train_s3: str,
    output_s3: str,
    instance_type: str,
    role: str,
    region: str,
    bucket: str,
    cleanup_previous: bool = True,
) -> str:
    from sagemaker.sklearn import SKLearn

    from tools.sagemaker_infra import cleanup_before_training, unique_job_name

    job_hint = f"dijkfood-{name}"
    if cleanup_previous:
        cleanup_before_training(region=region, bucket=bucket, job_hint=job_hint)
    job_name = unique_job_name(job_hint)

    estimator = SKLearn(
        entry_point=entry_point,
        source_dir=str(SCRIPTS),
        role=role,
        instance_type=instance_type,
        framework_version=SKLEARN_FRAMEWORK_VERSION,
        py_version="py3",
        output_path=output_s3,
        sagemaker_session=__import__("sagemaker").Session(boto3.Session(region_name=region)),
    )
    print(f"  Starting training job {job_name}")
    estimator.fit({"train": train_s3}, job_name=job_name)
    return estimator.model_data


def train_all(
    *,
    bucket: str | None = None,
    instance_type: str = "ml.m5.large",
    deploy_delivery: bool = False,
    cleanup_previous: bool = True,
) -> dict[str, str]:
    _load_env()
    bucket = bucket or os.environ.get("DATALAKE_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("Set DATALAKE_S3_BUCKET in connection.env")
    region = os.environ.get("AWS_REGION", "us-east-1")
    role = _role_arn()
    output = f"s3://{bucket}/ml/artifacts/"
    artifacts: dict[str, str] = {}

    for name, script, prefix in [
        ("delivery", "train_delivery.py", "delivery"),
        ("demand", "train_demand.py", "demand"),
        ("anomaly", "train_anomaly.py", "anomaly"),
    ]:
        train_uri = f"s3://{bucket}/ml/datasets/{prefix}/"
        print(f"Training {name} from {train_uri}...")
        artifacts[name] = _train_one(
            name=name,
            entry_point=script,
            train_s3=train_uri,
            output_s3=output,
            instance_type=instance_type,
            role=role,
            region=region,
            bucket=bucket,
            cleanup_previous=cleanup_previous,
        )
        print(f"  artifact: {artifacts[name]}")

    if deploy_delivery and artifacts.get("delivery"):
        from tools.sagemaker_infra import deploy_delivery_endpoint

        endpoint = deploy_delivery_endpoint(
            model_data=artifacts["delivery"],
            role_arn=role,
            region=region,
            endpoint_name=os.environ.get(
                "SAGEMAKER_DELIVERY_ENDPOINT", "dijkfood-delivery-endpoint"
            ),
        )
        artifacts["delivery_endpoint"] = endpoint
    print("\nAdd to connection.env after training:")
    if artifacts.get("delivery"):
        ep = artifacts.get("delivery_endpoint", "")
        print(f"SAGEMAKER_DELIVERY_ENDPOINT={ep or 'dijkfood-delivery-endpoint'}")
    if artifacts.get("demand"):
        print(f"SAGEMAKER_DEMAND_MODEL_NAME={artifacts['demand']}")
    if artifacts.get("anomaly"):
        print(f"SAGEMAKER_ANOMALY_MODEL_NAME={artifacts['anomaly']}")
    return artifacts


def main() -> None:
    p = argparse.ArgumentParser(description="Train DijkFood SageMaker models")
    p.add_argument("--bucket", default="")
    p.add_argument("--instance-type", default="ml.m5.large")
    p.add_argument("--deploy-delivery", action="store_true")
    p.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Do not delete prior SageMaker jobs / S3 artifacts before training",
    )
    p.add_argument("--all", action="store_true", default=True)
    args = p.parse_args()
    train_all(
        bucket=args.bucket.strip() or None,
        instance_type=args.instance_type,
        deploy_delivery=args.deploy_delivery,
        cleanup_previous=not args.no_cleanup,
    )


if __name__ == "__main__":
    main()
