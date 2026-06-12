"""SageMaker entry point: delivery time regression."""

from __future__ import annotations

import argparse
import os

import joblib
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error


def model_fn(model_dir: str):
    return joblib.load(os.path.join(model_dir, "model.joblib"))


def predict_fn(input_data, model):
    import numpy as np

    if isinstance(input_data, (bytes, str)):
        import json

        rows = json.loads(input_data)
        if isinstance(rows, dict):
            rows = [rows]
    else:
        rows = input_data
    X = pd.DataFrame(rows)
    cols = ["food_place_id", "hour", "weekday"]
    for c in cols:
        if c not in X.columns:
            X[c] = 0
    pred = model.predict(X[cols])
    return {"predicted_seconds": float(pred[0]) if len(pred) == 1 else pred.tolist()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=str, default=os.environ.get("SM_CHANNEL_TRAIN"))
    parser.add_argument("--model-dir", type=str, default=os.environ.get("SM_MODEL_DIR"))
    args = parser.parse_args()

    train_path = os.path.join(args.train, "train.csv")
    df = pd.read_csv(train_path)
    feature_cols = ["food_place_id", "hour", "weekday"]
    X = df[feature_cols]
    y = df["delivery_seconds"]
    X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=42)
    model = GradientBoostingRegressor(n_estimators=50, max_depth=4, random_state=42)
    model.fit(X_train, y_train)
    mae = mean_absolute_error(y_val, model.predict(X_val))
    print(f"validation MAE seconds: {mae:.1f}")
    joblib.dump(model, os.path.join(args.model_dir, "model.joblib"))
