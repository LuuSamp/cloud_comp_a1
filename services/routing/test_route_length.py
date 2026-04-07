"""Unit test for route length helper (no OSM download). Run: python -m unittest test_route_length -v"""

from __future__ import annotations

import unittest

import networkx as nx

from route_metrics import route_length_m


class TestRouteLength(unittest.TestCase):
    def test_simple_path(self) -> None:
        G = nx.MultiDiGraph()
        G.add_edge(1, 2, length=100.0)
        G.add_edge(2, 3, length=50.5)
        self.assertAlmostEqual(route_length_m(G, [1, 2, 3]), 150.5)

    def test_parallel_edges_uses_minimum_per_step(self) -> None:
        G = nx.MultiDiGraph()
        G.add_edge(1, 2, length=999.0, key=0)
        G.add_edge(1, 2, length=10.0, key=1)
        G.add_edge(2, 3, length=5.0)
        self.assertAlmostEqual(route_length_m(G, [1, 2, 3]), 15.0)


if __name__ == "__main__":
    unittest.main()
