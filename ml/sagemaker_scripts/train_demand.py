"""SageMaker entry point: demand forecasting by region/hour."""

from __future__ import annotations

import argparse
import os

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error
from sklearn.preprocessing import LabelEncoder


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=str, default=os.environ.get("SM_CHANNEL_TRAIN"))
    parser.add_argument("--model-dir", type=str, default=os.environ.get("SM_MODEL_DIR"))
    args = parser.parse_args()

    df = pd.read_csv(os.path.join(args.train, "train.csv"))
    le = LabelEncoder()
    df["region_enc"] = le.fit_transform(df["region_grid"].astype(str))
    feature_cols = ["region_enc", "hour", "weekday"]
    X = df[feature_cols]
    y = df["order_count"]
    X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=42)
    model = RandomForestRegressor(n_estimators=50, max_depth=6, random_state=42)
    model.fit(X_train, y_train)
    mae = mean_absolute_error(y_val, model.predict(X_val))
    print(f"validation MAE order_count: {mae:.2f}")
    joblib.dump({"model": model, "label_encoder": le}, os.path.join(args.model_dir, "model.joblib"))
