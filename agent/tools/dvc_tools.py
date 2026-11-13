"""DVC data-management tools.

All access goes through :func:`_run`, which only executes an explicit allowlist
of read-only ``git`` / ``dvc`` sub-commands, never through a shell. Nothing in
this module modifies the workspace: old dataset versions are materialised into
a temporary directory with ``dvc get`` instead of ``dvc checkout``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator

from agent.config import get_settings
from agent.schemas import NoArgs, ToolArgs
from agent.tools.base import PermissionDeniedError, Tool, ToolContext, ToolError, ToolUnavailableError

# (binary, sub-command) pairs the agent may run. Everything else is refused.
ALLOWED_COMMANDS: set[tuple[str, str]] = {
    ("git", "rev-parse"),
    ("git", "log"),
    ("git", "show"),
    ("git", "tag"),
    ("git", "ls-files"),
    ("dvc", "status"),
    ("dvc", "diff"),
    ("dvc", "get"),
    ("dvc", "remote"),
    ("dvc", "data"),
}
VERSION_TAG_GLOB = "data-v*"
_REV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}$")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


def _run(cmd: list[str], timeout: float = 25.0) -> str:
    """Run one allowlisted command in the repository root and return stdout."""
    if len(cmd) < 2 or (cmd[0], cmd[1]) not in ALLOWED_COMMANDS:
        raise PermissionDeniedError(f"command not approved: {' '.join(cmd[:2])}")
    settings = get_settings()
    argv = list(cmd)
    if argv[0] == "dvc":
        argv[0] = settings.dvc_bin if Path(settings.dvc_bin).exists() else (shutil.which("dvc") or "")
        if not argv[0]:
            raise ToolUnavailableError("the dvc executable is not installed")
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell, allowlisted
            argv, cwd=settings.repo_root, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise ToolUnavailableError(f"{cmd[0]} executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"`{' '.join(cmd[:2])}` timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise ToolError(f"`{' '.join(cmd[:3])}` failed: {' | '.join(detail)}")
    return proc.stdout


def _check_rev(rev: str) -> str:
    if not _REV_RE.match(rev):
        raise ValueError("revision must be a git commit, tag or branch name")
    return rev


def _tracked_dvc_files() -> list[str]:
    return [line for line in _run(["git", "ls-files", "*.dvc"]).splitlines() if line.strip()]


def _resolve_dataset(path: str) -> str:
    """Return the dataset path, refusing anything that is not DVC-tracked."""
    path = path or get_settings().dataset_path
    if f"{path}.dvc" not in _tracked_dvc_files():
        raise ToolError(f"'{path}' is not a DVC-tracked dataset in this repository")
    return path


def _parse_dvc_file(text: str) -> dict[str, Any]:
    out = (yaml.safe_load(text) or {}).get("outs", [{}])[0]
    md5 = out.get("md5")  # YAML reads an all-digit hash as an int
    return {
        "md5": str(md5) if md5 is not None else None,
        "size_bytes": out.get("size"),
        "hash": out.get("hash", "md5"),
    }


def _pointer_at(rev: str, path: str) -> dict[str, Any]:
    """The DVC pointer (md5, size) of ``path`` as recorded at git revision ``rev``."""
    commit = _run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"]).strip()
    info = _parse_dvc_file(_run(["git", "show", f"{commit}:{path}.dvc"]))
    info["git_commit"] = commit
    return info


def _version_tags(path: str) -> list[dict[str, Any]]:
    tags = []
    for tag in _run(["git", "tag", "--list", VERSION_TAG_GLOB, "--sort=creatordate"]).split():
        try:
            tags.append({"tag": tag, **_pointer_at(tag, path)})
        except ToolError:
            continue  # tag predates this dataset
    return tags


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - DVC's content address, not a security hash
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _materialise(rev: str, path: str, dest: Path) -> Path:
    """Fetch ``path`` as of ``rev`` from the DVC cache/remote into ``dest`` (read-only)."""
    target = dest / Path(path).name
    _run(["dvc", "get", ".", path, "--rev", rev, "-o", str(target)], timeout=60)
    return target


def _csv_shape(path: Path) -> dict[str, int]:
    with path.open() as handle:
        header = handle.readline()
        rows = sum(1 for _ in handle)
    return {"rows": rows, "columns": len(header.split(","))}


# --------------------------------------------------------------------------- tools


class DatasetArgs(ToolArgs):
    path: str = Field(default="", description="DVC-tracked dataset path. Empty = default dataset.")


class CompareArgs(ToolArgs):
    rev_a: str = Field(description="First git revision (tag such as data-v1.0, or commit).")
    rev_b: str = Field(description="Second git revision (tag, commit, or HEAD).")
    path: str = Field(default="", description="DVC-tracked dataset path. Empty = default dataset.")

    @field_validator("rev_a", "rev_b")
    @classmethod
    def _revs(cls, v: str) -> str:
        return _check_rev(v)


class FindArgs(ToolArgs):
    md5: str = Field(default="", description="DVC md5 recorded for a training run.")
    rev: str = Field(default="", description="Dataset tag or dvc.pointer_commit of a run (not git.commit).")
    path: str = Field(default="", description="DVC-tracked dataset path. Empty = default dataset.")

    @field_validator("md5")
    @classmethod
    def _md5(cls, v: str) -> str:
        if v and not _MD5_RE.match(v):
            raise ValueError("md5 must be 32 lowercase hex characters")
        return v

    @field_validator("rev")
    @classmethod
    def _rev(cls, v: str) -> str:
        return _check_rev(v) if v else v


class ReproduceArgs(ToolArgs):
    rev: str = Field(description="Git tag or commit of the dataset version to reproduce.")
    path: str = Field(default="", description="DVC-tracked dataset path. Empty = default dataset.")
    verify: bool = Field(
        default=True,
        description="Actually fetch that version into a temporary directory and check its md5.",
    )

    @field_validator("rev")
    @classmethod
    def _rev(cls, v: str) -> str:
        return _check_rev(v)


def list_tracked_datasets(args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
    settings = get_settings()
    datasets = []
    for dvc_file in _tracked_dvc_files():
        pointer = _parse_dvc_file((settings.repo_root / dvc_file).read_text())
        datasets.append({"path": dvc_file.removesuffix(".dvc"), "dvc_file": dvc_file, **pointer})
    pipeline_outputs = []
    lock = settings.repo_root / "dvc.lock"
    if lock.exists():
        for stage, body in (yaml.safe_load(lock.read_text()) or {}).get("stages", {}).items():
            for out in body.get("outs", []):
                pipeline_outputs.append({"stage": stage, "path": out["path"], "md5": str(out.get("md5"))})
    remotes = [line.split()[:2] for line in _run(["dvc", "remote", "list"]).splitlines() if line]
    return {
        "datasets": datasets,
        "pipeline_outputs": pipeline_outputs,
        "remotes": [{"name": r[0], "url": r[-1]} for r in remotes],
    }


def current_version(args: DatasetArgs, ctx: ToolContext) -> dict[str, Any]:
    settings = get_settings()
    path = _resolve_dataset(args.path)
    recorded = _parse_dvc_file((settings.repo_root / f"{path}.dvc").read_text())
    data_file = settings.repo_root / path
    workspace_md5 = _file_md5(data_file) if data_file.is_file() else None
    tags = _version_tags(path)
    return {
        "path": path,
        "md5": recorded["md5"],
        "size_bytes": recorded["size_bytes"],
        "version_tags": [t["tag"] for t in tags if t["md5"] == recorded["md5"]],
        "git_head_commit": _run(["git", "rev-parse", "HEAD"]).strip(),
        "pointer_last_changed_in_commit": _run(
            ["git", "log", "-1", "--format=%H", "--", f"{path}.dvc"]
        ).strip(),
        "workspace_file_present": workspace_md5 is not None,
        "workspace_matches_recorded_version": workspace_md5 == recorded["md5"],
        "all_version_tags": [{"tag": t["tag"], "md5": t["md5"]} for t in tags],
    }


def compare_versions(args: CompareArgs, ctx: ToolContext) -> dict[str, Any]:
    path = _resolve_dataset(args.path)
    sides = {"a": {"rev": args.rev_a}, "b": {"rev": args.rev_b}}
    for side in sides.values():
        side.update(_pointer_at(side["rev"], path))
    with tempfile.TemporaryDirectory(prefix="dvc-compare-") as tmp:
        for name, side in sides.items():
            try:
                dest = Path(tmp) / name
                dest.mkdir()
                side.update(_csv_shape(_materialise(side["rev"], path, dest)))
            except ToolError as exc:
                side["content_unavailable"] = str(exc)
    diff = json.loads(_run(["dvc", "diff", args.rev_a, args.rev_b, "--json"]) or "{}")
    a, b = sides["a"], sides["b"]
    result: dict[str, Any] = {
        "path": path,
        "a": a,
        "b": b,
        "identical": a["md5"] == b["md5"],
        "size_delta_bytes": (b["size_bytes"] or 0) - (a["size_bytes"] or 0),
        "dvc_diff": {k: [e["path"] for e in v] for k, v in diff.items() if v},
    }
    if "rows" in a and "rows" in b:
        result["row_delta"] = b["rows"] - a["rows"]
    return result


def status(args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
    workspace = json.loads(_run(["dvc", "status", "--json"]) or "{}")
    data = json.loads(_run(["dvc", "data", "status", "--json"]) or "{}")
    uncommitted = data.get("uncommitted", {})
    result: dict[str, Any] = {
        "tracked_data_changed": bool(workspace) or bool(uncommitted),
        "changed_stages_or_pointers": workspace,
        "uncommitted_data_changes": uncommitted,
        "not_in_cache": data.get("not_in_cache", []),
    }
    try:
        cloud = json.loads(_run(["dvc", "status", "--cloud", "--json"]) or "{}")
        result["remote_in_sync"] = not cloud
        result["remote_differences"] = cloud
    except ToolError as exc:
        result["remote_in_sync"] = None
        result["remote_check_error"] = str(exc)
    return result


def find_version(args: FindArgs, ctx: ToolContext) -> dict[str, Any]:
    """Cross-check a dataset reference recorded elsewhere (e.g. MLflow tags) against git/DVC."""
    if not args.md5 and not args.rev:
        raise ToolError("provide the md5 and/or the git revision recorded for the run")
    path = _resolve_dataset(args.path)
    result: dict[str, Any] = {"path": path, "query": {"md5": args.md5, "rev": args.rev}}
    md5 = args.md5
    if args.rev:
        at_rev = _pointer_at(args.rev, path)
        result["pointer_at_rev"] = at_rev
        result["rev_and_md5_consistent"] = (not args.md5) or at_rev["md5"] == args.md5
        md5 = md5 or at_rev["md5"]
    commits = _run(["git", "log", "--format=%H", "--", f"{path}.dvc"]).split()
    introduced_in = [c for c in commits if _pointer_at(c, path)["md5"] == md5]
    tags = [t["tag"] for t in _version_tags(path) if t["md5"] == md5]
    result.update(
        md5=md5,
        found_in_history=bool(introduced_in),
        version_tags=tags,
        pointer_commits=introduced_in,
        is_current_workspace_version=_parse_dvc_file((get_settings().repo_root / f"{path}.dvc").read_text())[
            "md5"
        ]
        == md5,
    )
    return result


def reproduce_instructions(args: ReproduceArgs, ctx: ToolContext) -> dict[str, Any]:
    path = _resolve_dataset(args.path)
    pointer = _pointer_at(args.rev, path)
    result: dict[str, Any] = {
        "path": path,
        "rev": args.rev,
        **pointer,
        "commands_restore_in_place": [
            f"git checkout {args.rev} -- {path}.dvc",
            f"dvc checkout {path}.dvc   # dvc pull {path}.dvc first if the cache is empty",
        ],
        "commands_export_copy": [f"dvc get . {path} --rev {args.rev} -o /tmp/{Path(path).name}"],
        "commands_return_to_latest": [f"git checkout HEAD -- {path}.dvc", f"dvc checkout {path}.dvc"],
    }
    if args.verify:
        with tempfile.TemporaryDirectory(prefix="dvc-verify-") as tmp:
            fetched = _materialise(args.rev, path, Path(tmp))
            fetched_md5 = _file_md5(fetched)
            result["verification"] = {
                "method": "dvc get into a temporary directory (workspace untouched)",
                "fetched_md5": fetched_md5,
                "matches_recorded_md5": fetched_md5 == pointer["md5"],
                **_csv_shape(fetched),
            }
    return result


TOOLS = [
    Tool(
        name="dvc_list_tracked_datasets",
        description="List datasets tracked by DVC (path, md5, size), pipeline outputs and remotes.",
        args_model=NoArgs,
        func=list_tracked_datasets,
        category="dvc",
    ),
    Tool(
        name="dvc_current_version",
        description=(
            "Current dataset version in the workspace: DVC md5, matching version tags, git commit, "
            "and whether the file on disk still matches the recorded version."
        ),
        args_model=DatasetArgs,
        func=current_version,
        category="dvc",
    ),
    Tool(
        name="dvc_compare_versions",
        description="Compare a dataset between two git revisions (md5, size, rows, dvc diff).",
        args_model=CompareArgs,
        func=compare_versions,
        category="dvc",
        timeout_seconds=90,
    ),
    Tool(
        name="dvc_status",
        description=(
            "Detect whether DVC-tracked data changed versus what is recorded, and whether the "
            "remote is in sync."
        ),
        args_model=NoArgs,
        func=status,
        category="dvc",
        timeout_seconds=60,
    ),
    Tool(
        name="dvc_find_version",
        description=(
            "Resolve a dataset reference recorded for a training run (the dvc.md5 and/or "
            "git.commit tags of an MLflow run) to a dataset version tag and verify it exists in "
            "git/DVC history."
        ),
        args_model=FindArgs,
        func=find_version,
        category="dvc",
    ),
    Tool(
        name="dvc_reproduce_instructions",
        description=(
            "Explain how to reproduce a previous dataset version, and verify it by fetching that "
            "version into a temporary directory and checking its md5."
        ),
        args_model=ReproduceArgs,
        func=reproduce_instructions,
        category="dvc",
        timeout_seconds=90,
    ),
]
