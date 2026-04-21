"""
DynamoDB: order logs and courier positions (ERD composite keys).
"""

from __future__ import annotations

import json

from botocore.exceptions import ClientError

from tools.state import DeploymentState

TAG_PROJECT = "dijkfood-a1"
DYNAMO_TASK_POLICY_NAME = "DijkFoodDynamoAccess"


def _tag_table(ddb, table_arn: str, suffix: str) -> None:
    try:
        ddb.tag_resource(
            ResourceArn=table_arn,
            Tags=[
                {"Key": "Project", "Value": TAG_PROJECT},
                {"Key": "DeploymentId", "Value": suffix},
            ],
        )
    except ClientError:
        pass


def create_dynamodb_tables(ddb, suffix: str, state: DeploymentState) -> tuple[str, str, str]:
    order_logs = f"dijkfood-order-logs-{suffix}"
    courier_pos = f"dijkfood-courier-positions-{suffix}"
    routes = f"dijkfood-routes-{suffix}"

    try:
        r1 = ddb.create_table(
            TableName=order_logs,
            KeySchema=[
                {"AttributeName": "orderId", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "orderId", "AttributeType": "N"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        arn1 = r1["TableDescription"]["TableArn"]
        ddb.get_waiter("table_exists").wait(TableName=order_logs)
        _tag_table(ddb, arn1, suffix)
        print(f"  [DynamoDB] Table {order_logs}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
        d = ddb.describe_table(TableName=order_logs)
        arn1 = d["Table"]["TableArn"]
        print(
            f"  [DynamoDB] Table {order_logs} exists "
            "(if schema changed vs. orderId+timestamp PK, delete the table or use a new suffix)"
        )

    try:
        r2 = ddb.create_table(
            TableName=courier_pos,
            KeySchema=[
                {"AttributeName": "courierId", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "courierId", "AttributeType": "N"},
                {"AttributeName": "timestamp", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        arn2 = r2["TableDescription"]["TableArn"]
        ddb.get_waiter("table_exists").wait(TableName=courier_pos)
        _tag_table(ddb, arn2, suffix)
        print(f"  [DynamoDB] Table {courier_pos}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
        d = ddb.describe_table(TableName=courier_pos)
        arn2 = d["Table"]["TableArn"]
        print(
            f"  [DynamoDB] Table {courier_pos} exists "
            "(if schema changed, delete the table or use a new suffix)"
        )

    try:
        r3 = ddb.create_table(
            TableName=routes,
            KeySchema=[
                {"AttributeName": "routeKey", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "routeKey", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        arn3 = r3["TableDescription"]["TableArn"]
        ddb.get_waiter("table_exists").wait(TableName=routes)
        _tag_table(ddb, arn3, suffix)
        print(f"  [DynamoDB] Table {routes}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
        d = ddb.describe_table(TableName=routes)
        arn3 = d["Table"]["TableArn"]
        print(
            f"  [DynamoDB] Table {routes} exists "
            "(if schema changed, delete the table or use a new suffix)"
        )

    state.dynamo_order_logs_table = order_logs
    state.dynamo_courier_positions_table = courier_pos
    state.dynamo_order_logs_arn = arn1
    state.dynamo_courier_positions_arn = arn2
    state.dynamo_routes_table = routes
    state.dynamo_routes_arn = arn3
    return order_logs, courier_pos, routes


def attach_dynamo_policy_to_task_role(
    iam, task_role_name: str, order_logs_arn: str, courier_positions_arn: str, routes_arn: str
) -> None:
    resources = [
        order_logs_arn,
        f"{order_logs_arn}/index/*",
        courier_positions_arn,
        routes_arn,
        f"{routes_arn}/index/*",
    ]
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:BatchWriteItem",
                    "dynamodb:BatchGetItem",
                    "dynamodb:DescribeTable",
                ],
                "Resource": resources,
            }
        ],
    }
    iam.put_role_policy(
        RoleName=task_role_name,
        PolicyName=DYNAMO_TASK_POLICY_NAME,
        PolicyDocument=json.dumps(doc),
    )
    print(f"  [IAM] Attached {DYNAMO_TASK_POLICY_NAME} to {task_role_name}")


def destroy_dynamodb_tables(ddb, state: DeploymentState) -> None:
    for attr in ("dynamo_order_logs_table", "dynamo_courier_positions_table", "dynamo_routes_table"):
        name = getattr(state, attr)
        if not name:
            continue
        try:
            ddb.delete_table(TableName=name)
            ddb.get_waiter("table_not_exists").wait(TableName=name)
            print(f"  [teardown] Deleted DynamoDB table {name}")
        except ClientError as exc:
            print(f"  [teardown] DynamoDB {name}: {exc.response['Error']['Code']}")
        setattr(state, attr, None)
    state.dynamo_order_logs_arn = None
    state.dynamo_courier_positions_arn = None
    state.dynamo_routes_arn = None
