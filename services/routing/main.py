"""DijkFood routing service: OSMnx graph + Dijkstra. ALB path prefix: /routing*"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

import boto3
import httpx
import networkx as nx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from graph_service import RoutingGraph

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

_graph = RoutingGraph()


def _load_graph_sync() -> None:
    _graph.load_blocking()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loader = threading.Thread(
        target=_load_graph_sync, name="osmnx-graph-load", daemon=True
    )
    loader.start()
    yield


app = FastAPI(title="DijkFood routing", lifespan=lifespan)


@app.get("/health")
def alb_health() -> dict[str, str]:
    return {"status": "ok", "service": "routing"}


routing_api = FastAPI(title="DijkFood routing API")


class LatLng(BaseModel):
    lat: float = Field(..., description="Latitude (WGS84)")
    lng: float = Field(..., description="Longitude (WGS84)")


class ShortestPathIn(BaseModel):
    origin: LatLng
    destination: LatLng
    order_id: int | None = None
    customer_id: int | None = None
    food_place_id: int | None = None


class ShortestPathOut(BaseModel):
    distance_m: float | None = None
    node_ids: list[int] | None = None
    coordinates: list[dict[str, float]] | None = None
    error: str | None = None


class CourierCandidate(BaseModel):
    courier_id: int
    lat: float
    lng: float


class NearestCourierIn(BaseModel):
    restaurant: LatLng
    candidates: list[CourierCandidate]


class NearestCourierOut(BaseModel):
    courier_id: int | None = None
    distance_m: float | None = None
    node_ids: list[int] | None = None
    coordinates: list[dict[str, float]] | None = None
    error: str | None = None


class CachedRouteOut(BaseModel):
    route_status: str
    order_id: int | None = None
    customer_id: int | None = None
    food_place_id: int | None = None
    distance_m: float | None = None
    node_ids: list[int] | None = None
    coordinates: list[dict[str, float]] | None = None
    created_at: str | None = None
    cache_hit: bool = True
    lookup: str
    error: str | None = None


class RandomPointOut(BaseModel):
    lat: float
    lon: float


class RandomPointsOut(BaseModel):
    points: list[RandomPointOut]


def _require_graph() -> None:
    if _graph.load_error and not _graph.ready:
        raise HTTPException(
            status_code=503,
            detail=f"Graph failed to load: {_graph.load_error}",
        )
    if not _graph.ready:
        raise HTTPException(status_code=503, detail="Graph is still loading")


def _ordering_base_url() -> str:
    base = (
        (os.environ.get("ORDERING_BASE_URL") or os.environ.get("BASE_URL") or "")
        .strip()
        .rstrip("/")
    )
    if not base:
        raise HTTPException(
            status_code=503,
            detail="ORDERING_BASE_URL (or BASE_URL) is required for route lookup",
        )
    return base


def _ordering_get_json(base_url: str, path: str) -> dict[str, object]:
    timeout = httpx.Timeout(8.0, connect=3.0)
    with httpx.Client(timeout=timeout) as client:
        try:
            resp = client.get(f"{base_url}{path}")
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"ordering lookup failed: {type(exc).__name__}",
            ) from exc
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=404 if resp.status_code == 404 else 502,
            detail=f"ordering lookup failed for {path}: HTTP {resp.status_code}",
        )
    data = resp.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=f"invalid ordering payload for {path}")
    return data


def _routes_table():
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    name = (os.environ.get("DYNAMODB_ROUTES_TABLE") or "").strip()
    if not name:
        raise HTTPException(status_code=503, detail="DYNAMODB_ROUTES_TABLE is not configured")
    return boto3.resource("dynamodb", region_name=region).Table(name)


def _get_route_payload(route_key: str) -> dict[str, object] | None:
    resp = _routes_table().get_item(Key={"routeKey": route_key})
    item = resp.get("Item")
    if not isinstance(item, dict):
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload


@routing_api.get("/health")
def mounted_health() -> dict[str, str]:
    return {"status": "ok", "service": "routing"}


@routing_api.get("/v1/random-points", response_model=RandomPointsOut)
def random_points(n: int = Query(1, ge=1, le=500)) -> RandomPointsOut:
    """Sample lat/lon from street graph nodes (for data loaders / tests)."""
    _require_graph()
    pairs = _graph.random_node_latlons(n)
    return RandomPointsOut(
        points=[RandomPointOut(lat=lat, lon=lon) for lat, lon in pairs],
    )


@routing_api.get("/ready", response_model=None)
def readiness():
    if not _graph.ready:
        payload: dict[str, object] = {"ready": False}
        if _graph.load_error:
            payload["detail"] = _graph.load_error
        else:
            payload["detail"] = "loading"
        return JSONResponse(payload, status_code=503)
    return {"ready": True, "service": "routing"}


@routing_api.post("/v1/shortest-path", response_model=ShortestPathOut)
def shortest_path(
    body: ShortestPathIn,
    include_geometry: bool = Query(True),
) -> ShortestPathOut:
    _require_graph()
    cache_order_id = body.order_id
    cache_customer_id = body.customer_id
    cache_food_place_id = body.food_place_id
    effective_include_geometry = include_geometry or (
        cache_customer_id is not None and cache_food_place_id is not None
    )
    try:
        dist, path, coords = _graph.shortest_path(
            body.origin.lat,
            body.origin.lng,
            body.destination.lat,
            body.destination.lng,
            include_geometry=effective_include_geometry,
        )
    except nx.NetworkXNoPath:
        return ShortestPathOut(error="no_path_between_points")
    if dist != dist:  # NaN
        return ShortestPathOut(error="could_not_measure_route")
    if (
        cache_customer_id is not None
        and cache_food_place_id is not None
        and len(path) > 0
        and len(coords) > 0
    ):
        _graph.cache_route(
            customer_id=int(cache_customer_id),
            food_place_id=int(cache_food_place_id),
            order_id=(int(cache_order_id) if cache_order_id is not None else None),
            distance_m=float(dist),
            node_ids=path,
            coordinates=coords,
        )
    out_path = path if include_geometry else []
    out_coords = coords if include_geometry else []
    return ShortestPathOut(
        distance_m=float(dist), node_ids=out_path, coordinates=out_coords
    )


@routing_api.post("/v1/nearest-courier", response_model=NearestCourierOut)
def nearest_courier(
    body: NearestCourierIn,
    include_geometry: bool = Query(True),
) -> NearestCourierOut:
    _require_graph()
    tuples = [
        (c.courier_id, c.lat, c.lng) for c in body.candidates
    ]
    best_id, dist, path, coords = _graph.nearest_courier_from_restaurant(
        body.restaurant.lat,
        body.restaurant.lng,
        tuples,
        include_geometry=include_geometry,
    )
    if best_id is None:
        return NearestCourierOut(error="no_reachable_courier")
    return NearestCourierOut(
        courier_id=best_id,
        distance_m=float(dist) if dist is not None else None,
        node_ids=path,
        coordinates=coords,
    )


@routing_api.get("/v1/get-route", response_model=CachedRouteOut)
def get_route(
    order_id: int | None = Query(default=None),
    customer_id: int | None = Query(default=None),
    food_place_id: int | None = Query(default=None),
) -> CachedRouteOut:
    by_order = order_id is not None
    by_pair = customer_id is not None or food_place_id is not None
    if by_order and by_pair:
        raise HTTPException(
            status_code=422,
            detail="Use either order_id or customer_id+food_place_id, not both",
        )
    if not by_order and not by_pair:
        raise HTTPException(
            status_code=422,
            detail="Provide order_id or customer_id+food_place_id",
        )

    if by_order:
        assert order_id is not None
        base = _ordering_base_url()
        order = _ordering_get_json(base, f"/orders/{int(order_id)}")
        cid = int(order.get("customer_id") or 0)
        fid = int(order.get("food_place_id") or 0)
        route_status = str(order.get("route_status") or "calculating").strip().lower()
        route_error = str(order.get("route_error") or "").strip() or None
        if cid <= 0 or fid <= 0:
            raise HTTPException(status_code=404, detail="order missing customer_id/food_place_id")
        if route_status == "calculating":
            return JSONResponse(
                status_code=202,
                content=CachedRouteOut(
                    route_status="calculating",
                    order_id=int(order_id),
                    customer_id=cid,
                    food_place_id=fid,
                    lookup="order_id",
                    cache_hit=False,
                ).model_dump(),
            )
        if route_status == "error":
            raise HTTPException(status_code=424, detail=route_error or "route calculation failed")
        payload = _get_route_payload(f"order#{int(order_id)}") or _get_route_payload(f"pair#{fid}#{cid}")
        if payload is None:
            raise HTTPException(status_code=404, detail="route payload not found")
        return CachedRouteOut(
            route_status="calculated",
            order_id=int(order_id),
            customer_id=int(payload.get("customer_id") or cid),
            food_place_id=int(payload.get("food_place_id") or fid),
            distance_m=float(payload.get("distance_m") or 0.0),
            node_ids=[int(n) for n in (payload.get("node_ids") or [])],
            coordinates=[dict(p) for p in (payload.get("coordinates") or [])],
            created_at=str(payload.get("updated_at") or ""),
            lookup="order_id",
            cache_hit=True,
        )

    assert customer_id is not None and food_place_id is not None
    payload = _get_route_payload(f"pair#{int(food_place_id)}#{int(customer_id)}")
    if payload is None:
        base = _ordering_base_url()
        _ordering_get_json(base, f"/customers/{int(customer_id)}")
        _ordering_get_json(base, f"/food-places/{int(food_place_id)}")
        return JSONResponse(
            status_code=202,
            content=CachedRouteOut(
                route_status="calculating",
                customer_id=int(customer_id),
                food_place_id=int(food_place_id),
                lookup="pair",
                cache_hit=False,
            ).model_dump(),
        )
    return CachedRouteOut(
        route_status="calculated",
        customer_id=int(payload.get("customer_id") or int(customer_id)),
        food_place_id=int(payload.get("food_place_id") or int(food_place_id)),
        order_id=(int(payload["order_id"]) if payload.get("order_id") is not None else None),
        distance_m=float(payload.get("distance_m") or 0.0),
        node_ids=[int(n) for n in (payload.get("node_ids") or [])],
        coordinates=[dict(p) for p in (payload.get("coordinates") or [])],
        created_at=str(payload.get("updated_at") or ""),
        lookup="pair",
        cache_hit=True,
    )


@routing_api.get("/")
def mounted_root() -> dict[str, str]:
    return {"service": "routing", "detail": "OSMnx + Dijkstra (São Paulo default)"}


app.mount("/routing", routing_api)
