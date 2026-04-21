"""Shared loop helpers and metrics for simulation services."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class SimulationMetrics:
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests: int = 0
    errors: int = 0

    def add_requests(self, n: int = 1) -> None:
        with self.lock:
            self.requests += n

    def add_errors(self, n: int = 1) -> None:
        with self.lock:
            self.errors += n

    def snapshot(self) -> tuple[int, int]:
        with self.lock:
            return self.requests, self.errors


def sleep_until(next_tick: float) -> float:
    now = time.monotonic()
    wait = max(0.0, next_tick - now)
    if wait > 0:
        time.sleep(wait)
    return time.monotonic()
