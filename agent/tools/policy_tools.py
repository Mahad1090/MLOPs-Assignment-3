"""Retraining-policy tool.

Turns "is retraining necessary?" into a deterministic check of an explicit,
version-controlled policy (config/retraining_policy.yaml) against evidence read
from MLflow and DVC. It never starts a training run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml
from pydantic import Field

from agent.config import get_settings
from agent.schemas import ToolArgs
from agent.tools import dvc_tools, mlflow_tools
from agent.tools.base import Tool, ToolContext, ToolError


class RetrainingArgs(ToolArgs):
    run: str = Field(min_length=1, max_length=120, description="MLflow run id of the model.")
    observed_live_f1: float = Field(
        default=-1,
        ge=-1,
        le=1,
        description="Live/production F1 supplied by the user. -1 = no degradation data supplied.",
    )


def _load_policy() -> dict[str, Any]:
    path = get_settings().retraining_policy_path
    if not path.exists():
        raise ToolError(f"no retraining policy file at {path}")
    return yaml.safe_load(path.read_text())


def evaluate_retraining_need(args: RetrainingArgs, ctx: ToolContext) -> dict[str, Any]:
    policy = _load_policy()
    client = mlflow_tools._client()
    run = mlflow_tools._summary(mlflow_tools._find_run(client, args.run))
    provenance = run["provenance"]
    checks: list[dict[str, Any]] = []
    unknowns: list[str] = []

    # 1. freshness
    max_age = policy["freshness"]["max_model_age_days"]
    if run["ended_at"]:
        age_days = (datetime.now(UTC) - datetime.fromisoformat(run["ended_at"])).total_seconds() / 86400
        checks.append(
            {
                "rule": f"model age must not exceed {max_age} days",
                "evidence": f"training run ended {run['ended_at']} ({age_days:.1f} days ago)",
                "triggers_retraining": age_days > max_age,
            }
        )
    else:
        unknowns.append("run has no end time, so model age cannot be evaluated")

    # 2. quality gate
    gate = provenance.get("quality_gate")
    if gate is None:
        unknowns.append("no quality-gate result is recorded for this run")
    elif policy["quality"]["retrain_when_quality_gate_failed"]:
        checks.append(
            {
                "rule": "the model's training run must have passed the quality gate",
                "evidence": f"quality_gate tag = {gate}"
                + (
                    f" ({provenance['quality_gate.reasons']})"
                    if provenance.get("quality_gate.reasons")
                    else ""
                ),
                "triggers_retraining": gate != "passed",
            }
        )

    # 3. dataset version
    trained_md5 = provenance.get("dvc.md5")
    if not trained_md5:
        unknowns.append("the run has no recorded DVC dataset reference")
    elif policy["dataset"]["retrain_when_dataset_version_differs"]:
        try:
            current = dvc_tools.current_version(dvc_tools.DatasetArgs(), ctx)
            checks.append(
                {
                    "rule": "model must be trained on the current DVC dataset version",
                    "evidence": (
                        f"trained on md5 {trained_md5} "
                        f"({provenance.get('dvc.dataset_version', 'untagged')}); current dataset is "
                        f"md5 {current['md5']} ({', '.join(current['version_tags']) or 'untagged'})"
                    ),
                    "triggers_retraining": current["md5"] != trained_md5,
                }
            )
        except ToolError as exc:
            unknowns.append(f"current DVC dataset version could not be read: {exc}")

    # 4. supplied degradation results
    max_drop = policy["degradation"]["max_f1_drop"]
    recorded_f1 = run["metrics"].get("f1")
    if args.observed_live_f1 < 0:
        unknowns.append("no live/production performance data was supplied, so model degradation is unknown")
    elif recorded_f1 is None:
        unknowns.append("the run has no recorded f1 to compare the supplied live f1 against")
    else:
        drop = recorded_f1 - args.observed_live_f1
        checks.append(
            {
                "rule": f"live F1 must not be more than {max_drop} below the recorded test F1",
                "evidence": (
                    f"recorded f1 {recorded_f1}, supplied live f1 {args.observed_live_f1} (drop {drop:.4f})"
                ),
                "triggers_retraining": drop > max_drop,
            }
        )

    reasons = [c["evidence"] for c in checks if c["triggers_retraining"]]
    return {
        "run_id": run["run_id"],
        "policy_file": str(get_settings().retraining_policy_path.name),
        "policy_version": policy.get("policy_version"),
        "retraining_recommended": bool(reasons),
        "reasons": reasons,
        "checks": checks,
        "unknowns": unknowns,
        "note": (
            "This is a recommendation only. No training run was started; triggering the "
            "pipeline needs a separate, explicitly confirmed request."
        ),
    }


TOOLS = [
    Tool(
        name="evaluate_retraining_need",
        description=(
            "Check a model's training run against the explicit retraining policy (model age, "
            "quality gate, dataset version drift, user-supplied degradation). Read-only: it "
            "never starts training. Use it before saying anything about whether to retrain."
        ),
        args_model=RetrainingArgs,
        func=evaluate_retraining_need,
        category="policy",
        timeout_seconds=45,
    ),
]
