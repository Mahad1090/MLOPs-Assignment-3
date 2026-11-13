"""Run all stages locally in order (same stages and order as the Airflow DAG).

python -m training.pipeline --model-type logistic_regression --C 0.5
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from training.common import ROOT


def _stage(module: str, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", f"training.{module}", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    print(proc.stdout, end="")
    if proc.returncode != 0:
        sys.exit(f"stage '{module}' failed with exit code {proc.returncode}; pipeline stopped")
    return proc.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-f1", default=None)
    opts, train_args = parser.parse_known_args()

    _stage("validate")
    _stage("preprocess")
    out = _stage("train", *train_args)
    run_id = out.strip().splitlines()[-1].removeprefix("RUN_ID=")
    _stage("evaluate", "--run-id", run_id)
    _stage("quality_gate", "--run-id", run_id, *(["--min-f1", opts.min_f1] if opts.min_f1 else []))
    _stage("register", "--run-id", run_id)


if __name__ == "__main__":
    main()
