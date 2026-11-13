"""Model training pipeline (Airflow 3).

    validate_dataset -> preprocess_data -> train_model -> evaluate_and_log_mlflow
        -> quality_gate -> register_model

* Each task runs one stage of ``training/`` in the project's Python environment,
  so Airflow's own environment stays free of ML dependencies.
* Transient failures (non-zero exit) are retried twice. A stage that *rejects*
  its input (exit code 2: invalid data, failed quality gate) fails immediately
  without retries, because re-running cannot change the outcome.
* ``register_model`` (promotion) is downstream of ``quality_gate`` with the
  default all_success trigger rule, so a failed validation or gate leaves it in
  ``upstream_failed`` and nothing is promoted.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from airflow.sdk import Param, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException

PROJECT_ROOT = Path(os.environ.get("MLOPS_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
PROJECT_PYTHON = os.environ.get("MLOPS_PYTHON", str(PROJECT_ROOT / ".venv" / "bin" / "python"))
EXIT_REJECTED = 2

log = logging.getLogger("training_pipeline")


def run_stage(module: str, *args: str) -> str:
    """Run ``python -m training.<module>`` and stream its output into the task log."""
    command = [PROJECT_PYTHON, "-m", f"training.{module}", *args]
    log.info("running stage: %s", " ".join(command))
    proc = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=900, check=False)
    for line in proc.stdout.splitlines():
        log.info("[%s] %s", module, line)
    for line in proc.stderr.splitlines()[-30:]:
        log.warning("[%s] %s", module, line)
    if proc.returncode == EXIT_REJECTED:
        reasons = [line for line in proc.stdout.splitlines() if "ERROR" in line][-3:]
        raise AirflowFailException(
            f"stage '{module}' rejected its input (not retried): {' | '.join(reasons)}"
        )
    if proc.returncode != 0:
        raise RuntimeError(f"stage '{module}' crashed with exit code {proc.returncode} (will retry)")
    return proc.stdout


@dag(
    dag_id="training_pipeline",
    description="Validate data, train, evaluate to MLflow, apply quality gate, register model",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(seconds=15),
        "execution_timeout": timedelta(minutes=20),
    },
    params={
        "model_type": Param("random_forest", enum=["random_forest", "logistic_regression"]),
        "n_estimators": Param(100, type="integer", minimum=1, maximum=1000),
        "max_depth": Param(5, type="integer", minimum=1, maximum=50),
        "min_f1": Param(0.93, type="number", minimum=0, maximum=1),
    },
    tags=["mlops", "training"],
)
def training_pipeline():
    @task
    def validate_dataset() -> None:
        run_stage("validate")

    @task
    def preprocess_data() -> None:
        run_stage("preprocess")

    @task
    def train_model() -> str:
        context = get_current_context()
        params = context["params"]
        output = run_stage(
            "train",
            "--model-type",
            str(params["model_type"]),
            "--n-estimators",
            str(params["n_estimators"]),
            "--max-depth",
            str(params["max_depth"]),
            "--dag-run-id",
            context["run_id"],
        )
        run_id = output.strip().splitlines()[-1].removeprefix("RUN_ID=")
        log.info("MLflow run id: %s", run_id)
        return run_id

    @task
    def evaluate_and_log_mlflow(mlflow_run_id: str) -> str:
        run_stage("evaluate", "--run-id", mlflow_run_id)
        return mlflow_run_id

    @task
    def quality_gate(mlflow_run_id: str) -> str:
        min_f1 = get_current_context()["params"]["min_f1"]
        run_stage("quality_gate", "--run-id", mlflow_run_id, "--min-f1", str(min_f1))
        return mlflow_run_id

    @task
    def register_model(mlflow_run_id: str) -> None:
        run_stage("register", "--run-id", mlflow_run_id)

    trained = train_model()
    validate_dataset() >> preprocess_data() >> trained
    register_model(quality_gate(evaluate_and_log_mlflow(trained)))


training_pipeline()
