# DijkFood (cloud_comp_a1)

Automated AWS deployment for the cloud computing assignment: **RDS (PostgreSQL)** for core entities, **DynamoDB** for order logs and courier positions, **three ECR/ECS Fargate services** (**ordering**, **tracking**, **routing**) behind one **ALB** (path-based routing) on the **Learner Lab** account, optional load simulators, and teardown.

The **conversational agent** (Bedrock) runs on the **same lab ALB** as the microservices (`--with-agent`). Bedrock API calls use keys from [`.env.agent`](.env.agent.example) only — no ECS/ALB in the credits account.

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
python deploy.py --skip-teardown   # keep stack; connection.env updated throughout + on exit
python deploy.py --teardown-only  # destroy using connection.env (deleted after, unless --keep-connection-env)
python deploy.py --service routing   # rebuild/push one image and roll ECS (ordering|tracking|routing)
```

### Agent (Bedrock keys only; infra on lab)

```bash
cp .env.agent.example .env.agent   # credits-account API keys + BEDROCK_MODEL_ID
python deploy.py --skip-teardown --with-agent   # lab stack + agent at /agent/*
python deploy_agent_ui.py          # optional UI at {BASE_URL}/ui/
python deploy.py --service agent   # redeploy agent image only
```

Bedrock credentials are injected as `BEDROCK_*` on the lab ECS task (DynamoDB sessions use the lab task role). Keys in task definitions are visible in the ECS console — course lab only.

`deploy.py` reads Bedrock keys from `.env.agent` **only for the ECS task**; it uses **`.env` or your default AWS profile** for lab infra. Do not `export` Bedrock keys in the shell before deploy, or run `unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY` first.

Use **`--service`** after a successful deploy (with **`connection.env`**) when only one microservice changed. **Ordering** redeploys need **`DIJKFOOD_DB_PASSWORD`** in `.env` (never stored in `connection.env`). **`DB_HOST`** is written on new full deploys; older snapshots can still resolve the host from **`RDS_INSTANCE_ID`**.

If you set **`TASK_ROLE_ARN`** in `.env`, the deploy script **does not** attach IAM inline policies to that role. Add **DynamoDB** table access as required by your lab, and for routing graph cache add **`s3:GetObject`**, **`s3:PutObject`**, **`s3:HeadObject`** on **`arn:aws:s3:::ROUTING_GRAPH_S3_BUCKET/*`** (see **`connection.env`** after deploy).

After a successful deploy, install simulator deps and run tools **manually** (not wired into `deploy.py`):

```bash
pip install -r simulator/requirements.txt
python -m simulator.orchestration.check_health --base-url "http://$(grep '^BASE_URL=' connection.env | cut -d= -f2-)"
python -m simulator.loaders.data_loader --base-url "http://<your-alb-dns>" --customers 10
python -m simulator.orchestration.load_test --base-url "http://<your-alb-dns>" --rate 3 --duration 30 --workers 2
python -m simulator.orchestration.load_sim --base-url "http://<your-alb-dns>" --path /health --rate 10 --duration 15
```

- **Ordering** (full API): default ALB target (`/health`, `/customers`, …). **`POST /orders`** uses **`order_status_id`** (1–6); **`courier_id`** may be omitted when **`ROUTING_BASE_URL`** is set (nearest idle courier by drive distance using lat/**lon** / initial coordinates). Placeholder workflow routes under **`/sim/*`** return **501** until you implement them; the load test falls back to **`POST /orders`**, **`PUT /orders/{id}`**, and **`POST /order-logs`**.
- **Tracking** (stub): `http://<alb>/tracking/health`, `/tracking/`.
- **Routing** (OSMnx + NetworkX): `GET /health` (ALB liveness), **`GET /routing/ready`** (503 until the street graph is loaded), **`GET /routing/v1/random-points?n=...`** (graph-backed lat/**lon** samples for [`simulator/data_loader.py`](simulator/data_loader.py)), **`POST /routing/v1/shortest-path`**, **`POST /routing/v1/nearest-courier`**. Full **`deploy.py`** creates an **S3 bucket** per deployment and sets **`ROUTING_GRAPH_S3_BUCKET`** on the routing task: the first task **downloads OSM** and **uploads a GraphML** snapshot (`graphs/{place}__{network_type}.graphml`); later task starts and redeploys **load from S3** (much faster). Without that env (e.g. local runs), behavior is OSM-only. Data © OpenStreetMap contributors ([ODbL](https://www.openstreetmap.org/copyright)).

### `connection.env`

[`deploy.py`](deploy.py) **refreshes** [`connection.env`](connection.env) at the repo root (gitignored) **after each major provisioning step**. With `--skip-teardown`, the file is always rewritten on exit. **`BASE_URL`** may be empty until the ALB exists. **No database password** is stored.

| Key | Purpose |
|-----|---------|
| `AWS_REGION` | Region for boto3 clients |
| `DEPLOYMENT_SUFFIX` | Same suffix used in resource names |
| `BASE_URL` | `http://<alb-dns>` for HTTP tools (ordering API at root paths) |
| `ECS_SERVICES_JSON` | JSON array of ECS microservice records (required if `ECS_CLUSTER_NAME` is set) — used for teardown |
| `RDS_INSTANCE_ID`, `DB_HOST` (RDS endpoint), `RDS_SECURITY_GROUP_ID`, `DB_SUBNET_GROUP` | RDS address / teardown |
| `ECS_TASK_SECURITY_GROUP_ID`, `ALB_SECURITY_GROUP_ID` | SG teardown |
| `ALB_ARN`, `LISTENER_ARN` | ALB teardown |
| `ECS_CLUSTER_NAME` | ECS cluster name |
| `EXECUTION_ROLE_ARN`, `TASK_ROLE_ARN` | Reference; **IAM roles are not deleted** on teardown |
| `DYNAMO_ORDER_LOGS_TABLE`, `DYNAMO_COURIER_POSITIONS_TABLE` | DynamoDB teardown |
| `ROUTING_GRAPH_S3_BUCKET` | S3 bucket for cached routing GraphML (teardown deletes bucket) |
| `CREATED_SECURITY_GROUP_IDS` | Optional comma-separated list (extra SGs) |

### Load tools

Install once: `pip install -r simulator/requirements.txt` (Faker for the data loader). The load test expects **RDS + DynamoDB** schemas matching this repo (composite order logs, courier positions with `lat`/`lon`, etc.).

Simulator commands load **[`.env`](.env)** first, then **[`connection.env`](connection.env)** if it exists ([`simulator/shared/env_load.py`](simulator/shared/env_load.py)). Duplicate keys keep the value from `.env` so you can override the deploy snapshot locally. **`BASE_URL`** from `connection.env` (written by `deploy.py`) is picked up automatically—no need to pass `--base-url` if it is set.

| Module | Purpose |
|--------|---------|
| [`simulator/orchestration/check_health.py`](simulator/orchestration/check_health.py) | `GET` ordering `/health`, routing `/routing/health` + `/routing/ready`, tracking `/tracking/health`. **`--strict-ready`** fails if the routing graph is not ready (503). |
| [`simulator/loaders/data_loader.py`](simulator/loaders/data_loader.py) | **Faker**-backed **POST** of customers, food places, and **3×** couriers (configurable factor); **no orders**. Tries **`GET /routing/v1/random-points`** on `--routing-base-url` (defaults to `--base-url`), else São Paulo **bbox** fallback. Writes **`dijkfood_sim_state.json`** (override with `--state-file`). |
| [`simulator/orchestration/load_test.py`](simulator/orchestration/load_test.py) | Service-mode orchestrator that runs customer/food_place/courier simulation loops together (API discovery via GET; no runtime state-file dependency). |
| [`simulator/orchestration/load_sim.py`](simulator/orchestration/load_sim.py) | Lightweight **GET** traffic (default `/health`). |
| [`simulator/shared/http_client.py`](simulator/shared/http_client.py), [`simulator/loaders/sp_coords.py`](simulator/loaders/sp_coords.py), [`simulator/shared/scenarios.py`](simulator/shared/scenarios.py), [`simulator/shared/env_load.py`](simulator/shared/env_load.py) | Shared HTTP, coordinates, scenario steps, env loading. |

## Microservices layout

| Service | ALB rule | Source | Role (sketch) |
|---------|-----------|--------|---------------|
| ordering | default | [`services/ordering/`](services/ordering) (build context: repo root) | Full FastAPI app in [`services/ordering/`](services/ordering/) |
| tracking | `/tracking*` | [`services/tracking/`](services/tracking) | Stub (FastAPI); future courier tracking |
| routing | `/routing*` | [`services/routing/`](services/routing) | OSMnx street graph + Dijkstra shortest paths (`length` in meters) |

## Data model (ERD; see `erdplus.png`)

### RDS (PostgreSQL)

- **`order_statuses`** — `order_status_id` (PK) + `status` text. Seeded rows: **1** CONFIRMED, **2** PREPARING, **3** READY_FOR_PICKUP, **4** PICKED_UP, **5** IN_TRANSIT, **6** DELIVERED.
- **`customers`**, **`food_places`** — `lat` and **`lon`** are **NOT NULL** (WGS84).
- **`couriers`** — `initial_lat`, **`initial_lon`** NOT NULL (initial depot / home coordinates); **`courier_status_t`** for operational status (`idle`, …).
- **`orders`** — FK `order_status_id` → `order_statuses`, plus `customer_id`, `food_place_id`, `courier_id`.
- **`kitchen_type_t`**, **`vehicle_type_t`** — unchanged enum pattern.

`GET /order-statuses` lists status IDs for clients. **Order event history** is in **DynamoDB**, not Postgres.

### DynamoDB

- **Order logs** — composite primary key **`orderId`** (number) + **`timestamp`** (string, e.g. ISO-8601). Attribute **`orderStatusId`** (number) aligns with RDS `order_statuses`. Optional `detail`.
- **Courier positions** — composite key **`courierId`** + **`timestamp`** (epoch ms). Attributes **`position`**, **`lat`**, **`lon`** (numbers).

If you change Dynamo key design vs. an existing table, **delete the table** (or use a new deployment suffix) before redeploying; `ResourceInUseException` reuses the old layout.

The **ordering** ECS task receives `DYNAMODB_ORDER_LOGS_TABLE`, `DYNAMODB_COURIER_POSITIONS_TABLE`, and `AWS_REGION` / `AWS_DEFAULT_REGION`.

## Layout

- [`deploy.py`](deploy.py) — full pipeline, `--service` (single ECS redeploy), and `--teardown-only`; shared teardown via `run_teardown` (ECS → DynamoDB → RDS, including security groups)
- [`tools/connection_env.py`](tools/connection_env.py) — read/write `connection.env`
- [`tools/rds_infra.py`](tools/rds_infra.py) — RDS + `SCHEMA_STEPS` enum bootstrap
- [`tools/dynamodb_infra.py`](tools/dynamodb_infra.py) — DynamoDB create/destroy + IAM policy attachment
- [`tools/s3_routing_graph_infra.py`](tools/s3_routing_graph_infra.py) — S3 bucket for routing graph cache + task-role policy + teardown
- [`tools/ecs_infra.py`](tools/ecs_infra.py) — ALB, multi-target routing, ECS; teardown does **not** delete IAM roles
- [`services/ordering/`](services/ordering/) — FastAPI ordering service source; OpenAPI UI at `/docs`
- [`simulator/`](simulator/) — simulation service runners + orchestrator; see [`simulator/SERVICES.md`](simulator/SERVICES.md)
- [`deploy_simulation.py`](deploy_simulation.py) — deploy/redeploy/start/stop/shutdown simulation ECS services independently

## REST API (per entity)

Each entity has its own router module under [`app/routers/`](app/routers/): **POST** (create), **GET** list and **GET** by id (or composite key), **PUT** (replace), **DELETE**.

| Prefix | Module | Storage |
|--------|--------|---------|
| `/customers` | [`customers.py`](app/routers/customers.py) | RDS (`lat`, `lon` required) |
| `/food-places` | [`food_places.py`](app/routers/food_places.py) | RDS (enum `kitchen_type_t`) |
| `/couriers` | [`couriers.py`](app/routers/couriers.py) | RDS (`initial_lat`, `initial_lon` required) |
| `/order-statuses` | [`order_statuses.py`](app/routers/order_statuses.py) | RDS (read-only list) |
| `/orders` | [`orders.py`](app/routers/orders.py) | RDS (`order_status_id` FK) |
| `/order-logs` | [`order_logs.py`](app/routers/order_logs.py) | DynamoDB composite `order_id` + `timestamp` in path; list `?order_id=` |
| `/courier-positions` | [`courier_positions.py`](app/routers/courier_positions.py) | DynamoDB `courier_id` + `timestamp_ms`; `lat`/`lon` on items |
| `/sim` | [`sim_placeholders.py`](app/routers/sim_placeholders.py) | Placeholder **501** workflow hooks for the simulator (to be implemented) |

Shared DB access: [`app/database.py`](app/database.py) (Postgres dependency). DynamoDB tables: [`app/dynamo.py`](app/dynamo.py).
