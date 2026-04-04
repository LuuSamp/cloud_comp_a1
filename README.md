# DijkFood (cloud_comp_a1)

Automated AWS deployment for the cloud computing assignment: RDS (PostgreSQL) for the ERD schema, ECR/ECS Fargate API behind an ALB, load simulator, and teardown.

## Prerequisites

- AWS CLI credentials in `~/.aws/credentials` (or environment variables)
- Docker (for `docker build` / `docker push` to ECR)
- Python 3.11+

## Setup

```bash
cd cloud_comp_a1
pip install -r requirements-deploy.txt
```

Optional: set `DIJKFOOD_DB_PASSWORD` for a stable RDS master password. If unset, a random password is generated for the run (store it if you use `--skip-teardown`).

## Run

```bash
python deploy.py
python deploy.py --sim-rate 20 --sim-duration 30
python deploy.py --skip-teardown   # debug: no teardown
```

## Layout

- [`deploy.py`](deploy.py) — orchestrates provision → deploy → simulator → teardown
- [`tools/`](tools/) — RDS, ECS/ECR/ALB helpers
- [`app/`](app/) — minimal FastAPI container (`/health`, `/db-check`)
- [`simulator/`](simulator/) — HTTP load tool (`python -m simulator.load_sim --help`)

PostgreSQL DDL matches the planned entities: `customers`, `food_places`, `couriers`, `orders`, `order_logs` (with `created_at` for chronological event history).
