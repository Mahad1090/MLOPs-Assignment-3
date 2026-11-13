#!/usr/bin/env bash
# Injected failure 2: run the training DAG with a quality gate no model can pass
# (min_f1 = 0.999). The gate fails and register_model must not run.
source "$(dirname "$0")/common.sh"
BASE="${AIRFLOW_BASE_URL:-http://127.0.0.1:8080}"
TOKEN=$(curl -fsS -X POST "$BASE/auth/token" -H 'Content-Type: application/json' \
  -d "{\"username\":\"${AIRFLOW_USERNAME:-admin}\",\"password\":\"$AIRFLOW_PASSWORD\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -fsS -X POST "$BASE/api/v2/dags/training_pipeline/dagRuns" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"logical_date": null, "conf": {"min_f1": 0.999}}' | python3 -m json.tool
