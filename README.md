# DijkFood (cloud_comp_a1)

Automated AWS deployment for the cloud computing assignment: **RDS (PostgreSQL)** for core entities, **DynamoDB** for order logs and courier positions, **ECR/ECS Fargate** behind an **ALB**, load simulator, and teardown.

## Prerequisites

- AWS CLI credentials in `~/.aws/credentials` (or environment variables)
- Docker (for `docker build` / `docker push` to ECR)
- Python 3.11+

## Setup

```bash
cd cloud_comp_a1
pip install -r requirements-deploy.txt
cp env.example .env   # edit ACCOUNT_ID and IAM role ARNs for your learner lab
```

The app and simulators also load `.env` from the repo root when present. Optional: set `DIJKFOOD_DB_PASSWORD` for a stable RDS master password. If unset, a random password is generated for the run (store it if you use `--skip-teardown`).

## Run

```bash
python deploy.py
python deploy.py --sim-rate 20 --sim-duration 30
python deploy.py --skip-teardown   # keep stack; connection.env updated throughout + on exit
python deploy.py --teardown-only    # destroy using connection.env (deleted after, unless --keep-connection-env)
python deploy.py --load-test-only --sim-rate 5 --sim-duration 60   # API load test via BASE_URL in connection.env
```

### `connection.env`

[`deploy.py`](deploy.py) **refreshes** [`connection.env`](connection.env) at the repo root (gitignored) **after each major provisioning step**, so a crash or failed step still leaves a file usable with `--teardown-only`. With `--skip-teardown`, the file is always rewritten on exit (success or failure). If a normal deploy fails, the file is saved again before automatic teardown so you can retry cleanup. **`BASE_URL`** may be empty until the ALB exists. **No database password** is stored (keep using `DIJKFOOD_DB_PASSWORD` when you re-deploy).

| Key | Purpose |
|-----|---------|
| `AWS_REGION` | Region for boto3 clients |
| `DEPLOYMENT_SUFFIX` | Same suffix used in resource names |
| `BASE_URL` | `http://<alb-dns>` for HTTP tools |
| `RDS_INSTANCE_ID`, `RDS_SECURITY_GROUP_ID`, `DB_SUBNET_GROUP` | RDS teardown |
| `ECS_TASK_SECURITY_GROUP_ID`, `ALB_SECURITY_GROUP_ID` | SG teardown |
| `ALB_ARN`, `LISTENER_ARN`, `TARGET_GROUP_ARN` | ALB teardown |
| `ECS_CLUSTER_NAME`, `ECS_SERVICE_NAME`, `ECR_REPO_NAME` | ECS/ECR teardown |
| `EXECUTION_ROLE_ARN`, `TASK_ROLE_ARN`, `LOG_GROUP_NAME`, `TASK_DEFINITION_FAMILY` | Recorded for reference; **IAM roles are not deleted** on teardown. Log group is deleted. |
| `DYNAMO_ORDER_LOGS_TABLE`, `DYNAMO_COURIER_POSITIONS_TABLE` | DynamoDB teardown |
| `CREATED_SECURITY_GROUP_IDS` | Optional comma-separated list (extra SGs) |

### Load tools

- [`simulator/load_sim.py`](simulator/load_sim.py) — lightweight **health** traffic (`GET /health`); used automatically at the end of a full `deploy.py` run.
- [`simulator/load_test.py`](simulator/load_test.py) — **CRUD** across all entities plus a background thread posting **courier positions** at `--position-interval-ms` (default **100** ms), matching high-frequency telemetry. Run via `python -m simulator.load_test` or `deploy.py --load-test-only`.

## Data model (ERD v2)

### RDS (PostgreSQL)

- **`customers`**, **`food_places`**, **`couriers`**, **`orders`** — relational tables with FKs on `orders`.
- **`kitchen_type_t`** (`UNSPECIFIED`, `OTHER`) — extend with `ALTER TYPE kitchen_type_t ADD VALUE '...';`
- **`vehicle_type_t`** (`UNSPECIFIED`, `OTHER`) — same pattern.
- **`courier_status_t`** — `idle`, `in_route_to_restaurant`, `waiting_for_order`, `in_route_to_customer`, `waiting_for_customer`.
- **Order lifecycle** `orders.status` remains `VARCHAR` for PDF states (e.g. `CONFIRMED` → `DELIVERED`).
- **Order event history** lives in **DynamoDB**, not Postgres.

### DynamoDB

- **Order logs** — PK `orderLogId` (string). GSI `orderId-timestamp-index`: `orderId` (number, matches RDS `orders.order_id`), `timestamp` (string, use ISO-8601 for sortable queries).
- **Courier positions** — composite key `courierId` (number) + `timestamp` (number, **epoch milliseconds**). Attribute `position` (string/JSON) is application-defined.

The ECS task receives `DYNAMODB_ORDER_LOGS_TABLE`, `DYNAMODB_COURIER_POSITIONS_TABLE`, and `AWS_REGION` / `AWS_DEFAULT_REGION`.

## Layout

- [`deploy.py`](deploy.py) — full pipeline, `--teardown-only`, or `--load-test-only`; shared teardown via `run_teardown` (ECS → DynamoDB → RDS, including security groups)
- [`tools/connection_env.py`](tools/connection_env.py) — read/write `connection.env`
- [`tools/rds_infra.py`](tools/rds_infra.py) — RDS + `SCHEMA_STEPS` enum bootstrap
- [`tools/dynamodb_infra.py`](tools/dynamodb_infra.py) — DynamoDB create/destroy + IAM policy attachment
- [`tools/ecs_infra.py`](tools/ecs_infra.py) — ALB, ECS; teardown does **not** delete IAM roles or detach policies
- [`app/`](app/) — FastAPI (see **REST API** below); OpenAPI UI at `/docs` when the container runs

## REST API (per entity)

Each entity has its own router module under [`app/routers/`](app/routers/): **POST** (create), **GET** list and **GET** by id (or composite key), **PUT** (replace), **DELETE**.

| Prefix | Module | Storage |
|--------|--------|---------|
| `/customers` | [`customers.py`](app/routers/customers.py) | RDS |
| `/food-places` | [`food_places.py`](app/routers/food_places.py) | RDS (enum `kitchen_type_t`) |
| `/couriers` | [`couriers.py`](app/routers/couriers.py) | RDS (enums `vehicle_type_t`, `courier_status_t`) |
| `/orders` | [`orders.py`](app/routers/orders.py) | RDS |
| `/order-logs` | [`order_logs.py`](app/routers/order_logs.py) | DynamoDB (`orderLogId`; list with `?order_id=` uses GSI) |
| `/courier-positions` | [`courier_positions.py`](app/routers/courier_positions.py) | DynamoDB (`courier_id` + `timestamp_ms` in path; list with `?courier_id=`) |

Shared DB access: [`app/database.py`](app/database.py) (Postgres dependency). DynamoDB tables: [`app/dynamo.py`](app/dynamo.py).

- [`simulator/`](simulator/) — [`load_sim`](simulator/load_sim.py) (health) and [`load_test`](simulator/load_test.py) (API + position stream)
