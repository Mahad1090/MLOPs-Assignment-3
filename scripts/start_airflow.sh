#!/usr/bin/env bash
# Airflow 3 (api-server, scheduler, dag-processor, triggerer) on http://127.0.0.1:8080
# Login: admin / $AIRFLOW_PASSWORD from .env
source "$(dirname "$0")/common.sh"
: "${AIRFLOW_PASSWORD:?set AIRFLOW_PASSWORD in .env}"
export AIRFLOW_HOME="$ROOT/.airflow"
export AIRFLOW__CORE__DAGS_FOLDER="$ROOT/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS="${AIRFLOW_USERNAME:-admin}:admin"
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE="$AIRFLOW_HOME/simple_auth_passwords.json"
export AIRFLOW__API__WORKERS=1
export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=30
export MLOPS_PROJECT_ROOT="$ROOT"
export MLOPS_PYTHON="$ROOT/.venv/bin/python"
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://127.0.0.1:5000}"
mkdir -p "$AIRFLOW_HOME"
( umask 077; printf '{"%s": "%s"}\n' "${AIRFLOW_USERNAME:-admin}" "$AIRFLOW_PASSWORD" \
    > "$AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE" )
# "airflow standalone" starts its components by calling the "airflow" command from PATH
export PATH="$ROOT/.venv-airflow/bin:$PATH"
exec airflow standalone
