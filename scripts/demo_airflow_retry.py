"""Injected failure 4: a transient MLflow outage while the training DAG runs.

MLflow is stopped as soon as validation succeeds, so `train_model` crashes (exit code 1, not a
rejection). Airflow marks it up_for_retry and runs it again after the retry delay. MLflow is
started again right after the first failure, so the retry succeeds and the run finishes.

    .venv/bin/python scripts/demo_airflow_retry.py
"""

import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent.config import get_settings  # noqa: E402

settings = get_settings()
DAG = "training_pipeline"
t0 = time.time()


def log(message: str) -> None:
    print(f"[{time.time() - t0:6.1f}s] {message}", flush=True)


client = httpx.Client(base_url=settings.airflow_base_url, timeout=20)
token = client.post(
    "/auth/token",
    json={"username": settings.airflow_username, "password": settings.airflow_password.get_secret_value()},
).json()["access_token"]
client.headers["Authorization"] = f"Bearer {token}"


def stop_mlflow() -> None:
    subprocess.run(["pkill", "-9", "-f", "[m]lflow server"], check=False)
    subprocess.run(["pkill", "-9", "-f", "[m]lflow[./]server"], check=False)


def start_mlflow() -> None:
    log_file = open(ROOT / ".logs" / "mlflow.log", "ab")  # noqa: SIM115
    subprocess.Popen(  # noqa: S603
        [str(ROOT / "scripts" / "start_mlflow.sh")], stdout=log_file, stderr=log_file, start_new_session=True
    )


run = client.post(f"/api/v2/dags/{DAG}/dagRuns", json={"logical_date": None, "conf": {}}).json()
run_id = run["dag_run_id"]
log(f"triggered {run_id}")
seen: dict[str, tuple] = {}
mlflow_stopped = mlflow_started = False
while True:
    tasks = client.get(f"/api/v2/dags/{DAG}/dagRuns/{run_id}/taskInstances").json()["task_instances"]
    states = {t["task_id"]: (t["state"], t["try_number"]) for t in tasks}
    for task_id, value in states.items():
        if seen.get(task_id) != value:
            seen[task_id] = value
            log(f"{task_id:26} state={value[0]} try_number={value[1]}")
    if not mlflow_stopped and states.get("validate_dataset", ("",))[0] == "success":
        stop_mlflow()
        mlflow_stopped = True
        log("INJECTED: MLflow server stopped")
    if mlflow_stopped and not mlflow_started and states.get("train_model", ("",))[0] == "up_for_retry":
        start_mlflow()
        mlflow_started = True
        log("MLflow server started again (outage over)")
    state = client.get(f"/api/v2/dags/{DAG}/dagRuns/{run_id}").json()["state"]
    if state in ("success", "failed"):
        break
    time.sleep(1)

log(f"DAG run finished: {state}")
tries = client.get(f"/api/v2/dags/{DAG}/dagRuns/{run_id}/taskInstances/train_model/tries").json()
for item in tries.get("task_instances", []):
    log(f"train_model try {item['try_number']}: {item['state']}")
