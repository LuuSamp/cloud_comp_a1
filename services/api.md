# DijkFood Services API

This document describes the HTTP endpoints exposed by the services under `services/`:

- `services/ordering` (mounted at ALB root, e.g. `/customers`, `/orders`)
- `services/tracking` (mounted at `/tracking/*`)
- `services/routing` (mounted at `/routing/*`)

---

## Ordering Service (`services/ordering`)

Base path: `/`

### Health / Infra Checks

#### `GET /health`
- **What it does:** Basic liveness check for ordering.
- **Input:** none
- **Output:** `{ "status": "ok" }`

#### `GET /db-check`
- **What it does:** Verifies ordering can connect to PostgreSQL.
- **Input:** none
- **Output:** one of:
  - `{ "db": "ok" }`
  - `{ "db": "skipped", "detail": "DB_HOST unset" }`

#### `GET /dynamo-check`
- **What it does:** Verifies DynamoDB table envs are configured and reachable.
- **Input:** none
- **Output:** one of:
  - `{ "dynamo": "ok" }`
  - `{ "dynamo": "skipped", "detail": "..." }`
  - `{ "dynamo": "error", "detail": "..." }`

### Customers

#### `POST /customers`
- **What it does:** Creates a customer profile.
- **Input JSON:** `{ name, email, phone, address, lat, lon }`
- **Output JSON (201):** `{ customer_id, name, email, phone, address, lat, lon }`

#### `GET /customers`
- **What it does:** Lists customers (paginated).
- **Query params:** `skip` (default `0`), `limit` (default `50`, max `200`)
- **Output JSON:** `[CustomerOut, ...]`

#### `GET /customers/{customer_id}`
- **What it does:** Gets one customer by ID.
- **Input:** path `customer_id`
- **Output JSON:** `CustomerOut`

#### `PUT /customers/{customer_id}`
- **What it does:** Replaces all editable customer fields.
- **Input JSON:** same shape as create
- **Output JSON:** updated `CustomerOut`

#### `DELETE /customers/{customer_id}`
- **What it does:** Deletes a customer.
- **Input:** path `customer_id`
- **Output:** `204 No Content`

### Food Places

#### `POST /food-places`
- **What it does:** Creates a restaurant/food place.
- **Input JSON:** `{ name, kitchen_type, address, lat, lon }`
- **Output JSON (201):** `{ food_place_id, name, kitchen_type, address, lat, lon }`

#### `GET /food-places`
- **What it does:** Lists food places (paginated).
- **Query params:** `skip`, `limit`
- **Output JSON:** `[FoodPlaceOut, ...]`

#### `GET /food-places/{food_place_id}`
- **What it does:** Gets one food place by ID.
- **Output JSON:** `FoodPlaceOut`

#### `PUT /food-places/{food_place_id}`
- **What it does:** Replaces food place attributes.
- **Input JSON:** same shape as create
- **Output JSON:** updated `FoodPlaceOut`

#### `DELETE /food-places/{food_place_id}`
- **What it does:** Deletes a food place.
- **Output:** `204 No Content`

### Couriers

#### `POST /couriers`
- **What it does:** Creates a courier.
- **Input JSON:** `{ name, vehicle_type, initial_address, status, last_position?, initial_lat, initial_lon }`
- **Output JSON (201):** `{ courier_id, ... }`

#### `GET /couriers`
- **What it does:** Lists couriers (paginated).
- **Query params:** `skip`, `limit`
- **Output JSON:** `[CourierOut, ...]`

#### `GET /couriers/{courier_id}`
- **What it does:** Gets courier details by ID.
- **Output JSON:** `CourierOut`

#### `PUT /couriers/{courier_id}`
- **What it does:** Replaces courier data (including status and initial position).
- **Input JSON:** same shape as create (with required fields)
- **Output JSON:** updated `CourierOut`

#### `DELETE /couriers/{courier_id}`
- **What it does:** Deletes a courier.
- **Output:** `204 No Content`

### Orders

#### `POST /orders`
- **What it does:** Creates an order; if `courier_id` is omitted and routing is configured, assigns nearest available courier.
- **Input JSON:** `{ customer_id, food_place_id, courier_id?, order_status_id }`
- **Output JSON (201):**
  `{ order_id, order_status_id, order_status, customer_id, food_place_id, courier_id }`

