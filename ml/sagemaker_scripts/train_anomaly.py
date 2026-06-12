"""SageMaker entry point: operational anomaly detection."""

from __future__ import annotations

import argparse
import os

import joblib
import pandas as pd
from sklearn.ensemble import IsolationForest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=str, default=os.environ.get("SM_CHANNEL_TRAIN"))
    parser.add_argument("--model-dir", type=str, default=os.environ.get("SM_MODEL_DIR"))
    args = parser.parse_args()

    df = pd.read_csv(os.path.join(args.train, "train.csv"))
    feature_cols = ["orders_per_hour", "avg_delivery_seconds", "position_updates"]
    for c in feature_cols:
        if c not in df.columns:
            df[c] = 0
    X = df[feature_cols].fillna(0)
    model = IsolationForest(contamination=0.1, random_state=42)
    model.fit(X)
    joblib.dump({"model": model, "feature_cols": feature_cols}, os.path.join(args.model_dir, "model.joblib"))
