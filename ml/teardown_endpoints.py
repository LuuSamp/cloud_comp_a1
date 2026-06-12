"""Delete SageMaker delivery endpoint to save Learner Lab budget."""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from dotenv import load_dotenv

from tools.connection_env import load_connection_env
from tools.sagemaker_infra import destroy_sagemaker_endpoints

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    if (ROOT / "connection.env").is_file():
        state, region, _ = load_connection_env()
    else:
        from tools.state import DeploymentState

        state = DeploymentState(suffix="local")
        state.sagemaker_delivery_endpoint = os.environ.get("SAGEMAKER_DELIVERY_ENDPOINT")
        region = os.environ.get("AWS_REGION", "us-east-1")
    sm = boto3.client("sagemaker", region_name=region)
    destroy_sagemaker_endpoints(sm, state)
    print("Done.")


if __name__ == "__main__":
    main()
