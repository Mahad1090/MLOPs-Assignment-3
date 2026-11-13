"""Stage 3 - train the model and open the MLflow run that records its provenance.

Prints ``RUN_ID=<mlflow run id>`` on the last line so the orchestrator can pass
the run to the evaluation, quality-gate and registration stages.
"""

from __future__ import annotations

import argparse
import json

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from training.common import (
    FEATURES,
    MLFLOW_EXPERIMENT,
    MODEL_FILE,
    MODELS_DIR,
    TARGET,
    TEST_CSV,
    TRAIN_CSV,
    get_logger,
    init_mlflow,
    load_params,
)
from training.provenance import dataset_provenance


def build_model(model_type: str, n_estimators: int, max_depth: int, c: float, seed: int) -> Pipeline:
    if model_type == "random_forest":
        clf = RandomForestClassifier(
            n_estimators=n_estimators, max_depth=max_depth, random_state=seed, n_jobs=1
        )
    elif model_type == "logistic_regression":
        clf = LogisticRegression(C=c, max_iter=2000, random_state=seed)
    else:
        raise ValueError(f"unknown model_type '{model_type}'")
    return Pipeline([("scale", StandardScaler()), ("clf", clf)])


def main() -> None:
    defaults = load_params()["train"]
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-type", default=defaults["model_type"])
    parser.add_argument("--n-estimators", type=int, default=defaults["n_estimators"])
    parser.add_argument("--max-depth", type=int, default=defaults["max_depth"])
    parser.add_argument("--C", type=float, default=defaults["C"])
    parser.add_argument("--seed", type=int, default=defaults["seed"])
    parser.add_argument("--dag-run-id", default="")
    args = parser.parse_args()
    log = get_logger("train")

    train = pd.read_csv(TRAIN_CSV)
    model = build_model(args.model_type, args.n_estimators, args.max_depth, args.C, args.seed)
    model.fit(train[FEATURES], train[TARGET])
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_FILE)
    log.info("trained %s on %d rows", args.model_type, len(train))

    params = {"model_type": args.model_type, "seed": args.seed}
    if args.model_type == "random_forest":
        params |= {"n_estimators": args.n_estimators, "max_depth": args.max_depth}
    else:
        params |= {"C": args.C}

    mlflow = init_mlflow()
    mlflow.set_experiment(MLFLOW_EXPERIMENT)
    provenance = dataset_provenance()
    with mlflow.start_run() as run:
        mlflow.log_params(params)
        mlflow.log_params({"train_rows": len(train), "test_rows": len(pd.read_csv(TEST_CSV))})
        mlflow.set_tags(provenance)
        mlflow.set_tag("quality_gate", "pending")
        if args.dag_run_id:
            mlflow.set_tag("airflow.dag_run_id", args.dag_run_id)
        run_id = run.info.run_id
    (MODELS_DIR / "run_info.json").write_text(json.dumps({"run_id": run_id, **provenance}, indent=2))
    log.info("dataset provenance: %s", provenance)
    print(f"RUN_ID={run_id}")


if __name__ == "__main__":
    main()
