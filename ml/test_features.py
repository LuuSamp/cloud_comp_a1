"""Unit tests for ml.features."""

from __future__ import annotations

import pandas as pd

from ml.features import delivery_features, demand_features, normalize_events


def test_normalize_and_delivery_features():
    rows = [
        {
            "type": "order_status",
            "order_id": 1,
            "status_id": 1,
            "timestamp": "2026-01-01T10:00:00+00:00",
            "food_place_id": 3,
        },
        {
            "type": "order_status",
            "order_id": 1,
            "status_id": 6,
            "timestamp": "2026-01-01T10:30:00+00:00",
            "food_place_id": 3,
        },
    ]
    events = normalize_events(rows)
    delivery = delivery_features(events)
    assert len(delivery) == 1
    assert delivery.iloc[0]["delivery_seconds"] == 1800.0


def test_demand_features():
    rows = [
        {
            "type": "order_status",
            "order_id": i,
            "status_id": 1,
            "timestamp": f"2026-01-01T{10 + (i % 3)}:00:00+00:00",
            "food_place_id": 1,
        }
        for i in range(5)
    ]
    events = normalize_events(rows)
    demand = demand_features(events)
    assert not demand.empty
    assert "order_count" in demand.columns
