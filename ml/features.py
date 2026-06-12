"""Shared feature engineering from analytics events (used by dashboard prep and training)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

STATUS_LABELS = {
    1: "CONFIRMED",
    2: "PREPARING",
    3: "READY_FOR_PICKUP",
    4: "PICKED_UP",
    5: "IN_TRANSIT",
    6: "DELIVERED",
}

GRID_SIZE_DEG = 0.01  # ~1 km at São Paulo latitude


def normalize_events(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "type" not in df.columns:
        if "orderStatusId" in df.columns or "orderId" in df.columns:
            df["type"] = "order_status"
            if "orderId" in df.columns and "order_id" not in df.columns:
                df["order_id"] = df["orderId"]
            if "orderStatusId" in df.columns and "status_id" not in df.columns:
                df["status_id"] = df["orderStatusId"]
        elif "courierId" in df.columns or "lat" in df.columns:
            df["type"] = "courier_position"
            if "courierId" in df.columns and "courier_id" not in df.columns:
                df["courier_id"] = df["courierId"]
        else:
            return pd.DataFrame()
    if "timestamp" not in df.columns:
        return pd.DataFrame()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce", format="mixed")
    return df.dropna(subset=["timestamp", "type"])


def delivery_time_labels(events: pd.DataFrame) -> pd.DataFrame:
    """Per-order total delivery seconds (status 1 -> 6)."""
    orders = events[events["type"] == "order_status"].copy()
    if orders.empty:
        return pd.DataFrame()
    first = orders[orders["status_id"] == 1].groupby("order_id")["timestamp"].min()
    last = orders[orders["status_id"] == 6].groupby("order_id")["timestamp"].max()
    merged = pd.concat([first, last], axis=1, keys=["first_ts", "last_ts"]).dropna()
    merged["delivery_seconds"] = (merged["last_ts"] - merged["first_ts"]).dt.total_seconds()
    return merged.reset_index()


def delivery_features(events: pd.DataFrame) -> pd.DataFrame:
    """Feature rows for delivery-time model — one row per completed order."""
    labels = delivery_time_labels(events)
    if labels.empty:
        return pd.DataFrame()
    orders = events[events["type"] == "order_status"].copy()
    first_evt = (
        orders[orders["status_id"] == 1]
        .sort_values("timestamp")
        .groupby("order_id")
        .first()
        .reset_index()
    )
    merged = labels.merge(
        first_evt[["order_id", "food_place_id", "customer_id", "timestamp"]],
        on="order_id",
        how="inner",
    )
    merged["hour"] = merged["timestamp"].dt.hour
    merged["weekday"] = merged["timestamp"].dt.dayofweek
    merged["food_place_id"] = merged["food_place_id"].fillna(0).astype(int)
    return merged[
        ["order_id", "food_place_id", "hour", "weekday", "delivery_seconds"]
    ]


def lat_lon_to_grid(lat: float, lon: float) -> str:
    glat = int(lat / GRID_SIZE_DEG)
    glon = int(lon / GRID_SIZE_DEG)
    return f"{glat}_{glon}"


def demand_features(events: pd.DataFrame, food_places: pd.DataFrame | None = None) -> pd.DataFrame:
    """Aggregate order counts by region grid, hour, weekday."""
    orders = events[events["type"] == "order_status"].copy()
    orders = orders[orders["status_id"] == 1]
    if orders.empty:
        return pd.DataFrame()
    orders["hour"] = orders["timestamp"].dt.hour
    orders["weekday"] = orders["timestamp"].dt.dayofweek
    if food_places is not None and not food_places.empty and "food_place_id" in orders.columns:
        fp = food_places.set_index("food_place_id")[["lat", "lon"]]
        orders = orders.merge(fp, left_on="food_place_id", right_index=True, how="left")
        orders["region_grid"] = orders.apply(
            lambda r: lat_lon_to_grid(float(r.get("lat") or 0), float(r.get("lon") or 0)),
            axis=1,
        )
    else:
        orders["region_grid"] = orders["food_place_id"].fillna(0).astype(int).astype(str)
    agg = (
        orders.groupby(["region_grid", "hour", "weekday"])
        .size()
        .reset_index(name="order_count")
    )
    return agg


def anomaly_features(events: pd.DataFrame, window_hours: int = 1) -> pd.DataFrame:
    """Rolling operational KPI vectors for anomaly detection."""
    orders = events[events["type"] == "order_status"].copy()
    if orders.empty:
        return pd.DataFrame()
    orders = orders.set_index("timestamp").sort_index()
    hourly_orders = orders.resample(f"{window_hours}h").order_id.nunique().rename("orders_per_hour")
    labels = delivery_time_labels(events)
    if not labels.empty:
        labels = labels.set_index("first_ts")
        avg_delivery = labels["delivery_seconds"].resample(f"{window_hours}h").mean().rename(
            "avg_delivery_seconds"
        )
    else:
        avg_delivery = pd.Series(dtype=float, name="avg_delivery_seconds")
    positions = events[events["type"] == "courier_position"].copy()
    if not positions.empty:
        positions = positions.set_index("timestamp").sort_index()
        pos_rate = positions.resample(f"{window_hours}h").size().rename("position_updates")
    else:
        pos_rate = pd.Series(dtype=int, name="position_updates")
    kpi = pd.concat([hourly_orders, avg_delivery, pos_rate], axis=1).fillna(0)
    kpi = kpi.reset_index().rename(columns={"timestamp": "window_start"})
    return kpi


def heuristic_delivery_seconds(features: dict[str, Any], historical_mean: float = 1800.0) -> float:
    """Fallback when SageMaker endpoint is unavailable."""
    base = historical_mean
    hour = int(features.get("hour") or 12)
    if 11 <= hour <= 14 or 18 <= hour <= 21:
        base *= 1.2
    fp = int(features.get("food_place_id") or 0)
    return base + (fp % 7) * 30.0
