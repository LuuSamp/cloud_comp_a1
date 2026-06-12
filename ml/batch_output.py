"""Parse SageMaker Batch Transform output (.out) into row dicts."""

from __future__ import annotations

import io
import json
from typing import Any

import pandas as pd


def parse_batch_transform_body(body: str) -> list[dict[str, Any]]:
    """Accept CSV, a JSON array, or JSONL from sklearn inference scripts."""
    text = body.strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
        except json.JSONDecodeError:
            pass

    if not text.startswith("{") and not text.startswith("["):
        try:
            df = pd.read_csv(io.StringIO(text))
            if len(df) > 0:
                return df.to_dict(orient="records")
        except Exception:
            pass

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
        elif isinstance(parsed, list):
            rows.extend(item for item in parsed if isinstance(item, dict))
    return rows


def parse_jsonl_body(body: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows
