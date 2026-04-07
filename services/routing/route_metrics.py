"""Pure NetworkX helpers (no OSM download)."""

from __future__ import annotations

from typing import Any

import networkx as nx


def route_length_m(G: nx.MultiDiGraph, path: list[Any]) -> float:
    """Sum ``length`` along consecutive pairs, matching one edge per step in a MultiDiGraph."""
    total = 0.0
    for u, v in zip(path[:-1], path[1:]):
        if u not in G or v not in G[u]:
            return float("nan")
        best = min(
            (d.get("length", float("inf")) for d in G[u][v].values()),
            default=float("inf"),
        )
        if best is float("inf"):
            return float("nan")
        total += float(best)
    return total
