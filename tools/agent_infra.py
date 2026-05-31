"""
Agent service: DynamoDB conversation sessions and Bedrock IAM.
"""

from __future__ import annotations

import json
import os

from botocore.exceptions import ClientError

from tools.dynamodb_infra import TAG_PROJECT, _tag_table
from tools.state import DeploymentState

AGENT_SESSIONS_POLICY_NAME = "DijkFoodAgentSessionsAccess"
AGENT_BEDROCK_POLICY_NAME = "DijkFoodAgentBedrockAccess"


def create_agent_sessions_table(ddb, suffix: str, state: DeploymentState) -> str:
    table_name = f"dijkfood-agent-sessions-{suffix}"
    try:
        r = ddb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "conversationId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "conversationId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        arn = r["TableDescription"]["TableArn"]
        ddb.get_waiter("table_exists").wait(TableName=table_name)
        _tag_table(ddb, arn, suffix)
        print(f"  [DynamoDB] Table {table_name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
        d = ddb.describe_table(TableName=table_name)
        arn = d["Table"]["TableArn"]
        print(f"  [DynamoDB] Table {table_name} exists")
    state.dynamo_agent_sessions_table = table_name
    state.dynamo_agent_sessions_arn = arn
    return table_name


def destroy_agent_sessions_table(ddb, state: DeploymentState) -> None:
    name = state.dynamo_agent_sessions_table
    if not name:
        return
    try:
        ddb.delete_table(TableName=name)
        ddb.get_waiter("table_not_exists").wait(TableName=name)
        print(f"  [teardown] Deleted DynamoDB table {name}")
    except ClientError as exc:
        print(f"  [teardown] DynamoDB {name}: {exc.response['Error']['Code']}")
    state.dynamo_agent_sessions_table = None
    state.dynamo_agent_sessions_arn = None


def attach_agent_sessions_policy(iam, task_role_name: str, sessions_table_arn: str) -> None:
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:DescribeTable",
                ],
                "Resource": [sessions_table_arn, f"{sessions_table_arn}/index/*"],
            }
        ],
    }
    iam.put_role_policy(
        RoleName=task_role_name,
        PolicyName=AGENT_SESSIONS_POLICY_NAME,
        PolicyDocument=json.dumps(doc),
    )
    print(f"  [IAM] Attached {AGENT_SESSIONS_POLICY_NAME} to {task_role_name}")


def attach_agent_bedrock_policy(
    iam,
    task_role_name: str,
    *,
    region: str,
    account_id: str,
    model_id: str,
) -> None:
    model_arn = f"arn:aws:bedrock:{region}::foundation-model/{model_id}"
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:Converse",
                ],
                "Resource": [model_arn],
            }
        ],
    }
    iam.put_role_policy(
        RoleName=task_role_name,
        PolicyName=AGENT_BEDROCK_POLICY_NAME,
        PolicyDocument=json.dumps(doc),
    )
    print(f"  [IAM] Attached {AGENT_BEDROCK_POLICY_NAME} ({model_id}) to {task_role_name}")


def default_bedrock_model_id() -> str:
    return (os.environ.get("BEDROCK_MODEL_ID") or "amazon.nova-lite-v1:0").strip()
