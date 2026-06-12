import os
import json
from datetime import datetime
from typing import Any
from pathlib import Path

import streamlit as st
import pandas as pd
import boto3
from botocore.exceptions import ClientError
from pyathena import connect
from dotenv import load_dotenv

from ml.batch_output import parse_batch_transform_body, parse_jsonl_body


st.set_page_config(page_title="DijkFood Analytics", layout="wide")
st.title("🍔 DijkFood - Dashboard Analítico")


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
if (ROOT / "connection.env").is_file():
    load_dotenv(ROOT / "connection.env", override=True)


# --- Data loading -------------------------------------------------
ATHENA_DB = os.environ.get("ATHENA_DB") or os.environ.get("GLUE_DATABASE")
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
S3_BUCKET_CANDIDATES = [
    (os.environ.get("DATALAKE_S3_BUCKET") or "").strip(),
    (os.environ.get("ROUTING_GRAPH_S3_BUCKET") or "").strip(),
]
S3_PREFIX = (os.environ.get("DATALAKE_EVENTS_PREFIX") or "events/").strip().lstrip("/")


def _athena_staging_dir() -> str:
    bucket = next((b for b in S3_BUCKET_CANDIDATES if b), "")
    if bucket:
        return f"s3://{bucket}/athena-results/"
    sts = boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    return f"s3://dijkfood-datalake-{account_id}-{AWS_REGION}/athena-results/"


def _resolve_events_table(db_name: str) -> str:
    glue = boto3.client("glue", region_name=AWS_REGION)
    try:
        glue.get_table(DatabaseName=db_name, Name="events")
        return "events"
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "EntityNotFoundException":
            raise
    for table in glue.get_tables(DatabaseName=db_name).get("TableList", []):
        location = (table.get("StorageDescriptor") or {}).get("Location") or ""
        if location.rstrip("/").endswith("/events"):
            return table["Name"]
    return "events"


def query_athena(query: str) -> pd.DataFrame:
    conn = connect(s3_staging_dir=_athena_staging_dir(), region_name=AWS_REGION)
    return pd.read_sql(query, conn)


