"""AWS Glue Data Catalog for analytics events in the datalake."""

from __future__ import annotations

import time

from botocore.exceptions import ClientError

from tools.state import DeploymentState

TAG_PROJECT = "dijkfood-a1"


def glue_database_name(suffix: str) -> str:
    return f"dijkfood_analytics_{suffix}".lower()


def glue_crawler_name(suffix: str) -> str:
    return f"dijkfood-events-crawler-{suffix}".lower()


def ensure_events_table(glue, *, db_name: str, datalake_bucket: str) -> str:
    """Create the Athena/Glue `events` table if the crawler has not registered one yet."""
    table_name = "events"
    s3_target = f"s3://{datalake_bucket}/events/"
    try:
        glue.get_table(DatabaseName=db_name, Name=table_name)
        return table_name
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityNotFoundException":
            raise

    glue.create_table(
        DatabaseName=db_name,
        TableInput={
            "Name": table_name,
            "Description": "Operational events (order status, courier positions) from datalake JSONL",
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {
                "classification": "json",
                "projection.enabled": "true",
                "projection.year.type": "integer",
                "projection.year.range": "2024,2035",
                "projection.month.type": "integer",
                "projection.month.range": "1,12",
                "projection.month.digits": "2",
                "projection.day.type": "integer",
                "projection.day.range": "1,31",
                "projection.day.digits": "2",
                "storage.location.template": f"s3://{datalake_bucket}/events/${{year}}/${{month}}/${{day}}",
            },
            "PartitionKeys": [
                {"Name": "year", "Type": "string"},
                {"Name": "month", "Type": "string"},
                {"Name": "day", "Type": "string"},
            ],
            "StorageDescriptor": {
                "Location": s3_target,
                "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                "SerdeInfo": {
                    "SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe",
                    "Parameters": {"ignore.malformed.json": "true"},
                },
                "Columns": [
                    {"Name": "type", "Type": "string"},
                    {"Name": "order_id", "Type": "bigint"},
                    {"Name": "status_id", "Type": "int"},
                    {"Name": "timestamp", "Type": "string"},
                    {"Name": "detail", "Type": "string"},
                    {"Name": "food_place_id", "Type": "bigint"},
                    {"Name": "customer_id", "Type": "bigint"},
                    {"Name": "courier_id", "Type": "bigint"},
                    {"Name": "lat", "Type": "double"},
                    {"Name": "lon", "Type": "double"},
                ],
            },
        },
    )
    print(f"  [Glue] Table {db_name}.{table_name}")
    return table_name


def create_glue_catalog(
    glue,
    *,
    suffix: str,
    datalake_bucket: str,
    state: DeploymentState,
) -> tuple[str, str]:
    db_name = glue_database_name(suffix)
    crawler = glue_crawler_name(suffix)
    s3_target = f"s3://{datalake_bucket}/events/"

    try:
        glue.create_database(
            DatabaseInput={
                "Name": db_name,
                "Description": "DijkFood operational events from DynamoDB streams",
            }
        )
        print(f"  [Glue] Database {db_name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "AlreadyExistsException":
            raise
        print(f"  [Glue] Database {db_name} exists")

    ensure_events_table(glue, db_name=db_name, datalake_bucket=datalake_bucket)

    try:
        glue.create_crawler(
            Name=crawler,
            Role=os_glue_crawler_role_arn(glue),
            DatabaseName=db_name,
            Targets={"S3Targets": [{"Path": s3_target}]},
            SchemaChangePolicy={
                "UpdateBehavior": "UPDATE_IN_DATABASE",
                "DeleteBehavior": "LOG",
            },
            Configuration='{"Version":1.0,"Grouping":{"TableGroupingPolicy":"CombineCompatibleSchemas"}}',
        )
        print(f"  [Glue] Crawler {crawler}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "AlreadyExistsException":
            raise
        print(f"  [Glue] Crawler {crawler} exists")

    state.glue_database = db_name
    state.glue_crawler_name = crawler
    return db_name, crawler


def os_glue_crawler_role_arn(glue) -> str:
    """Learner Lab: Glue crawler uses the service-linked role or LabRole from env."""
    import os

    explicit = (os.environ.get("TASK_ROLE_ARN") or os.environ.get("GLUE_CRAWLER_ROLE_ARN") or "").strip()
    if explicit:
        return explicit
    # Fallback: common lab role name pattern (caller must set TASK_ROLE_ARN in practice)
    sts = __import__("boto3").client("sts")
    account = sts.get_caller_identity()["Account"]
    return f"arn:aws:iam::{account}:role/LabRole"


def run_glue_crawler(glue, crawler_name: str, *, wait: bool = True) -> None:
    try:
        glue.start_crawler(Name=crawler_name)
        print(f"  [Glue] Started crawler {crawler_name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "CrawlerRunningException":
            print(f"  [Glue] Crawler {crawler_name} already running")
        else:
            raise
    if not wait:
        return
    for _ in range(60):
        resp = glue.get_crawler(Name=crawler_name)["Crawler"]
        state = resp.get("State", "")
        if state == "READY":
            print(f"  [Glue] Crawler {crawler_name} ready")
            return
        if state == "STOPPING":
            time.sleep(3)
            continue
        time.sleep(5)
    print(f"  [Glue] WARNING: crawler {crawler_name} did not reach READY in time")


def destroy_glue_catalog(glue, state: DeploymentState) -> None:
    crawler = state.glue_crawler_name
    db_name = state.glue_database
    if crawler:
        try:
            glue.delete_crawler(Name=crawler)
            print(f"  [teardown] Deleted Glue crawler {crawler}")
        except ClientError as exc:
            print(f"  [teardown] Glue crawler {crawler}: {exc.response['Error']['Code']}")
    if db_name:
        try:
            tables = glue.get_tables(DatabaseName=db_name).get("TableList", [])
            for t in tables:
                glue.delete_table(DatabaseName=db_name, Name=t["Name"])
            glue.delete_database(Name=db_name)
            print(f"  [teardown] Deleted Glue database {db_name}")
        except ClientError as exc:
            print(f"  [teardown] Glue database {db_name}: {exc.response['Error']['Code']}")
    state.glue_database = None
    state.glue_crawler_name = None
