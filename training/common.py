"""Paths, schema and helpers shared by the pipeline stages."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW_DATA = ROOT / "data" / "raw" / "dataset.csv"
PROCESSED_DIR = ROOT / "data" / "processed"
TRAIN_CSV = PROCESSED_DIR / "train.csv"
TEST_CSV = PROCESSED_DIR / "test.csv"
REPORTS_DIR = ROOT / "reports"
VALIDATION_REPORT = REPORTS_DIR / "validation.json"
MODELS_DIR = ROOT / "models"
MODEL_FILE = MODELS_DIR / "model.pkl"
METRICS_FILE = MODELS_DIR / "metrics.json"

# Exit code for "the stage ran correctly and rejected its input" (bad data, failed
# quality gate). The Airflow DAG does not retry these; any other failure is retried.
EXIT_REJECTED = 2

TARGET = "target"
FEATURES = [
    f"{stat}_{name}"
    for stat in ("mean", "error", "worst")
    for name in (
        "radius",
        "texture",
        "perimeter",
        "area",
        "smoothness",
        "compactness",
        "concavity",
        "concave_points",
        "symmetry",
        "fractal_dimension",
    )
]

MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000")
MLFLOW_EXPERIMENT = os.environ.get("MLFLOW_EXPERIMENT", "breast-cancer-classifier")
MLFLOW_MODEL_NAME = os.environ.get("MLFLOW_MODEL_NAME", "breast-cancer-classifier")


def load_params() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "params.yaml").read_text())


def get_logger(stage: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return logging.getLogger(f"pipeline.{stage}")


def init_mlflow():
    import mlflow

    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "2")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "15")
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    return mlflow
