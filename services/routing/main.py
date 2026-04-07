"""DijkFood routing service: OSMnx graph + Dijkstra. ALB path prefix: /routing*"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

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
def shortest_path(body: ShortestPathIn) -> ShortestPathOut:
    _require_graph()
    try:
        dist, path, coords = _graph.shortest_path(
            body.origin.lat,
            body.origin.lng,
            body.destination.lat,
            body.destination.lng,
        )
    except nx.NetworkXNoPath:
        return ShortestPathOut(error="no_path_between_points")
    if dist != dist:  # NaN
        return ShortestPathOut(error="could_not_measure_route")
    return ShortestPathOut(
        distance_m=float(dist), node_ids=path, coordinates=coords
    )


@routing_api.post("/v1/nearest-courier", response_model=NearestCourierOut)
def nearest_courier(body: NearestCourierIn) -> NearestCourierOut:
    _require_graph()
    tuples = [
        (c.courier_id, c.lat, c.lng) for c in body.candidates
    ]
    best_id, dist, path, coords = _graph.nearest_courier_from_restaurant(
        body.restaurant.lat,
        body.restaurant.lng,
        tuples,
    )
    if best_id is None:
        return NearestCourierOut(error="no_reachable_courier")
    return NearestCourierOut(
        courier_id=best_id,
        distance_m=float(dist) if dist is not None else None,
        node_ids=path,
        coordinates=coords,
    )


@routing_api.get("/")
def mounted_root() -> dict[str, str]:
    return {"service": "routing", "detail": "OSMnx + Dijkstra (São Paulo default)"}


app.mount("/routing", routing_api)
