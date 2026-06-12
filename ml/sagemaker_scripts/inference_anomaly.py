"""SageMaker batch/real-time inference for anomaly model."""

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
    cols = bundle["feature_cols"]
    df = input_data.copy()
    for c in cols:
        if c not in df.columns:
            df[c] = 0
    scores = model.predict(df[cols].fillna(0))
    df["anomaly_score"] = scores.astype(int)
    df["is_anomaly"] = df["anomaly_score"] == -1
    return df


def output_fn(prediction, accept):
    if accept in ("text/csv", "application/csv", "*/*"):
        return prediction.to_csv(index=False), "text/csv"
    return prediction.to_json(orient="records"), "application/json"
