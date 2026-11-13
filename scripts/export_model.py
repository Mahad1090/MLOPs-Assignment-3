"""Export a registered MLflow model version into serving_model/ for the Docker build.

python -m scripts.export_model --alias candidate
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import joblib

from training.common import MLFLOW_MODEL_NAME, ROOT, init_mlflow


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alias", default="candidate")
    parser.add_argument("--out", type=Path, default=ROOT / "serving_model")
    args = parser.parse_args()

    mlflow = init_mlflow()
    client = mlflow.tracking.MlflowClient()
    version = client.get_model_version_by_alias(MLFLOW_MODEL_NAME, args.alias)
    run = client.get_run(version.run_id)
    model = mlflow.sklearn.load_model(f"models:/{MLFLOW_MODEL_NAME}/{version.version}")

    args.out.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.out / "model.pkl")
    meta = {
        "model_name": MLFLOW_MODEL_NAME,
        "model_version": version.version,
        "mlflow_run_id": version.run_id,
        "f1": run.data.metrics.get("f1"),
        "dataset_version": run.data.tags.get("dvc.dataset_version"),
        "dataset_md5": run.data.tags.get("dvc.md5"),
        "git_commit": run.data.tags.get("git.commit"),
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (args.out / "model_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
