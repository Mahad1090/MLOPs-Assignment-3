"""Collect the dataset reference (DVC + git) that is recorded on every MLflow run."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml

from training.common import RAW_DATA, ROOT


def _git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - DVC content address, not a security hash
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_provenance(data_file: Path = RAW_DATA) -> dict[str, str]:
    """Tags describing exactly which dataset version is in the workspace right now."""
    pointer_file = data_file.with_name(data_file.name + ".dvc")
    rel_pointer = str(pointer_file.relative_to(ROOT))
    recorded_md5 = ""
    if pointer_file.exists():
        outs = (yaml.safe_load(pointer_file.read_text()) or {}).get("outs", [{}])
        recorded_md5 = str(outs[0].get("md5", ""))
    actual_md5 = file_md5(data_file)

    version = "untagged"
    for tag in _git("tag", "--list", "data-v*", "--sort=-creatordate").split():
        tagged = yaml.safe_load(_git("show", f"{tag}:{rel_pointer}") or "{}") or {}
        if str(tagged.get("outs", [{}])[0].get("md5")) == actual_md5:
            version = tag
            break

    return {
        "dvc.dataset_path": str(data_file.relative_to(ROOT)),
        # md5 of the bytes actually trained on; equals the DVC pointer when the flag below is true
        "dvc.md5": actual_md5,
        "dvc.workspace_matches_pointer": str(actual_md5 == recorded_md5).lower(),
        "dvc.dataset_version": version,
        "dvc.pointer_commit": _git("log", "-1", "--format=%H", "--", rel_pointer),
        "git.commit": _git("rev-parse", "HEAD"),
    }
