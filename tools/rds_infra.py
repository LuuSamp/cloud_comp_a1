"""
RDS PostgreSQL: security group, subnet group, instance, schema bootstrap.
"""

from __future__ import annotations

import time

import psycopg
from botocore.exceptions import ClientError

from tools.state import DeploymentState

TAG_PROJECT = "dijkfood-a1"
DB_PORT = 5432
PG_VERSION = "16"
INSTANCE_CLASS = "db.t3.micro"


# Execute in order. Enum types use DO blocks (semicolons inside); do not split this as one string.
SCHEMA_STEPS = [
    """DO $e$ BEGIN
        CREATE TYPE kitchen_type_t AS ENUM ('UNSPECIFIED', 'OTHER');
    EXCEPTION WHEN duplicate_object THEN NULL; END $e$;""",
    """DO $e$ BEGIN
        CREATE TYPE vehicle_type_t AS ENUM ('UNSPECIFIED', 'OTHER');
    EXCEPTION WHEN duplicate_object THEN NULL; END $e$;""",
    """DO $e$ BEGIN
        CREATE TYPE courier_status_t AS ENUM (
            'offline',
            'available',
            'in_route_to_restaurant',
            'waiting_for_order',
            'in_route_to_customer',
            'waiting_for_customer'
        );
    EXCEPTION WHEN duplicate_object THEN NULL; END $e$;""",
    """
CREATE TABLE IF NOT EXISTS order_statuses (
    order_status_id INT PRIMARY KEY,
    status          VARCHAR(64) NOT NULL UNIQUE
)
    """,
    """
INSERT INTO order_statuses (order_status_id, status) VALUES
    (1, 'CONFIRMED'),
    (2, 'PREPARING'),
    (3, 'READY_FOR_PICKUP'),
    (4, 'PICKED_UP'),
    (5, 'IN_TRANSIT'),
    (6, 'DELIVERED')
ON CONFLICT (order_status_id) DO NOTHING
    """,
    """
CREATE TABLE IF NOT EXISTS customers (
    customer_id   SERIAL PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    email         VARCHAR(255) NOT NULL UNIQUE,
    phone         VARCHAR(64)  NOT NULL,
    address       TEXT           NOT NULL,
    lat           DOUBLE PRECISION NOT NULL,
    lon           DOUBLE PRECISION NOT NULL
)
    """,
    """
CREATE TABLE IF NOT EXISTS food_places (
    food_place_id SERIAL PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    kitchen_type  kitchen_type_t NOT NULL DEFAULT 'UNSPECIFIED',
    address       TEXT           NOT NULL,
    lat           DOUBLE PRECISION NOT NULL,
    lon           DOUBLE PRECISION NOT NULL
)
    """,
    """
CREATE TABLE IF NOT EXISTS couriers (
    courier_id       SERIAL PRIMARY KEY,
    name             VARCHAR(255) NOT NULL,
    vehicle_type     vehicle_type_t NOT NULL DEFAULT 'UNSPECIFIED',
    initial_address  TEXT         NOT NULL,
    status           courier_status_t NOT NULL DEFAULT 'offline',
    last_position    TEXT,
    initial_lat      DOUBLE PRECISION NOT NULL,
    initial_lon      DOUBLE PRECISION NOT NULL
)
    """,
    """
CREATE TABLE IF NOT EXISTS orders (
    order_id        SERIAL PRIMARY KEY,
    order_status_id INT NOT NULL DEFAULT 1 REFERENCES order_statuses(order_status_id),
    customer_id     INT NOT NULL REFERENCES customers(customer_id),
    food_place_id   INT NOT NULL REFERENCES food_places(food_place_id),
    courier_id      INT NOT NULL REFERENCES couriers(courier_id)
)
    """,
]

