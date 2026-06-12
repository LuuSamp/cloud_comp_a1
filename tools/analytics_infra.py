"""Analytics pipeline: DynamoDB Streams -> Lambda -> S3 datalake."""

from __future__ import annotations

import os
import tempfile
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from tools.state import DeploymentState

LAMBDA_NAME = "dijkfood-analytics-ingest"
_TOOLS_ROOT = Path(__file__).resolve().parent


def predictions_table_name(suffix: str) -> str:
    return f"dijkfood-predictions-{suffix}"


def create_predictions_table(ddb, suffix: str, state: DeploymentState) -> str:
    name = predictions_table_name(suffix)
    try:
        r = ddb.create_table(
            TableName=name,
            KeySchema=[
                {"AttributeName": "orderId", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "orderId", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        arn = r["TableDescription"]["TableArn"]
        ddb.get_waiter("table_exists").wait(TableName=name)
        print(f"  [DynamoDB] Predictions table {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
        d = ddb.describe_table(TableName=name)
        arn = d["Table"]["TableArn"]
        print(f"  [DynamoDB] Predictions table {name} exists")
    state.dynamo_predictions_table = name
    state.dynamo_predictions_arn = arn
    return name


def destroy_predictions_table(ddb, state: DeploymentState) -> None:
    name = state.dynamo_predictions_table
    if not name:
        return
    try:
        ddb.delete_table(TableName=name)
        print(f"  [teardown] Deleted DynamoDB table {name}")
    except ClientError as exc:
        print(f"  [teardown] DynamoDB {name}: {exc.response['Error']['Code']}")
    state.dynamo_predictions_table = None
    state.dynamo_predictions_arn = None


def _ensure_stream_enabled(ddb, table_name: str) -> str:
    desc = ddb.describe_table(TableName=table_name)["Table"]
    if not desc.get("StreamSpecification", {}).get("StreamEnabled"):
        ddb.update_table(
            TableName=table_name,
            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
        )
    for _ in range(20):
        desc = ddb.describe_table(TableName=table_name)["Table"]
        arn = desc.get("LatestStreamArn")
        if arn:
            return arn
        time.sleep(2)
    raise RuntimeError(f"Stream ARN did not appear for table {table_name}")


def _package_lambda() -> bytes:
    src = _TOOLS_ROOT / "lambda" / "ingest_lambda.py"
    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "ingest.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(src, arcname="ingest_lambda.py")
        return zip_path.read_bytes()


def _lambda_role_arn() -> str:
    role = (os.environ.get("TASK_ROLE_ARN") or os.environ.get("EXECUTION_ROLE_ARN") or "").strip()
    if not role:
        sts = __import__("boto3").client("sts")
        account = sts.get_caller_identity()["Account"]
        role = f"arn:aws:iam::{account}:role/LabRole"
    return role


def _deploy_ingest_lambda(
    lambda_client,
    *,
    role_arn: str,
    bucket_name: str,
    ordering_base_url: str,
) -> str:
    code = _package_lambda()
    env = {
        "Variables": {
            "DATALAKE_S3_BUCKET": bucket_name,
            "ORDERING_BASE_URL": ordering_base_url,
        }
    }

    def _wait_ready() -> None:
        for _ in range(20):
            try:
                cfg = lambda_client.get_function(FunctionName=LAMBDA_NAME)["Configuration"]
            except ClientError:
                return
            if (cfg.get("LastUpdateStatus") or "") not in {"InProgress"}:
                return
            time.sleep(3)

    try:
        lambda_client.get_function(FunctionName=LAMBDA_NAME)
        _wait_ready()
        lambda_client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=code)
        _wait_ready()
        lambda_client.update_function_configuration(
            FunctionName=LAMBDA_NAME, Role=role_arn, Environment=env, Timeout=60, MemorySize=256
        )
        _wait_ready()
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        lambda_client.create_function(
            FunctionName=LAMBDA_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="ingest_lambda.handler",
            Code={"ZipFile": code},
            Timeout=60,
            MemorySize=256,
            Environment=env,
        )
    return lambda_client.get_function(FunctionName=LAMBDA_NAME)["Configuration"]["FunctionArn"]


def _ensure_event_mapping(lambda_client, function_arn: str, stream_arn: str) -> None:
    resp = lambda_client.list_event_source_mappings(FunctionName=function_arn)
    for m in resp.get("EventSourceMappings", []):
        if m.get("EventSourceArn") == stream_arn:
            return
    lambda_client.create_event_source_mapping(
        EventSourceArn=stream_arn,
        FunctionName=function_arn,
        StartingPosition="TRIM_HORIZON",
        BatchSize=100,
    )


def enable_analytics_ingestion(
    *,
    ddb,
    lambda_client,
    state: DeploymentState,
    ordering_base_url: str,
) -> None:
    """Enable streams, deploy ingest Lambda, wire event source mappings."""
    if not state.datalake_s3_bucket:
        raise RuntimeError("datalake_s3_bucket required for analytics ingestion")
    if not state.dynamo_order_logs_table or not state.dynamo_courier_positions_table:
        raise RuntimeError("DynamoDB table names required for analytics ingestion")

    arn_logs = _ensure_stream_enabled(ddb, state.dynamo_order_logs_table)
    arn_pos = _ensure_stream_enabled(ddb, state.dynamo_courier_positions_table)

    role_arn = _lambda_role_arn()
    func_arn = _deploy_ingest_lambda(
        lambda_client,
        role_arn=role_arn,
        bucket_name=state.datalake_s3_bucket,
        ordering_base_url=ordering_base_url.rstrip("/"),
    )
    _ensure_event_mapping(lambda_client, func_arn, arn_logs)
    _ensure_event_mapping(lambda_client, func_arn, arn_pos)
    state.analytics_lambda_name = LAMBDA_NAME
    print(f"  [Analytics] Ingest Lambda {LAMBDA_NAME} -> s3://{state.datalake_s3_bucket}/events/")


def destroy_analytics_lambda(lambda_client, state: DeploymentState) -> None:
    name = state.analytics_lambda_name or LAMBDA_NAME
    try:
        mappings = lambda_client.list_event_source_mappings(FunctionName=name).get(
            "EventSourceMappings", []
        )
        for m in mappings:
            uuid = m.get("UUID")
            if uuid:
                lambda_client.delete_event_source_mapping(UUID=uuid)
        lambda_client.delete_function(FunctionName=name)
        print(f"  [teardown] Deleted Lambda {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            print(f"  [teardown] Lambda {name}: {exc.response['Error']['Code']}")
    state.analytics_lambda_name = None
