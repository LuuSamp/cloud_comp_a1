"""Shared runtime config for simulation service runners."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SimulationRuntime:
    ordering_base_url: str
    tracking_base_url: str
    debug_http: bool = False

    @staticmethod
    def from_env(
        *,
        ordering_base_url: str | None = None,
        tracking_base_url: str | None = None,
        debug_http: bool = False,
    ) -> "SimulationRuntime":
        base = (ordering_base_url or os.environ.get("BASE_URL") or "").strip()
        if not base:
            raise ValueError("BASE_URL (ordering URL) is required")
        tracking = (tracking_base_url or os.environ.get("TRACKING_BASE_URL") or "").strip()
        if not tracking:
            tracking = f"{base.rstrip('/')}/tracking"
        return SimulationRuntime(
            ordering_base_url=base.rstrip("/"),
            tracking_base_url=tracking.rstrip("/"),
            debug_http=debug_http,
        )
