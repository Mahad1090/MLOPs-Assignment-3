#!/usr/bin/env bash
# MLflow tracking server + model registry on http://127.0.0.1:5000
source "$(dirname "$0")/common.sh"
mkdir -p .mlflow
exec .venv/bin/mlflow server --host 127.0.0.1 --port 5000 --workers 1 \
  --backend-store-uri "sqlite:///$ROOT/.mlflow/mlflow.db" \
  --artifacts-destination "$ROOT/.mlflow/artifacts"