# Idempotent upgrades for databases created with older SCHEMA_STEPS (lng, varchar status, etc.).
SCHEMA_MIGRATIONS = [
    """DO $m$ BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'customers'
              AND column_name = 'lng'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'customers'
              AND column_name = 'lon'
        ) THEN
            ALTER TABLE customers RENAME COLUMN lng TO lon;
        END IF;
    END $m$;""",
    """UPDATE customers SET
            lat = COALESCE(lat, -23.5505),
            lon = COALESCE(lon, -46.6333)
        WHERE lat IS NULL OR lon IS NULL;""",
    """DO $m$ BEGIN
        ALTER TABLE customers ALTER COLUMN lat SET NOT NULL;
    EXCEPTION WHEN others THEN NULL; END $m$;""",
    """DO $m$ BEGIN
        ALTER TABLE customers ALTER COLUMN lon SET NOT NULL;
    EXCEPTION WHEN others THEN NULL; END $m$;""",
    """DO $m$ BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'food_places'
              AND column_name = 'lng'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'food_places'
              AND column_name = 'lon'
        ) THEN
            ALTER TABLE food_places RENAME COLUMN lng TO lon;
        END IF;
    END $m$;""",
    """UPDATE food_places SET
            lat = COALESCE(lat, -23.5505),
            lon = COALESCE(lon, -46.6333)
        WHERE lat IS NULL OR lon IS NULL;""",
    """DO $m$ BEGIN
        ALTER TABLE food_places ALTER COLUMN lat SET NOT NULL;
    EXCEPTION WHEN others THEN NULL; END $m$;""",
    """DO $m$ BEGIN
        ALTER TABLE food_places ALTER COLUMN lon SET NOT NULL;
    EXCEPTION WHEN others THEN NULL; END $m$;""",
    """DO $m$ BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'couriers'
              AND column_name = 'lat'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'couriers'
              AND column_name = 'initial_lat'
        ) THEN
            ALTER TABLE couriers RENAME COLUMN lat TO initial_lat;
            ALTER TABLE couriers RENAME COLUMN lng TO initial_lon;
        END IF;
    END $m$;""",
    """UPDATE couriers SET
            initial_lat = COALESCE(initial_lat, -23.5505),
            initial_lon = COALESCE(initial_lon, -46.6333)
        WHERE initial_lat IS NULL OR initial_lon IS NULL;""",
    """DO $m$ BEGIN
        ALTER TABLE couriers ALTER COLUMN initial_lat SET NOT NULL;
    EXCEPTION WHEN others THEN NULL; END $m$;""",
    """DO $m$ BEGIN
        ALTER TABLE couriers ALTER COLUMN initial_lon SET NOT NULL;
    EXCEPTION WHEN others THEN NULL; END $m$;""",
    """DO $m$ BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'orders'
              AND column_name = 'status'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'orders'
              AND column_name = 'order_status_id'
        ) THEN
            ALTER TABLE orders ADD COLUMN order_status_id INT
                REFERENCES order_statuses(order_status_id);
            UPDATE orders SET order_status_id = CASE TRIM(status)
                WHEN 'CONFIRMED' THEN 1
                WHEN 'PREPARING' THEN 2
                WHEN 'READY_FOR_PICKUP' THEN 3
                WHEN 'PICKED_UP' THEN 4
                WHEN 'IN_TRANSIT' THEN 5
                WHEN 'DELIVERED' THEN 6
                ELSE 1 END;
            ALTER TABLE orders ALTER COLUMN order_status_id SET NOT NULL;
            ALTER TABLE orders DROP COLUMN status;
        END IF;
    END $m$;""",
]


def _tags(suffix: str) -> list[dict[str, str]]:
    return [
        {"Key": "Project", "Value": TAG_PROJECT},
        {"Key": "DeploymentId", "Value": suffix},
    ]


def get_default_vpc_id(ec2) -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if not vpcs["Vpcs"]:
        raise RuntimeError("No default VPC found in this region.")
    return vpcs["Vpcs"][0]["VpcId"]


def get_default_subnet_ids(ec2, vpc_id: str) -> list[str]:
    subs = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )
    if len(subs["Subnets"]) < 2:
        raise RuntimeError("Default VPC needs at least two subnets for RDS subnet group.")
    # Prefer subnets with a route to an Internet Gateway (public) for publicly accessible RDS
    return [s["SubnetId"] for s in subs["Subnets"][:2]]


def create_rds_security_group(ec2, vpc_id: str, suffix: str, state: DeploymentState) -> str:
    name = f"dijkfood-rds-sg-{suffix}"
    try:
        resp = ec2.create_security_group(
            GroupName=name,
            Description="DijkFood RDS PostgreSQL",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": _tags(suffix),
                }
            ],
        )
        sg_id = resp["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": DB_PORT,
                    "ToPort": DB_PORT,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "demo migrate + ECS via SG rule below"}],
                }
            ],
        )
        print(f"  [RDS SG] Created {sg_id}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise
        existing = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}]
        )
        sg_id = existing["SecurityGroups"][0]["GroupId"]
        print(f"  [RDS SG] Reusing {sg_id}")
    state.rds_sg_id = sg_id
    state.note_sg(sg_id)
    return sg_id


