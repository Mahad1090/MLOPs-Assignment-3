"""Stage 1 - validate the raw dataset. Exits with EXIT_REJECTED if it is unfit for training."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from training.common import EXIT_REJECTED, FEATURES, RAW_DATA, TARGET, VALIDATION_REPORT, get_logger

MIN_ROWS = 200
MIN_MINORITY_SHARE = 0.10


def validate(df: pd.DataFrame) -> list[str]:
    problems: list[str] = []
    missing = [c for c in [*FEATURES, TARGET] if c not in df.columns]
    if missing:
        return [f"missing columns: {missing}"]
    if len(df) < MIN_ROWS:
        problems.append(f"only {len(df)} rows, need at least {MIN_ROWS}")
    nulls = df[[*FEATURES, TARGET]].isna().sum()
    if nulls.any():
        problems.append(f"null values in: {nulls[nulls > 0].to_dict()}")
    non_numeric = [c for c in FEATURES if not pd.api.types.is_numeric_dtype(df[c])]
    if non_numeric:
        problems.append(f"non-numeric feature columns: {non_numeric}")
    else:
        negative = [c for c in FEATURES if (df[c] < 0).any()]
        if negative:
            problems.append(f"negative values in: {negative}")
    labels = set(df[TARGET].dropna().unique().tolist())
    if not labels <= {0, 1}:
        problems.append(f"unexpected target labels: {sorted(labels)}")
    elif len(labels) < 2:
        problems.append("target contains a single class")
    elif df[TARGET].value_counts(normalize=True).min() < MIN_MINORITY_SHARE:
        problems.append("minority class is below 10% of rows")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=RAW_DATA)
    args = parser.parse_args()
    log = get_logger("validate")

    if not args.data.exists():
        log.error("dataset %s does not exist (run `dvc pull`)", args.data)
        sys.exit(EXIT_REJECTED)
    df = pd.read_csv(args.data)
    problems = validate(df)
    report = {
        "rows": len(df),
        "columns": len(df.columns),
        "passed": not problems,
        "problems": problems,
    }
    if TARGET in df.columns:
        report["class_counts"] = {str(k): int(v) for k, v in df[TARGET].value_counts().items()}
    VALIDATION_REPORT.parent.mkdir(parents=True, exist_ok=True)
    VALIDATION_REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    if problems:
        for problem in problems:
            log.error("VALIDATION FAILED: %s", problem)
        sys.exit(EXIT_REJECTED)
    log.info("validation passed: %d rows, %d columns", len(df), len(df.columns))


if __name__ == "__main__":
    main()
