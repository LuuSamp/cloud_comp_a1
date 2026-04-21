# Simulator Service Mode

The simulator is organized into domain packages:

- `simulator.services` for service loops
- `simulator.shared` for common runtime/http/discovery/scenario helpers
- `simulator.orchestration` for multi-service runners and health/load orchestration
- `simulator.loaders` for seed/load scripts

## Local Run

From repository root:

```bash
pip install -r simulator/requirements.txt
python -m simulator.services.customer_simulation_service --base-url "http://<alb>" --orders-per-second 3
python -m simulator.services.food_place_simulation_service --base-url "http://<alb>" --status-parallel 24
python -m simulator.services.courier_simulation_service --base-url "http://<alb>" --position-interval-ms 100
```

Each service prints periodic response-time stats (`min/p50/p95/max`) every 10 seconds by default.
Use `--log-interval-s <seconds>` to change it.

Run all three together:

```bash
python -m simulator.orchestration.load_test --base-url "http://<alb>" --orders-per-second 3 --log-interval-s 5
```

## Route Cache / Route Lookup

Routing now caches store->customer paths and exposes:

```bash
GET /routing/v1/get-route?order_id=<order_id>
GET /routing/v1/get-route?customer_id=<customer_id>&food_place_id=<food_place_id>
```

Route polling treats `HTTP 503` as transient during readiness/load transitions and avoids repeated stderr failure spam.

## ECS Deploy / Lifecycle

Use `deploy_simulation.py` for independent service lifecycle control:

```bash
python deploy_simulation.py --service customer --action deploy
python deploy_simulation.py --service food_place --action deploy
python deploy_simulation.py --service courier --action deploy
```

Control services without tearing down the core stack:

```bash
python deploy_simulation.py --service courier --action stop
python deploy_simulation.py --service courier --action start --desired-count 1
python deploy_simulation.py --service courier --action shutdown
python deploy_simulation.py --service courier --action status
```
