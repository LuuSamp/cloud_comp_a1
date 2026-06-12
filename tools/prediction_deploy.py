"""ECS spec and task environment for the prediction gateway service."""

from __future__ import annotations

import os
from pathlib import Path

PREDICTION_SERVICE_ID = "prediction"


def prediction_service_spec(project_root: Path) -> dict[str, object]:
    return {
        "id": PREDICTION_SERVICE_ID,
        "ecr_suffix": "prediction",
        "docker_context": project_root,
        "dockerfile": project_root / "services" / "prediction" / "Dockerfile",
        "path_pattern": "/prediction*",
        "cpu": "256",
        "memory": "512",
        "desired_count": 1,
        "full_stack_env": False,
        "autoscaling": {
            "enabled": True,
            "min_capacity": 1,
            "max_capacity": 4,
            "cpu_target": 60.0,
            "memory_target": 60.0,
            "scale_in_cooldown": 120,
            "scale_out_cooldown": 45,
        },
    }


def build_prediction_task_environment(
    *,
    region: str,
    datalake_bucket: str,
    predictions_table: str,
    delivery_endpoint: str | None = None,
) -> list[dict[str, str]]:
    env: list[dict[str, str]] = [
        {"name": "AWS_REGION", "value": region},
        {"name": "AWS_DEFAULT_REGION", "value": region},
        {"name": "SERVICE_ID", "value": PREDICTION_SERVICE_ID},
        {"name": "DATALAKE_S3_BUCKET", "value": datalake_bucket},
        {"name": "DYNAMODB_PREDICTIONS_TABLE", "value": predictions_table},
    ]
    ep = (delivery_endpoint or os.environ.get("SAGEMAKER_DELIVERY_ENDPOINT") or "").strip()
    if ep:
        env.append({"name": "SAGEMAKER_DELIVERY_ENDPOINT", "value": ep})
    fallback = (os.environ.get("PREDICTION_FALLBACK_SECONDS") or "").strip()
    if fallback:
        env.append({"name": "PREDICTION_FALLBACK_SECONDS", "value": fallback})
    return env
