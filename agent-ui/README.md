# DijkFood Agent UI

Chat UI for the [agent API](../agent/README.md).

## Deployed (recommended — no CORS)

After lab deploy with the agent (`python deploy.py --skip-teardown --with-agent`), publish the UI on the **same lab ALB**:

```bash
python deploy_agent_ui.py
```

Open the URL printed at the end (stored as `AGENT_UI_URL` in `connection.env`), e.g.:

`http://<lab-alb-dns>/ui/`

The page uses the **same origin** as the agent API (`/agent/...`), so you do not need `AGENT_CORS_ORIGINS`.

Redeploy UI only after changes (required if chat/monitor feels static — usually a stale image missing `api.js`):

```bash
python deploy_agent_ui.py
```

Remove UI resources:

```bash
python deploy_agent_ui.py --teardown
```

**Requires:** `connection.env` from `deploy.py --with-agent`.

## Local development

For local API on port 8003 with UI on 8080, set `AGENT_CORS_ORIGINS` in `.env.agent` and redeploy the agent:

```bash
export AGENT_CORS_ORIGINS=http://localhost:8080,http://127.0.0.1:8080
python deploy.py --service agent
```

```bash
cd agent-ui && python -m http.server 8080
```

Point the UI at `http://localhost:8003` (or your lab `BASE_URL`).

## Features

- Multi-turn chat (`conversation_id` in DynamoDB on the lab account)
- **Monitoring** page at `/ui/monitor.html` — token totals, daily breakdown, optional budgets
- **New chat** clears the server session
- **Tools used** expander on replies; per-reply token counts when the API returns `usage`
- Health check against `/agent/health`

### Usage budgets

In `.env.agent` set optional limits (shown as progress bars on the monitoring page):

```bash
AGENT_USAGE_BUDGET_TOKENS=500000
AGENT_USAGE_DAILY_BUDGET_TOKENS=50000
```

Redeploy the agent after changing these (`python deploy.py --service agent`).