#### `GET /orders`
- **What it does:** Lists orders with status label.
- **Query params:** `skip`, `limit`
- **Output JSON:** `[OrderOut, ...]`

#### `GET /orders/{order_id}`
- **What it does:** Returns one order.
- **Output JSON:** `OrderOut`

#### `PUT /orders/{order_id}`
- **What it does:** Replaces order fields (`customer_id`, `food_place_id`, `courier_id`, `order_status_id`).
- **Input JSON:** `{ customer_id, food_place_id, courier_id, order_status_id }`
- **Output JSON:** updated `OrderOut`

#### `DELETE /orders/{order_id}`
- **What it does:** Deletes an order row.
- **Output:** `204 No Content`

### Order Status Dictionary

#### `GET /order-statuses`
- **What it does:** Returns status lookup rows from RDS.
- **Output JSON:** `[ { order_status_id, status }, ... ]`

### Order Logs (DynamoDB)

#### `POST /order-logs`
- **What it does:** Inserts one order log entry (`orderId + timestamp` key).
- **Input JSON:** `{ order_id, timestamp, order_status_id, detail? }`
- **Output JSON (201):** `{ order_id, timestamp, order_status_id, detail? }`

#### `GET /order-logs`
- **What it does:** Lists logs; can filter by `order_id`.
- **Query params:** `order_id?`, `limit` (max `200`)
- **Output JSON:** `[OrderLogOut, ...]`

#### `GET /order-logs/{order_id}/{timestamp}`
- **What it does:** Reads one log record by key.
- **Output JSON:** `OrderLogOut`

#### `PUT /order-logs/{order_id}/{timestamp}`
- **What it does:** Replaces log status/detail at an existing key.
- **Input JSON:** `{ order_status_id, detail? }`
- **Output JSON:** updated `OrderLogOut`

#### `DELETE /order-logs/{order_id}/{timestamp}`
- **What it does:** Deletes one log record.
- **Output:** `204 No Content`

### Courier Positions (DynamoDB)

#### `POST /courier-positions`
- **What it does:** Writes one courier position snapshot.
- **Input JSON:** `{ courier_id, timestamp?, position, lat, lon }`
- **Output JSON (201):** `{ courier_id, timestamp, position, lat, lon }`

#### `GET /courier-positions`
- **What it does:** Lists positions; can filter by `courier_id`.
- **Query params:** `courier_id?`, `limit` (max `200`)
- **Output JSON:** `[CourierPositionOut, ...]`

#### `GET /courier-positions/{courier_id}/{timestamp_ms}`
- **What it does:** Reads one position record.
- **Output JSON:** `CourierPositionOut`

#### `PUT /courier-positions/{courier_id}/{timestamp_ms}`
- **What it does:** Replaces a position record.
- **Input JSON:** `{ position, lat, lon }`
- **Output JSON:** updated `CourierPositionOut`

#### `DELETE /courier-positions/{courier_id}/{timestamp_ms}`
- **What it does:** Deletes one position record.
- **Output:** `204 No Content`

### Workflow Endpoints (Ordering orchestration)

#### `POST /place-order`
- **What it does:** Creates a new order with `courier_id = NULL`, validates route food place -> customer via routing, and writes initial order log.
- **Input JSON:** `{ customer_id, food_place_id, order_status_id }`
- **Output JSON (201):** `{ ok, message, order_id }`

#### `POST /assign-courier`
- **What it does:** For orders at status `3` (READY_FOR_PICKUP), selects nearest available courier (latest Dynamo position fallback to initial RDS position) and assigns it.
- **Input JSON:** `{ order_id }`
- **Output JSON:** `{ ok, order_id, courier_id }`

### Simulation Placeholders

#### `POST /sim/orders/place`
- **What it does:** Placeholder endpoint, intentionally not implemented.
- **Input JSON:** `{ customer_id, food_place_id }`
- **Output:** `501` with `{ detail: "Not implemented..." }`

