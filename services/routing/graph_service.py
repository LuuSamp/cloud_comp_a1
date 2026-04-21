"""OSMnx street graph loading, snapping, and Dijkstra shortest paths (metric CRS)."""

from __future__ import annotations

import logging
import os
import random
import re
import tempfile
import threading
import time
from collections import OrderedDict

import boto3
import geopandas as gpd
import networkx as nx
import osmnx as ox
from botocore.exceptions import ClientError
from shapely.geometry import Point

from route_metrics import route_length_m

log = logging.getLogger(__name__)

_MAX_SLUG_LEN = 180


def _aws_region() -> str:
    return (
        (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
        .strip()
        or "us-east-1"
    )


def graph_s3_object_key(place: str, network_type: str) -> str:
    """S3 object key: graphs/{slugified_place}__{network_type}.graphml (UTF-8 safe).

    Deploy seeds a local GraphML to this key in tools/s3_routing_graph_infra.py
    (graph_s3_object_key_for_seed); keep the slug rules identical.
    """
    raw = f"{place}__{network_type}".lower()
    slug = re.sub(r"[^\w]+", "_", raw, flags=re.UNICODE)
    slug = slug.strip("_")
    if len(slug) > _MAX_SLUG_LEN:
        slug = slug[:_MAX_SLUG_LEN].rstrip("_")
    if not slug:
        slug = "graph"
    return f"graphs/{slug}.graphml"

# OSMnx configures its own HTTP user agent for API etiquette.
_DEFAULT_PLACE = "São Paulo, SP, Brazil"
_DEFAULT_NETWORK = "drive"
_ROUTE_CACHE_MAX_ENTRIES = max(
    100,
    int((os.environ.get("ROUTING_ROUTE_CACHE_MAX_ENTRIES") or "5000").strip()),
)
_ROUTE_CACHE_TTL_S = max(
    60.0,
    float((os.environ.get("ROUTING_ROUTE_CACHE_TTL_S") or "3600").strip()),
)


def _project_point(lng: float, lat: float, crs: str) -> tuple[float, float]:
    pt = gpd.GeoSeries([Point(lng, lat)], crs="EPSG:4326")
    t = pt.to_crs(crs)
    geom = t.iloc[0]
    return float(geom.x), float(geom.y)


def _project_points(latlons: list[tuple[float, float]], crs: str) -> tuple[list[float], list[float]]:
    if not latlons:
        return [], []
    pts = gpd.GeoSeries(
        [Point(lng, lat) for lat, lng in latlons],
        crs="EPSG:4326",
    )
    transformed = pts.to_crs(crs)
    xs = [float(g.x) for g in transformed]
    ys = [float(g.y) for g in transformed]
    return xs, ys


class RoutingGraph:
    """Thread-safe wrapper: projected graph for routing; original for node lat/lng."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._G_orig: nx.MultiDiGraph | None = None
        self._G_proj: nx.MultiDiGraph | None = None
        self._load_error: str | None = None
        self._route_cache_by_pair: OrderedDict[
            tuple[int, int], dict[str, object]
        ] = OrderedDict()
        self._route_cache_by_order: dict[int, tuple[int, int]] = {}

    @property
    def ready(self) -> bool:
        return self._G_proj is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def _try_download_graph_from_s3(
        self, bucket: str, key: str
    ) -> nx.MultiDiGraph | None:
        s3 = boto3.client("s3", region_name=_aws_region())
        fd, path = tempfile.mkstemp(suffix=".graphml")
        os.close(fd)
        try:
            try:
                s3.download_file(bucket, key, path)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    log.info(
                        "No graph in S3 s3://%s/%s (%s); will fetch from OSM",
                        bucket,
                        key,
                        code,
                    )
                    return None
                raise
            G = ox.load_graphml(path)
            log.info(
                "Loaded graph from S3 s3://%s/%s (%s nodes)",
                bucket,
                key,
                G.number_of_nodes(),
            )
            return G
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _try_upload_graph_to_s3(
        self,
        bucket: str,
        key: str,
        G: nx.MultiDiGraph,
    ) -> None:
        fd, path = tempfile.mkstemp(suffix=".graphml")
        os.close(fd)
        try:
            ox.save_graphml(G, path)
            boto3.client("s3", region_name=_aws_region()).upload_file(
                path, bucket, key
            )
            log.info("Cached graph to s3://%s/%s", bucket, key)
        except Exception as exc:
            log.warning("S3 graph upload failed (service still runs): %s", exc)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def load_blocking(
        self,
        place: str | None = None,
        network_type: str | None = None,
    ) -> None:
        place = place or os.environ.get("OSMNX_PLACE", _DEFAULT_PLACE)
        network_type = network_type or os.environ.get(
            "ROUTING_NETWORK_TYPE", _DEFAULT_NETWORK
        )
        with self._lock:
            self._load_error = None
            self._G_orig = None
            self._G_proj = None
        bucket = (os.environ.get("ROUTING_GRAPH_S3_BUCKET") or "").strip()
        key = graph_s3_object_key(place, network_type)
        log.info(
            "Street graph place=%r network_type=%r s3_bucket=%r",
            place,
            network_type,
            bucket or "(none)",
        )
        try:
            ox.settings.use_cache = True
            G: nx.MultiDiGraph | None = None
            if bucket:
                G = self._try_download_graph_from_s3(bucket, key)
            if G is None:
                log.info("Fetching graph from OSMnx (place query / cache)")
                G = ox.graph_from_place(place, network_type=network_type)
                if bucket:
                    self._try_upload_graph_to_s3(bucket, key, G)
            G_proj = ox.project_graph(G.copy())
            with self._lock:
                self._G_orig = G
                self._G_proj = G_proj
            log.info(
                "Graph ready: %s nodes, %s edges",
                G_proj.number_of_nodes(),
                G_proj.number_of_edges(),
            )
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            log.exception("Graph load failed: %s", msg)
            with self._lock:
                self._load_error = msg
                self._G_orig = None
                self._G_proj = None

    def _graphs(self) -> tuple[nx.MultiDiGraph, nx.MultiDiGraph]:
        if self._G_proj is None or self._G_orig is None:
            raise RuntimeError("Graph not loaded")
        return self._G_orig, self._G_proj

    def _prune_route_cache_locked(self, now: float) -> None:
        # OrderedDict keeps insertion/access order; pop oldest when oversize.
        while self._route_cache_by_pair:
            first_key = next(iter(self._route_cache_by_pair))
            entry = self._route_cache_by_pair[first_key]
            created_at = float(entry.get("created_at_epoch_s", now))
            if now - created_at <= _ROUTE_CACHE_TTL_S:
                break
            self._route_cache_by_pair.pop(first_key, None)
            stale_orders = [
                oid
                for oid, pair in self._route_cache_by_order.items()
                if pair == first_key
            ]
            for oid in stale_orders:
                self._route_cache_by_order.pop(oid, None)
        while len(self._route_cache_by_pair) > _ROUTE_CACHE_MAX_ENTRIES:
            evicted_pair, _entry = self._route_cache_by_pair.popitem(last=False)
            stale_orders = [
                oid
                for oid, pair in self._route_cache_by_order.items()
                if pair == evicted_pair
            ]
            for oid in stale_orders:
                self._route_cache_by_order.pop(oid, None)

    def cache_route(
        self,
        *,
        customer_id: int,
        food_place_id: int,
        distance_m: float,
        node_ids: list[int],
        coordinates: list[dict[str, float]],
        order_id: int | None = None,
    ) -> None:
        pair_key = (food_place_id, customer_id)
        now = time.time()
        with self._lock:
            self._route_cache_by_pair[pair_key] = {
                "food_place_id": food_place_id,
                "customer_id": customer_id,
                "distance_m": float(distance_m),
                "node_ids": [int(n) for n in node_ids],
                "coordinates": [
                    {"lat": float(c["lat"]), "lng": float(c["lng"])}
                    for c in coordinates
                ],
                "created_at_epoch_s": now,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            }
            self._route_cache_by_pair.move_to_end(pair_key, last=True)
            if order_id is not None:
                self._route_cache_by_order[int(order_id)] = pair_key
            self._prune_route_cache_locked(now)

    def get_cached_route_by_pair(
        self,
        *,
        customer_id: int,
        food_place_id: int,
    ) -> dict[str, object] | None:
        pair_key = (food_place_id, customer_id)
        now = time.time()
        with self._lock:
            self._prune_route_cache_locked(now)
            entry = self._route_cache_by_pair.get(pair_key)
            if entry is None:
                return None
            self._route_cache_by_pair.move_to_end(pair_key, last=True)
            return {
                "food_place_id": int(entry["food_place_id"]),
                "customer_id": int(entry["customer_id"]),
                "distance_m": float(entry["distance_m"]),
                "node_ids": [int(n) for n in entry["node_ids"]],  # type: ignore[index]
                "coordinates": [
                    {"lat": float(c["lat"]), "lng": float(c["lng"])}
                    for c in (entry["coordinates"])  # type: ignore[index]
                ],
                "created_at": str(entry["created_at"]),
            }

    def get_cached_route_by_order(
        self,
        *,
        order_id: int,
    ) -> dict[str, object] | None:
        now = time.time()
        with self._lock:
            self._prune_route_cache_locked(now)
            pair_key = self._route_cache_by_order.get(int(order_id))
            if pair_key is None:
                return None
            entry = self._route_cache_by_pair.get(pair_key)
            if entry is None:
                self._route_cache_by_order.pop(int(order_id), None)
                return None
            self._route_cache_by_pair.move_to_end(pair_key, last=True)
            return {
                "order_id": int(order_id),
                "food_place_id": int(entry["food_place_id"]),
                "customer_id": int(entry["customer_id"]),
                "distance_m": float(entry["distance_m"]),
                "node_ids": [int(n) for n in entry["node_ids"]],  # type: ignore[index]
                "coordinates": [
                    {"lat": float(c["lat"]), "lng": float(c["lng"])}
                    for c in (entry["coordinates"])  # type: ignore[index]
                ],
                "created_at": str(entry["created_at"]),
            }

    def random_node_latlons(self, n: int) -> list[tuple[float, float]]:
        """Sample up to n (lat, lon) pairs from unprojected graph nodes."""
        Go, _ = self._graphs()
        nodes = list(Go.nodes)
        if not nodes or n <= 0:
            return []
        k = min(n, len(nodes))
        picked = random.sample(nodes, k)
        out: list[tuple[float, float]] = []
        for node in picked:
            data = Go.nodes[node]
            out.append((float(data["y"]), float(data["x"])))
        return out

    def snap(self, lat: float, lng: float) -> int:
        _, Gp = self._graphs()
        crs = Gp.graph.get("crs")
        if crs is None:
            raise RuntimeError("Projected graph has no CRS")
        crs_s = str(crs)
        x, y = _project_point(lng, lat, crs_s)
        return ox.distance.nearest_nodes(Gp, x, y)

    def snap_many(self, latlons: list[tuple[float, float]]) -> list[int]:
        _, Gp = self._graphs()
        crs = Gp.graph.get("crs")
        if crs is None:
            raise RuntimeError("Projected graph has no CRS")
        xs, ys = _project_points(latlons, str(crs))
        if not xs:
            return []
        nodes = ox.distance.nearest_nodes(Gp, xs, ys)
        if isinstance(nodes, list):
            return [int(n) for n in nodes]
        return [int(n) for n in nodes.tolist()]

    def shortest_path(
        self,
        o_lat: float,
        o_lng: float,
        d_lat: float,
        d_lng: float,
        *,
        include_geometry: bool = True,
    ) -> tuple[float, list[int], list[dict[str, float]]]:
        """Return distance (m), node path, coordinates (lat,lng per node)."""
        Go, Gp = self._graphs()
        orig = self.snap(o_lat, o_lng)
        dest = self.snap(d_lat, d_lng)
        if not include_geometry:
            dist = float(
                nx.shortest_path_length(
                    Gp, orig, dest, weight="length", method="dijkstra"
                )
            )
            return dist, [], []
        path: list[int] = nx.shortest_path(
            Gp, orig, dest, weight="length", method="dijkstra"
        )
        dist = route_length_m(Gp, path)
        coords = [
            {"lat": float(Go.nodes[n]["y"]), "lng": float(Go.nodes[n]["x"])}
            for n in path
        ]
        return dist, path, coords

    def nearest_courier_from_restaurant(
        self,
        r_lat: float,
        r_lng: float,
        candidates: list[tuple[int, float, float]],
        *,
        include_geometry: bool = True,
    ) -> tuple[int | None, float | None, list[int] | None, list[dict[str, float]] | None]:
        """
        Single-source Dijkstra from restaurant snap node; pick the reachable candidate
        with minimum graph distance (same as expanding until the best courier is known
        when all edge weights are non-negative).
        """
        if not candidates:
            return None, None, None, None
        Go, Gp = self._graphs()
        source = self.snap(r_lat, r_lng)
        lengths = nx.single_source_dijkstra_path_length(
            Gp, source, weight="length", cutoff=None
        )
        best_id: int | None = None
        best_dist: float | None = None
        best_node: int | None = None
        ids = [cid for cid, _, _ in candidates]
        nodes = self.snap_many([(lat, lng) for _, lat, lng in candidates])
        for cid, node in zip(ids, nodes):
            d = lengths.get(node)
            if d is None:
                continue
            if best_dist is None or d < best_dist:
                best_dist = float(d)
                best_id = cid
                best_node = node
        if best_id is None or best_node is None:
            return None, None, None, None
        if include_geometry:
            path = nx.shortest_path(
                Gp, source, best_node, weight="length", method="dijkstra"
            )
            coords = [
                {"lat": float(Go.nodes[n]["y"]), "lng": float(Go.nodes[n]["x"])}
                for n in path
            ]
            dist = route_length_m(Gp, path)
            return best_id, dist, path, coords
        assert best_dist is not None
        return best_id, best_dist, None, None
