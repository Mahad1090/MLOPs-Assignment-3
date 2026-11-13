"""Stage 6 - promotion: register the model in the MLflow registry as the 'candidate'.

Runs only downstream of the quality gate, and re-checks the gate result itself.
"""

from __future__ import annotations

import argparse
import sys

from training.common import EXIT_REJECTED, MLFLOW_MODEL_NAME, get_logger, init_mlflow


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    log = get_logger("register")

    mlflow = init_mlflow()
    client = mlflow.tracking.MlflowClient()
    run = client.get_run(args.run_id)
    if run.data.tags.get("quality_gate") != "passed":
        log.error("refusing to register run %s: quality gate has not passed", args.run_id)
        sys.exit(EXIT_REJECTED)

    version = mlflow.register_model(f"runs:/{args.run_id}/model", MLFLOW_MODEL_NAME)
    client.set_registered_model_alias(MLFLOW_MODEL_NAME, "candidate", version.version)
    for key in ("dvc.md5", "dvc.dataset_version", "git.commit"):
        if key in run.data.tags:
            client.set_model_version_tag(MLFLOW_MODEL_NAME, version.version, key, run.data.tags[key])
    client.set_model_version_tag(
        MLFLOW_MODEL_NAME, version.version, "f1", f"{run.data.metrics.get('f1', float('nan')):.5f}"
    )
    log.info("registered %s version %s (alias: candidate)", MLFLOW_MODEL_NAME, version.version)
    print(f"MODEL_VERSION={version.version}")


if __name__ == "__main__":
    main()