#### `POST /sim/orders/{order_id}/transition`
- **What it does:** Placeholder status transition endpoint.
- **Input JSON:** `{ order_status_id, detail? }`
- **Output:** `501` with placeholder payload

---

## Tracking Service (`services/tracking`)

Base path in ALB: `/tracking`

### Health

#### `GET /health`
- **What it does:** App-level health endpoint (outside mount).
- **Output JSON:** `{ "status": "ok", "service": "tracking" }`

#### `GET /tracking/health`
- **What it does:** Mounted API health endpoint.
- **Output JSON:** `{ "status": "ok", "service": "tracking" }`

#### `GET /tracking/`
- **What it does:** Mounted API root info.
- **Output JSON:** `{ "service": "tracking", "detail": "ready" }`

### Tracking Operations

#### `POST /tracking/update-order-status`
- **What it does:** Applies one-step order transition in RDS (`prev -> next` only), appends to Dynamo order logs, and attempts courier assignment when status becomes `3`.
- **Input JSON:** `{ order_id, order_status_id, detail? }` (`order_status_id` must be `>= 2`)
- **Output JSON:** `{ ok, order_id, order_status_id }`

#### `POST /tracking/update-courier-position`
- **What it does:** Writes courier location snapshot to Dynamo positions table.
- **Input JSON:** `{ courier_id, timestamp?, position, lat, lon }`
- **Output JSON:** `{ ok, courier_id, timestamp }`

#### `POST /tracking/update-courier-status`
- **What it does:** Updates courier status in RDS.
- **Input JSON:** `{ courier_id, status }`
- **Output JSON:** `{ ok, courier_id, status }`

### Tracking Queries

#### `GET /tracking/get-courier-position?courier_id=...`
- **What it does:** Returns latest known position for a courier from Dynamo (descending timestamp).
- **Output JSON:** Dynamo-normalized object with `courierId`, `timestamp`, `position`, `lat`, `lon`

#### `GET /tracking/get-order-status?order_id=...`
- **What it does:** Returns latest order status from Dynamo logs.
- **Output JSON:** `{ order_id, timestamp, order_status_id, detail? }`

#### `GET /tracking/get-order-log?order_id=...`
- **What it does:** Returns full order log history from Dynamo (ascending timestamp).
- **Output JSON:** `{ order_id, items: [ ... ] }`

---

## Routing Service (`services/routing`)

Base path in ALB: `/routing`

### Health / Readiness

#### `GET /health`
- **What it does:** App-level liveness endpoint.
- **Output JSON:** `{ "status": "ok", "service": "routing" }`

#### `GET /routing/health`
- **What it does:** Mounted API health endpoint.
- **Output JSON:** `{ "status": "ok", "service": "routing" }`

#### `GET /routing/ready`
- **What it does:** Readiness check for graph load completion.
- **Output JSON:** one of:
  - `{ "ready": true, "service": "routing" }`
  - `503` with `{ "ready": false, "detail": "loading|<error>" }`

#### `GET /routing/`
- **What it does:** Mounted API info root.
- **Output JSON:** `{ "service": "routing", "detail": "OSMnx + Dijkstra (São Paulo default)" }`

### Routing Operations

#### `GET /routing/v1/random-points?n=...`
- **What it does:** Samples random graph node coordinates, useful for fixture generation.
- **Query params:** `n` (1..500)
- **Output JSON:** `{ "points": [ { "lat": float, "lon": float }, ... ] }`

#### `POST /routing/v1/shortest-path`
- **What it does:** Computes shortest path between two lat/lng points on the road graph.
- **Input JSON:** `{ "origin": {lat,lng}, "destination": {lat,lng} }`
- **Output JSON:** 
  - success: `{ distance_m, node_ids, coordinates, error: null }`
  - no route/error: `{ error: "no_path_between_points|could_not_measure_route" }`

#### `POST /routing/v1/nearest-courier`
- **What it does:** Chooses courier with minimum graph route distance from restaurant.
- **Input JSON:** `{ "restaurant": {lat,lng}, "candidates": [ { courier_id, lat, lng }, ... ] }`
- **Output JSON:**
  - success: `{ courier_id, distance_m, node_ids, coordinates, error: null }`
  - no reachable courier: `{ error: "no_reachable_courier" }`