def allow_rds_from_ecs_tasks(ec2, rds_sg_id: str, ecs_task_sg_id: str) -> None:
    try:
        ec2.authorize_security_group_ingress(
            GroupId=rds_sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": DB_PORT,
                    "ToPort": DB_PORT,
                    "UserIdGroupPairs": [{"GroupId": ecs_task_sg_id, "Description": "ECS tasks"}],
                }
            ],
        )
        print(f"  [RDS SG] Allowed PostgreSQL from ECS task SG {ecs_task_sg_id}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "InvalidPermission.Duplicate":
            print("  [RDS SG] ECS ingress rule already present")
        else:
            raise


def create_db_subnet_group(rds, subnet_ids: list[str], suffix: str, state: DeploymentState) -> str:
    name = f"dijkfood-db-subnet-{suffix}"
    try:
        rds.create_db_subnet_group(
            DBSubnetGroupName=name,
            DBSubnetGroupDescription="DijkFood",
            SubnetIds=subnet_ids,
            Tags=_tags(suffix),
        )
        print(f"  [RDS] Subnet group {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "DBSubnetGroupAlreadyExists":
            raise
        print(f"  [RDS] Subnet group {name} exists")
    state.db_subnet_group = name
    return name


def create_rds_instance(
    rds,
    *,
    instance_id: str,
    subnet_group: str,
    sg_id: str,
    db_name: str,
    master_user: str,
    master_password: str,
    suffix: str,
    state: DeploymentState,
) -> str:
    print(f"  [RDS] Creating instance {instance_id} ...")
    try:
        rds.create_db_instance(
            DBInstanceIdentifier=instance_id,
            DBInstanceClass=INSTANCE_CLASS,
            Engine="postgres",
            EngineVersion=PG_VERSION,
            MasterUsername=master_user,
            MasterUserPassword=master_password,
            DBName=db_name,
            AllocatedStorage=20,
            StorageType="gp2",
            VpcSecurityGroupIds=[sg_id],
            DBSubnetGroupName=subnet_group,
            PubliclyAccessible=True,
            BackupRetentionPeriod=0,
            MultiAZ=False,
            AutoMinorVersionUpgrade=True,
            Tags=_tags(suffix),
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "DBInstanceAlreadyExists":
            raise
        print("  [RDS] Instance already exists")
    waiter = rds.get_waiter("db_instance_available")
    waiter.wait(
        DBInstanceIdentifier=instance_id,
        WaiterConfig={"Delay": 30, "MaxAttempts": 60},
    )
    info = rds.describe_db_instances(DBInstanceIdentifier=instance_id)
    inst = info["DBInstances"][0]
    endpoint = inst["Endpoint"]["Address"]
    print(f"  [RDS] Available at {endpoint}")
    state.rds_instance_id = instance_id
    return endpoint


def run_schema_bootstrap(
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
) -> None:
    conn_str = (
        f"host={host} port={port} dbname={dbname} user={user} password={password} "
        "connect_timeout=15"
    )
    for attempt in range(1, 8):
        try:
            with psycopg.connect(conn_str) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    for step in SCHEMA_STEPS:
                        stmt = step.strip()
                        if stmt:
                            cur.execute(stmt)
                    for step in SCHEMA_MIGRATIONS:
                        stmt = step.strip()
                        if stmt:
                            cur.execute(stmt)
                conn.commit()
            print("  [RDS] Schema bootstrap complete")
            return
        except Exception as e:
            if attempt == 7:
                raise
            print(f"  [RDS] Bootstrap attempt {attempt}/7: {e}. Retrying in 15s ...")
            time.sleep(15)


def destroy_rds(rds, ec2, state: DeploymentState) -> None:
    if state.rds_instance_id:
        print(f"  [teardown] Deleting RDS {state.rds_instance_id} ...")
        try:
            rds.delete_db_instance(
                DBInstanceIdentifier=state.rds_instance_id,
                SkipFinalSnapshot=True,
                DeleteAutomatedBackups=True,
            )
            w = rds.get_waiter("db_instance_deleted")
            w.wait(
                DBInstanceIdentifier=state.rds_instance_id,
                WaiterConfig={"Delay": 30, "MaxAttempts": 60},
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in ("DBInstanceNotFound",):
                print(f"  [teardown] RDS delete: {code}")
        state.rds_instance_id = None

    if state.db_subnet_group:
        try:
            rds.delete_db_subnet_group(DBSubnetGroupName=state.db_subnet_group)
            print(f"  [teardown] Deleted subnet group {state.db_subnet_group}")
        except ClientError as exc:
            print(f"  [teardown] Subnet group: {exc.response['Error']['Code']}")
        state.db_subnet_group = None

    if state.rds_sg_id:
        try:
            ec2.delete_security_group(GroupId=state.rds_sg_id)
            print(f"  [teardown] Deleted RDS SG {state.rds_sg_id}")
        except ClientError as exc:
            print(f"  [teardown] RDS SG: {exc.response['Error']['Code']}")
        state.rds_sg_id = None
