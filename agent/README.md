# DijkFood conversational agent

Natural-language Q&A about order state and history. The agent calls **read-only tools** in `agent_functions.py` (HTTP wrappers to ordering, tracking, and routing). It does not access RDS or DynamoDB data tables directly.

**Deploy:** [`deploy.py`](../deploy.py) with `--with-agent` on **Learner Lab** (same ALB as microservices). Copy [`.env.agent.example`](../.env.agent.example) → `.env.agent` with **Bedrock account API keys** only (no infra in that account).

## HTTP API (ALB prefix `/agent`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/agent/health` | Liveness |
| GET | `/agent/v1/tools` | Enabled tools and metadata |
| GET | `/agent/v1/usage` | Aggregated usage: tokens from CloudWatch (Bedrock account), chat/tool counts from DynamoDB |
| POST | `/agent/v1/chat` | `{ "message": "...", "conversation_id"?: "uuid" }` → `{ "conversation_id", "reply", "tools_used", "usage" }` |
| DELETE | `/agent/v1/conversations/{id}` | Delete session |

## Tool registry

Tools are registered in `agent/tools/` on import. Enable subsets with `AGENT_ENABLED_TOOLS` in `.env.agent`. Disable with `AGENT_DISABLED_TOOLS`. Unset `AGENT_ENABLED_TOOLS` exposes all `stable` and `beta` tools.

| Tool | Service | Status | Backend |
|------|---------|--------|---------|
| `get_order` | ordering | stable | `GET /orders/{order_id}` |
| `list_orders` | ordering | stable | `GET /orders` |
| `list_order_statuses` | ordering | stable | `GET /order-statuses` |
| `get_order_current_status` | tracking | stable | `GET /tracking/get-order-status` |
| `get_order_history` | tracking | stable | `GET /tracking/get-order-log` |
| `get_route_for_order` | routing | beta | `GET /routing/v1/get-route` |
| `get_courier` | ordering | beta | `GET /couriers/{courier_id}` |
| `list_couriers` | ordering | beta | `GET /couriers` |
| `get_courier_position` | tracking | beta | `GET /tracking/get-courier-position` |
| `get_customer` | ordering | beta | `GET /customers/{customer_id}` |
| `get_food_place` | ordering | beta | `GET /food-places/{food_place_id}` |
| `report_unrelated_question` | agent | stable | Last resort when no other tool can answer; fixed refusal message |

`report_unrelated_question` stays enabled even when `AGENT_ENABLED_TOOLS` is set (unless listed in `AGENT_DISABLED_TOOLS`).

## Environment (lab ECS task)

| Variable | Purpose |
|----------|---------|
| `BASE_URL` | Lab ALB (ordering) |
| `TRACKING_BASE_URL` | `{BASE_URL}/tracking` |
| `ROUTING_BASE_URL` | Lab ALB root |
| `DYNAMODB_AGENT_SESSIONS_TABLE` | Sessions table on **lab** account |
| `BEDROCK_MODEL_ID` | From `.env.agent` at deploy time |
| `BEDROCK_AWS_*` | Bedrock credentials (not `AWS_ACCESS_KEY_ID` on task) |
| `AGENT_MAX_TOOL_ROUNDS` | Default `5` |
| `AGENT_USAGE_BUDGET_*` | Optional monitoring UI budgets |
| `AGENT_USAGE_HISTORY_DAYS` | CloudWatch lookback for token totals (default `7`) |

Token totals on `/v1/usage` come from **CloudWatch** (`AWS/Bedrock` metrics) using the same `BEDROCK_AWS_*` credentials as Converse. The Bedrock account IAM user needs `cloudwatch:GetMetricData`. Chat request and tool-call counters remain in DynamoDB and reset when the sessions table is torn down.

Per-request token counts in `POST /v1/chat` still come from the Converse response (not CloudWatch).

## UI

| Mode | Setup |
|------|--------|
| **Deployed UI (no CORS)** | `python deploy_agent_ui.py` → `{BASE_URL}/ui/` and `/ui/monitor.html` |
| **Local UI → lab agent** | `AGENT_CORS_ORIGINS` in `.env.agent`, then `python deploy.py --service agent` |

## Local run (developer)

```bash
pip install -r agent/requirements.txt
export PYTHONPATH=/path/to/repo
export BASE_URL=http://lab-alb/...
export ROUTING_BASE_URL=$BASE_URL
export DYNAMODB_AGENT_SESSIONS_TABLE=...
export BEDROCK_MODEL_ID=amazon.nova-lite-v1:0
# Local: default AWS_* from .env.agent is fine for Bedrock
export AWS_REGION=us-east-1
uvicorn agent.main:app --reload --port 8003
```
