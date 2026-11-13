"""Stage 4 - evaluate on the held-out split and log metrics + model to the MLflow run."""

from __future__ import annotations

import argparse
import json

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score

from training.common import (
    FEATURES,
    METRICS_FILE,
    MODEL_FILE,
    TARGET,
    TEST_CSV,
    VALIDATION_REPORT,
    get_logger,
    init_mlflow,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    log = get_logger("evaluate")

    model = joblib.load(MODEL_FILE)
    test = pd.read_csv(TEST_CSV)
    predicted = model.predict(test[FEATURES])
    scores = model.predict_proba(test[FEATURES])[:, 1]
    metrics = {
        "accuracy": accuracy_score(test[TARGET], predicted),
        "precision": precision_score(test[TARGET], predicted),
        "recall": recall_score(test[TARGET], predicted),
        "f1": f1_score(test[TARGET], predicted),
        "roc_auc": roc_auc_score(test[TARGET], scores),
    }
    METRICS_FILE.write_text(json.dumps(metrics, indent=2))

    mlflow = init_mlflow()
    with mlflow.start_run(run_id=args.run_id):
        mlflow.log_metrics(metrics)
        mlflow.log_artifact(str(METRICS_FILE))
        mlflow.log_artifact(str(VALIDATION_REPORT))
        mlflow.sklearn.log_model(model, name="model", input_example=test[FEATURES].head(2))
    log.info("logged to MLflow run %s: %s", args.run_id, {k: round(v, 4) for k, v in metrics.items()})


if __name__ == "__main__":
    main()
