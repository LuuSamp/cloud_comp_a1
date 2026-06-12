"""SageMaker batch/real-time inference for demand model."""

from __future__ import annotations

import io
import os

import joblib
import pandas as pd


def model_fn(model_dir: str):
    return joblib.load(os.path.join(model_dir, "model.joblib"))


def input_fn(request_body, content_type):
    if content_type in ("text/csv", "application/csv"):
        return pd.read_csv(io.StringIO(request_body.decode("utf-8") if isinstance(request_body, bytes) else request_body))
    raise ValueError(f"Unsupported content type: {content_type}")


def predict_fn(input_data, bundle):
    model = bundle["model"]
    le = bundle["label_encoder"]
    df = input_data.copy()
    df["region_enc"] = le.transform(df["region_grid"].astype(str))
    df["predicted_order_count"] = model.predict(df[["region_enc", "hour", "weekday"]])
    return df


def output_fn(prediction, accept):
    if accept in ("text/csv", "application/csv", "*/*"):
        return prediction.to_csv(index=False), "text/csv"
    return prediction.to_json(orient="records"), "application/json"
