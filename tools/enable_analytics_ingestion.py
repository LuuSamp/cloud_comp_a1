"""Enable analytics ingestion on deployed DynamoDB tables.

This helper:
- loads connection.env to find `DYNAMO_*` table names and `DATALAKE_S3_BUCKET`
- enables DynamoDB Streams on the tables (NEW_AND_OLD_IMAGES)
- creates an IAM role for Lambda (if missing)
- packages and deploys a Lambda to write stream records to S3
- creates EventSourceMapping from the table stream -> Lambda

Run this after a successful `deploy.py --skip-teardown` and with AWS creds configured.
"""
from __future__ import annotations

import json
import os
import tempfile
import zipfile
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent
LAMBDA_NAME = "dijkfood-analytics-ingest"
LAMBDA_ROLE_NAME = "LabRole"


def _load_connection_env():
    load_dotenv(Path.cwd() / ".env", override=True)
    if (Path.cwd() / "connection.env").is_file():
        load_dotenv(Path.cwd() / "connection.env", override=False)


def _resolved_env() -> dict[str, str]:
    """Read config from files in a deterministic order.

    Process environment variables are intentionally ignored here so a stale
    exported DATALAKE_S3_BUCKET / BASE_URL does not override the current
    deployment snapshot.
    """

    values: dict[str, str] = {}
    values.update({k: v for k, v in dotenv_values(Path.cwd() / ".env").items() if v})
    connection_path = Path.cwd() / "connection.env"
    if connection_path.is_file():
        values.update({k: v for k, v in dotenv_values(connection_path).items() if v})
    return values


def _aws_session(region: str) -> boto3.Session:
    access_key = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip() or None
    secret_key = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip() or None
    session_token = (os.environ.get("AWS_SESSION_TOKEN") or "").strip() or None
    kwargs = {"region_name": region}
    if access_key and secret_key and session_token:
        kwargs.update(
            {
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
                "aws_session_token": session_token,
            }
        )
    return boto3.Session(**kwargs)


def _discover_table_name(ddb, preferred: str | None, prefix: str) -> str | None:
    """Return an existing table name, falling back to prefix matching if the preferred name is stale."""
    try:
        existing = set(ddb.list_tables().get("TableNames", []))
    except ClientError as e:
        print(f"Unable to list DynamoDB tables: {e}")
        return preferred if preferred in (None, "") else None

    if preferred and preferred in existing:
        return preferred

    matches = sorted(name for name in existing if name.startswith(prefix))
    if not matches:
        return preferred if preferred in existing else None
    chosen = matches[-1]
    if preferred and preferred != chosen:
        print(f"  table {preferred!r} not found; using discovered {chosen!r}")
    else:
        print(f"  discovered {chosen!r}")
    return chosen


def _ensure_stream_enabled(ddb, table_name: str):
    print(f"Enabling streams on {table_name}...")
    try:
        desc = ddb.describe_table(TableName=table_name)["Table"]
        if desc.get("StreamSpecification", {}).get("StreamEnabled"):
            print("  streams already enabled")
            return desc.get("LatestStreamArn")
    except ClientError as e:
        print("  describe_table failed:", e)
        raise
    # enable stream
    ddb.update_table(TableName=table_name, StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"})
    # wait until stream ARN appears
    for _ in range(20):
        desc = ddb.describe_table(TableName=table_name)["Table"]
        arn = desc.get("LatestStreamArn")
        if arn:
            return arn
        time.sleep(2)
    raise RuntimeError("Stream ARN did not appear for table %s" % table_name)


def _create_lambda_role(iam, bucket_arn: str) -> str:
    assume = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        r = iam.get_role(RoleName=LAMBDA_ROLE_NAME)
        arn = r["Role"]["Arn"]
        print(f"Using existing role {LAMBDA_ROLE_NAME}")
    except ClientError:
        print(f"Creating role {LAMBDA_ROLE_NAME}...")
        r = iam.create_role(RoleName=LAMBDA_ROLE_NAME, AssumeRolePolicyDocument=json.dumps(assume))
        arn = r["Role"]["Arn"]
    # attach managed policy for logging when permitted
    try:
        iam.attach_role_policy(
            RoleName=LAMBDA_ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
    except ClientError as e:
        print(f"  warning: could not attach AWSLambdaBasicExecutionRole to {LAMBDA_ROLE_NAME}: {e.response['Error']['Code']}")
    # add inline policy for S3 put when permitted
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:PutObjectAcl"],
                "Resource": [f"{bucket_arn}/*"],
            }
        ],
    }
    try:
        iam.put_role_policy(
            RoleName=LAMBDA_ROLE_NAME,
            PolicyName="DijkFoodAnalyticsS3",
            PolicyDocument=json.dumps(policy),
        )
    except ClientError as e:
        print(f"  warning: could not add inline S3 policy to {LAMBDA_ROLE_NAME}: {e.response['Error']['Code']}")
    return arn


def _package_lambda() -> bytes:
    # zip the ingest_lambda.py
    src = ROOT / "lambda" / "ingest_lambda.py"
    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "ingest.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(src, arcname="ingest_lambda.py")
        return zip_path.read_bytes()


