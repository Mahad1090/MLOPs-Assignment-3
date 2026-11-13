"""Stage 5 - model quality gate. Exits with EXIT_REJECTED when the model is below threshold."""

from __future__ import annotations

import argparse
import sys

from training.common import EXIT_REJECTED, get_logger, init_mlflow, load_params


def main() -> None:
    defaults = load_params()["quality_gate"]
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--min-f1", type=float, default=defaults["min_f1"])
    parser.add_argument("--min-accuracy", type=float, default=defaults["min_accuracy"])
    args = parser.parse_args()
    log = get_logger("quality_gate")

    mlflow = init_mlflow()
    client = mlflow.tracking.MlflowClient()
    metrics = client.get_run(args.run_id).data.metrics
    thresholds = {"f1": args.min_f1, "accuracy": args.min_accuracy}
    failures = []
    for name, minimum in thresholds.items():
        value = metrics.get(name)
        if value is None:
            failures.append(f"{name} was not recorded")
        elif value < minimum:
            failures.append(f"{name} {value:.4f} is below the required {minimum:.4f}")

    client.set_tag(args.run_id, "quality_gate", "failed" if failures else "passed")
    client.set_tag(args.run_id, "quality_gate.thresholds", str(thresholds))
    if failures:
        client.set_tag(args.run_id, "quality_gate.reasons", "; ".join(failures))
        for failure in failures:
            log.error("QUALITY GATE FAILED: %s", failure)
        sys.exit(EXIT_REJECTED)
    log.info("quality gate passed: %s >= %s", {k: round(metrics[k], 4) for k in thresholds}, thresholds)


if __name__ == "__main__":
    main()
