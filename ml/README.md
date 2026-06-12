# DijkFood ML / SageMaker

Predictive layer for Objective 3 (seminar theme: SageMaker).

## Lifecycle

1. Deploy stack with analytics: `python deploy.py --skip-teardown --with-analytics`
2. Generate traffic: `python -m simulator.orchestration.load_test --base-url $BASE_URL --duration 1800`
3. Prepare datasets: `python -m ml.prepare_datasets`
4. Train models: `python -m ml.train --deploy-delivery` (deletes prior `dijkfood-*` jobs + S3 artifacts by default; use `--no-cleanup` to skip)
5. Deploy predictions: `python deploy.py --skip-teardown --resume --with-predictions`
6. Copy `SAGEMAKER_DEMAND_MODEL_NAME` / `SAGEMAKER_ANOMALY_MODEL_NAME` from `ml.train` output into `connection.env` (optional — auto-discovers latest `model.tar.gz` under `ml/artifacts/`)
7. Batch forecasts: `python -m ml.batch_predict` (SageMaker Batch Transform — same sklearn as training)

If you already trained with the old `1.2-1` framework, **retrain** after pulling these changes:

```bash
python -m ml.train
python -m ml.batch_predict
```

Local-only inference (`--local`) needs `scikit-learn>=1.4,<1.5` matching the training container; on Python 3.14 prefer the default SageMaker path.

## Environment

Install ML client deps (SageMaker **V2** `<2.256`; do **not** install V3 `sagemaker-train` packages):

```bash
pip install -r requirements-ml.txt
```

**Python 3.14:** OK for `ml.train` / `ml.batch_predict` (jobs run on SageMaker). For `batch_predict --local`, use a **Python 3.11–3.12** venv.

Training uses preinstalled packages in the SageMaker **sklearn 1.4-2** container (no `requirements.txt` in `sagemaker_scripts/`).

Uses `TASK_ROLE_ARN` (LabRole) from `.env` / `connection.env` for SageMaker jobs.

Key variables in `connection.env`:

- `DATALAKE_S3_BUCKET`
- `GLUE_DATABASE`
- `SAGEMAKER_DELIVERY_ENDPOINT`
- `SAGEMAKER_DEMAND_MODEL_NAME` / `SAGEMAKER_ANOMALY_MODEL_NAME` (model artifact S3 URIs)

## Teardown endpoints (save lab budget)

```bash
python -m ml.teardown_endpoints
```

Or full stack: `python deploy.py --teardown-only`
