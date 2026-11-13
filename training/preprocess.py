"""Stage 2 - deterministic, stratified train/test split."""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd
from sklearn.model_selection import train_test_split

from training.common import (
    EXIT_REJECTED,
    FEATURES,
    PROCESSED_DIR,
    RAW_DATA,
    TARGET,
    TEST_CSV,
    TRAIN_CSV,
    VALIDATION_REPORT,
    get_logger,
    load_params,
)


def main() -> None:
    params = load_params()["preprocess"]
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-size", type=float, default=params["test_size"])
    parser.add_argument("--seed", type=int, default=params["seed"])
    args = parser.parse_args()
    log = get_logger("preprocess")

    if not VALIDATION_REPORT.exists() or not json.loads(VALIDATION_REPORT.read_text())["passed"]:
        log.error("refusing to preprocess: the dataset has not passed validation")
        sys.exit(EXIT_REJECTED)

    df = pd.read_csv(RAW_DATA)[[*FEATURES, TARGET]].drop_duplicates()
    train, test = train_test_split(df, test_size=args.test_size, random_state=args.seed, stratify=df[TARGET])
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    train.to_csv(TRAIN_CSV, index=False)
    test.to_csv(TEST_CSV, index=False)
    log.info("wrote %d train rows and %d test rows", len(train), len(test))


if __name__ == "__main__":
    main()
