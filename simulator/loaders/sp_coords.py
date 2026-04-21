"""São Paulo lat/lon sampling: routing graph API or bounding-box fallback."""

from __future__ import annotations

import json
import random
import urllib.error
import urllib.request

# Greater São Paulo approximate bounds (WGS84)
_SP_LAT_MIN, _SP_LAT_MAX = -23.80, -23.50
_SP_LON_MIN, _SP_LON_MAX = -46.95, -46.35


def _bbox_points(n: int, rng: random.Random) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for _ in range(n):
        lat = rng.uniform(_SP_LAT_MIN, _SP_LAT_MAX)
        lon = rng.uniform(_SP_LON_MIN, _SP_LON_MAX)
        out.append((lat, lon))
    return out


def sample_coordinates(
    n: int,
    routing_base_url: str | None,
    *,
    timeout: float = 30.0,
    rng: random.Random | None = None,
) -> list[tuple[float, float]]:
    """
    Try GET {routing_base_url}/routing/v1/random-points?n=...
    On non-200 or error, fall back to random points in a São Paulo bbox.
    """
    rng = rng or random
    base = (routing_base_url or "").strip().rstrip("/")
    if base and n > 0:
        path = f"/routing/v1/random-points?n={n}"
        url = base + path
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return _bbox_points(n, rng)
                raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            pts = data.get("points") or []
            coords: list[tuple[float, float]] = []
            for p in pts:
                if isinstance(p, dict) and "lat" in p and "lon" in p:
                    coords.append((float(p["lat"]), float(p["lon"])))
            if len(coords) >= n:
                return coords[:n]
            # partial result: pad with bbox
            extra = _bbox_points(n - len(coords), rng)
            return coords + extra
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
            pass
    return _bbox_points(n, rng)