def _normalize_events(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "type" not in df.columns:
        # Infer type from DynamoDB stream payload fields when Lambda did not add it.
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
    df = df.dropna(subset=["timestamp", "type"])
    return df


@st.cache_data(ttl=30)
def load_events_s3(bucket: str, prefix: str) -> pd.DataFrame:
    if not bucket:
        return pd.DataFrame()
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    rows: list[dict[str, Any]] = []
    seen_keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not key or not key.endswith(".jsonl"):
                continue
            seen_keys.append(key)
    for key in sorted(seen_keys):
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
    return _normalize_events(rows)


def load_events() -> pd.DataFrame:
    # Prefer Athena if ATHENA_DB is configured; otherwise read the S3 datalake.
    if ATHENA_DB:
        try:
            table = _resolve_events_table(ATHENA_DB)
            df = query_athena(f"SELECT * FROM {ATHENA_DB}.{table}")
            normalized = _normalize_events(df.to_dict(orient="records"))
            if not normalized.empty:
                return normalized
        except Exception as e:
            err = str(e)
            if "TABLE_NOT_FOUND" in err or "EntityNotFoundException" in err:
                st.warning(
                    "Tabela Glue `events` ainda não disponível no Athena; lendo eventos diretamente do S3."
                )
            else:
                st.error(f"Falha ao consultar Athena: {e}")
    candidates = [b for b in dict.fromkeys(S3_BUCKET_CANDIDATES) if b]
    if not candidates:
        st.warning(
            "Nenhum bucket S3 de analytics configurado. Defina DATALAKE_S3_BUCKET (ou ROUTING_GRAPH_S3_BUCKET) no ambiente."
        )
        return pd.DataFrame()

    last_error = None
    for bucket in candidates:
        try:
            df = load_events_s3(bucket, S3_PREFIX)
            if not df.empty:
                return df
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in {"NoSuchBucket", "404"}:
                continue
            last_error = f"Falha ao ler eventos do S3 s3://{bucket}/{S3_PREFIX}: {e}"
            break
        except Exception as e:
            last_error = f"Falha ao ler eventos do S3 s3://{bucket}/{S3_PREFIX}: {e}"
            break

    if last_error:
        st.error(last_error)
    return pd.DataFrame()


df = load_events()
if df.empty:
    st.info("Aguardando dados da camada analítica no S3/Athena. Gere tráfego e confira se o bucket de analytics recebeu arquivos em `events/`.")
    st.stop()


# --- Metrics ------------------------------------------------------
st.header("Indicadores Operacionais")


def volume_over_time(events: pd.DataFrame):
    e = events[events["type"] == "order_status"].copy()
    if e.empty:
        return None
    s = e.set_index("timestamp").resample("1h").order_id.count()
    return s


def avg_time_per_state(events: pd.DataFrame):
    orders = events[events["type"] == "order_status"].copy()
    if orders.empty:
        return pd.Series(dtype=float)
    orders = orders.sort_values(["order_id", "timestamp"]).copy()
    # compute durations between consecutive status events per order
    orders["next_ts"] = orders.groupby("order_id")["timestamp"].shift(-1)
    orders["duration_s"] = (orders["next_ts"] - orders["timestamp"]).dt.total_seconds()
    res = orders.groupby("status_id").duration_s.mean().sort_index()
    return res


def heatmap_by_hour_weekday(events: pd.DataFrame):
    orders = events[events["type"] == "order_status"].copy()
    if orders.empty:
        return pd.DataFrame()
    orders["hour"] = orders["timestamp"].dt.hour
    orders["weekday"] = orders["timestamp"].dt.day_name()
    pivot = orders.groupby(["weekday", "hour"]).size().unstack(fill_value=0)
    # ensure weekday order
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    pivot = pivot.reindex(days).fillna(0)
    return pivot


def top_restaurants(events: pd.DataFrame, top_n=10):
    # order events don't contain food_place by default; try to use 'detail' if it encodes place
    orders = events[events["type"] == "order_status"].copy()
    if orders.empty:
        return pd.Series(dtype=int)
    if "food_place_id" in orders.columns:
        return orders.groupby("food_place_id").order_id.nunique().nlargest(top_n)
    # fallback: try parsing food_place_id from detail
    try:
        fp = orders[orders["detail"].notna()]["detail"].str.extract(r"food_place=(\d+)")
        if not fp.empty:
            orders.loc[fp.index, "food_place_id"] = fp[0].astype(float)
            return orders.groupby("food_place_id").order_id.nunique().nlargest(top_n)
    except Exception:
        pass
    return pd.Series(dtype=int)


def total_delivery_time_hist(events: pd.DataFrame):
    orders = events[events["type"] == "order_status"].copy()
    if orders.empty:
        return pd.Series(dtype=float)
    # compute time from first status (assume status 1 CONFIRMED) to status 6 DELIVERED
    first = orders[orders["status_id"] == 1].groupby("order_id")["timestamp"].min()
    last = orders[orders["status_id"] == 6].groupby("order_id")["timestamp"].max()
    merged = pd.concat([first, last], axis=1)
    merged.columns = ["first_ts", "last_ts"]
    merged = merged.dropna(subset=["first_ts", "last_ts"])
    merged["tot_s"] = (merged["last_ts"] - merged["first_ts"]).dt.total_seconds()
    return merged["tot_s"].dropna()


# --- Render charts -----------------------------------------------
col1, col2 = st.columns(2)

with col1:
    st.subheader("Volume de pedidos (horário)")
    vol = volume_over_time(df)
    if vol is not None and not vol.empty:
        st.line_chart(vol)
    else:
        st.write("Sem eventos de pedido para mostrar volume.")

with col2:
    st.subheader("Tempo médio por estado (s)")
    avg = avg_time_per_state(df)
    if not avg.empty:
        st.bar_chart(avg)
    else:
        st.write("Dados insuficientes para calcular tempo por estado.")

st.subheader("Distribuição de pedidos por região / restaurante")
top = top_restaurants(df)
if not top.empty:
    st.table(top.head(10))
else:
    st.write("Não há informação de restaurante/região nos eventos coletados.")

st.subheader("Heatmap: demanda por horário x dia da semana")
pivot = heatmap_by_hour_weekday(df)
if not pivot.empty:
    st.dataframe(pivot)
else:
    st.write("Sem eventos suficientes para heatmap.")

st.subheader("Histograma do tempo total de entrega (s)")
hist = total_delivery_time_hist(df)
if not hist.empty:
    hist_bins = pd.cut(hist, bins=20).value_counts().sort_index()
    hist_bins.index = hist_bins.index.astype(str)
    st.bar_chart(hist_bins)
else:
    st.write("Sem entregas completas (status 6) nos dados coletados.")


def _load_s3_prediction_jsonl(bucket: str, prefix: str) -> list[dict[str, Any]]:
    if not bucket:
        return []
    s3 = boto3.client("s3")
    latest_key = f"{prefix.rstrip('/')}/latest.jsonl"
    try:
        body = s3.get_object(Bucket=bucket, Key=latest_key)["Body"].read().decode("utf-8")
        rows = parse_jsonl_body(body)
        if rows:
            return rows
    except ClientError:
        pass
    keys: list[str] = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                k = obj.get("Key", "")
                if k.endswith(".out"):
                    keys.append(k)
    except ClientError:
        return []
    if not keys:
        return []
    body = s3.get_object(Bucket=bucket, Key=sorted(keys)[-1])["Body"].read().decode("utf-8")
    return parse_batch_transform_body(body)


st.header("Camada Preditiva (SageMaker)")
datalake = next((b for b in dict.fromkeys(S3_BUCKET_CANDIDATES) if b), "")
demand_rows = _load_s3_prediction_jsonl(datalake, "ml/predictions/demand/")
anomaly_rows = _load_s3_prediction_jsonl(datalake, "ml/predictions/anomaly/")

col3, col4 = st.columns(2)
with col3:
    st.subheader("Previsão de demanda (batch)")
    if demand_rows:
        st.dataframe(pd.DataFrame(demand_rows).head(20))
    else:
        st.write("Execute `python -m ml.batch_predict` após o treinamento.")
with col4:
    st.subheader("Anomalias operacionais (batch)")
    if anomaly_rows:
        st.dataframe(pd.DataFrame(anomaly_rows).head(20))
    else:
        st.write("Sem saídas de anomalia em s3://…/ml/predictions/anomaly/.")

if ATHENA_DB or os.environ.get("GLUE_DATABASE"):
    st.caption(
        f"Athena/Glue: {ATHENA_DB or os.environ.get('GLUE_DATABASE')} — "
        "consultas históricas também disponíveis via agente (`query_analytics`)."
    )