def _deploy_lambda(lambda_client, role_arn, bucket_name, ordering_base_url):
    code = _package_lambda()
    env = {
        "Variables": {
            "DATALAKE_S3_BUCKET": bucket_name,
            "ORDERING_BASE_URL": ordering_base_url,
        }
    }

    def _wait_until_ready() -> None:
        for attempt in range(20):
            try:
                cfg = lambda_client.get_function(FunctionName=LAMBDA_NAME)["Configuration"]
            except ClientError:
                return
            state = (cfg.get("State") or "").strip()
            last_status = (cfg.get("LastUpdateStatus") or "").strip()
            if state in {"Active", "Pending"} and last_status not in {"InProgress"}:
                return
            print("  waiting for lambda to become ready...")
            time.sleep(5)
        raise RuntimeError(f"Lambda {LAMBDA_NAME} did not become ready in time")

    def _update_with_retry() -> str:
        for attempt in range(10):
            try:
                _wait_until_ready()
                lambda_client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=code)
                _wait_until_ready()
                lambda_client.update_function_configuration(
                    FunctionName=LAMBDA_NAME,
                    Role=role_arn,
                    Environment=env,
                )
                _wait_until_ready()
                return lambda_client.get_function(FunctionName=LAMBDA_NAME)["Configuration"]["FunctionArn"]
            except ClientError as exc:
                code_name = exc.response["Error"].get("Code")
                if code_name != "ResourceConflictException" or attempt == 9:
                    raise
                print("  lambda update in progress; retrying...")
                time.sleep(3)
        raise RuntimeError("Lambda update did not complete")

    try:
        existing = lambda_client.get_function(FunctionName=LAMBDA_NAME)
        print(f"Lambda {LAMBDA_NAME} exists, updating code and configuration...")
        return _update_with_retry()
    except ClientError as exc:
        if exc.response["Error"].get("Code") not in {"ResourceNotFoundException"}:
            print(f"Lambda get/update path failed ({exc.response['Error'].get('Code')}), trying create-or-update fallback...")
    try:
        print(f"Creating lambda {LAMBDA_NAME}...")
        resp = lambda_client.create_function(
            FunctionName=LAMBDA_NAME,
            Runtime="python3.9",
            Role=role_arn,
            Handler="ingest_lambda.handler",
            Code={"ZipFile": code},
            Timeout=60,
            MemorySize=256,
            Environment=env,
        )
        return resp["FunctionArn"]
    except ClientError as exc:
        if exc.response["Error"].get("Code") != "ResourceConflictException":
            raise
        print(f"Lambda {LAMBDA_NAME} already exists; updating instead of creating...")
        return _update_with_retry()


def _create_event_mapping(lambda_client, function_arn, stream_arn):
    # check existing mappings
    resp = lambda_client.list_event_source_mappings(FunctionName=function_arn)
    for m in resp.get("EventSourceMappings", []):
        if m.get("EventSourceArn") == stream_arn:
            print("Event source mapping already exists")
            return m.get("UUID")
    resp = lambda_client.create_event_source_mapping(EventSourceArn=stream_arn, FunctionName=function_arn, StartingPosition="TRIM_HORIZON", BatchSize=100)
    return resp.get("UUID")


def main():
    _load_connection_env()
    env = _resolved_env()
    region = (
        env.get("AWS_REGION")
        or env.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    ).strip()
    table_logs = env.get("DYNAMO_ORDER_LOGS_TABLE") or env.get("DYNAMODB_ORDER_LOGS_TABLE")
    table_pos = env.get("DYNAMO_COURIER_POSITIONS_TABLE") or env.get("DYNAMODB_COURIER_POSITIONS_TABLE")
    bucket = env.get("DATALAKE_S3_BUCKET") or env.get("ROUTING_GRAPH_S3_BUCKET")
    ordering_base_url = (env.get("ORDERING_BASE_URL") or env.get("BASE_URL") or "").strip().rstrip("/")
    if not table_logs or not table_pos:
        print("connection.env must include DYNAMO/DYNAMODB table names (DYNAMO_ORDER_LOGS_TABLE / DYNAMO_COURIER_POSITIONS_TABLE)")
        return
    if not bucket:
        print("Set DATALAKE_S3_BUCKET (or ROUTING_GRAPH_S3_BUCKET) in connection.env before running")
        return
    if not ordering_base_url:
        print("Set BASE_URL (or ORDERING_BASE_URL) so the Lambda can enrich order events with restaurant IDs")
        return

    session = _aws_session(region)
    sts = session.client("sts")
    print("AWS identity:", sts.get_caller_identity().get("Arn"))
    ddb = session.client("dynamodb")
    iam = session.client("iam")
    lambda_client = session.client("lambda")
    s3 = session.client("s3")

    # discover live table names in case connection.env is stale
    table_logs = _discover_table_name(ddb, table_logs, "dijkfood-order-logs-")
    table_pos = _discover_table_name(ddb, table_pos, "dijkfood-courier-positions-")
    if not table_logs or not table_pos:
        raise RuntimeError(
            "Could not resolve DynamoDB table names from account. "
            "Run deploy.py --skip-teardown to refresh connection.env or check the current AWS account."
        )

    # ensure bucket exists
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        print(f"S3 bucket {bucket} not found; creating...")
        s3.create_bucket(Bucket=bucket)

    # enable streams
    arn_logs = _ensure_stream_enabled(ddb, table_logs)
    arn_pos = _ensure_stream_enabled(ddb, table_pos)

    # create role
    bucket_arn = f"arn:aws:s3:::{bucket}"
    role_arn = _create_lambda_role(iam, bucket_arn)

    # deploy lambda
    func_arn = _deploy_lambda(lambda_client, role_arn, bucket, ordering_base_url)

    # create mappings
    _create_event_mapping(lambda_client, func_arn, arn_logs)
    _create_event_mapping(lambda_client, func_arn, arn_pos)

    print("Analytics ingestion enabled: Lambda deployed and EventSourceMappings created.")


if __name__ == "__main__":
    main()
