"""Materialise a dataset version as data/raw/dataset.csv (deterministic).

Source: the Wisconsin Diagnostic Breast Cancer dataset bundled with scikit-learn.
  v1.0 - the first 400 records of a fixed shuffle (initial data delivery)
  v2.0 - all 569 records (later, larger delivery)
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sklearn.datasets import load_breast_cancer

from training.common import FEATURES, RAW_DATA, TARGET

VERSIONS = {"v1.0": 400, "v2.0": 569}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", choices=sorted(VERSIONS), required=True)
    parser.add_argument("--out", type=Path, default=RAW_DATA)
    args = parser.parse_args()

    frame = load_breast_cancer(as_frame=True).frame
    frame.columns = [*FEATURES, TARGET]
    frame = frame.sample(frac=1.0, random_state=7).reset_index(drop=True).head(VERSIONS[args.version])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out, index=False)
    print(f"wrote {len(frame)} rows to {args.out} (dataset {args.version})")


if __name__ == "__main__":
    main()
