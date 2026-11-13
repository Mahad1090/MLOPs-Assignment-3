"""MLflow experiment-intelligence tools, built on ``MlflowClient`` (no UI scraping)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from pydantic import Field

from agent.config import get_settings
from agent.schemas import NoArgs, ToolArgs
from agent.tools.base import Tool, ToolContext, ToolError, ToolUnavailableError

# Fail fast instead of MLflow's default multi-minute retry loop when the server is down.
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")

PROVENANCE_TAGS = (
    "dvc.dataset_path",
    "dvc.md5",
    "dvc.dataset_version",
    "dvc.pointer_commit",
    "dvc.workspace_matches_pointer",
    "git.commit",
    "quality_gate",
    "quality_gate.reasons",
    "airflow.dag_run_id",
)
_UNREACHABLE_HINTS = ("max retries", "connection", "failed with exception", "timed out", "refused")


def _client():
    from mlflow.tracking import MlflowClient

    return MlflowClient(tracking_uri=get_settings().mlflow_tracking_uri)


def _call(fn, *args, **kwargs):
    """Run an MlflowClient call, mapping failures onto the agent's error types."""
    from mlflow.exceptions import MlflowException

    try:
        return fn(*args, **kwargs)
    except MlflowException as exc:
        message = str(exc)
        if any(hint in message.lower() for hint in _UNREACHABLE_HINTS):
            raise ToolUnavailableError(
                f"MLflow tracking server at {get_settings().mlflow_tracking_uri} is unreachable"
            ) from exc
        raise ToolError(f"MLflow error: {message[:300]}") from exc
    except (ConnectionError, OSError) as exc:
        raise ToolUnavailableError(
            f"MLflow tracking server at {get_settings().mlflow_tracking_uri} is unreachable"
        ) from exc


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds") if ms else None


def _experiment(client, ref: str):
    ref = ref or get_settings().mlflow_experiment
    exp = _call(client.get_experiment_by_name, ref)
    if exp is None and ref.isdigit():
        try:
            exp = _call(client.get_experiment, ref)
        except ToolUnavailableError:
            raise
        except ToolError:
            exp = None
    if exp is None:
        raise ToolError(f"no MLflow experiment named or numbered '{ref}'")
    return exp


def _summary(run) -> dict[str, Any]:
    tags = run.data.tags
    return {
        "run_id": run.info.run_id,
        "run_name": run.info.run_name,
        "status": run.info.status,
        "started_at": _iso(run.info.start_time),
        "ended_at": _iso(run.info.end_time),
        "metrics": {k: round(v, 5) for k, v in sorted(run.data.metrics.items())},
        "params": dict(sorted(run.data.params.items())),
        "provenance": {t: tags[t] for t in PROVENANCE_TAGS if t in tags},
    }


def _recent_runs(client, experiment: str, limit: int) -> tuple[Any, list[Any]]:
    exp = _experiment(client, experiment)
    runs = _call(
        client.search_runs,
        [exp.experiment_id],
        order_by=["attributes.start_time DESC"],
        max_results=limit,
    )
    return exp, list(runs)


def _find_run(client, ref: str):
    """Look a run up by id, falling back to its run name in the default experiment."""
    from mlflow.exceptions import MlflowException

    try:
        return client.get_run(ref)
    except MlflowException as exc:
        if any(hint in str(exc).lower() for hint in _UNREACHABLE_HINTS):
            raise ToolUnavailableError(
                f"MLflow tracking server at {get_settings().mlflow_tracking_uri} is unreachable"
            ) from exc
    exp = _experiment(client, "")
    safe = ref.replace("'", "")
    matches = _call(client.search_runs, [exp.experiment_id], filter_string=f"attributes.run_name = '{safe}'")
    if not matches:
        raise ToolError(f"no MLflow run with id or name '{ref}'")
    return matches[0]


def _artifacts(client, run_id: str, path: str = "", depth: int = 2) -> list[dict[str, Any]]:
    found = []
    for item in _call(client.list_artifacts, run_id, path or None):
        if item.is_dir and depth > 0:
            found.extend(_artifacts(client, run_id, item.path, depth - 1))
        else:
            found.append({"path": item.path, "size_bytes": item.file_size})
    return found


# --------------------------------------------------------------------------- tools


class ListRunsArgs(ToolArgs):
    experiment: str = Field(default="", description="Experiment name or id. Empty = default.")
    max_results: int = Field(default=10, ge=1, le=50, description="Most recent N runs.")


class GetRunArgs(ToolArgs):
    run: str = Field(min_length=1, max_length=120, description="MLflow run id or run name.")


class BestRunArgs(ToolArgs):
    metric: str = Field(default="f1", description="Metric to rank by, e.g. f1, accuracy, roc_auc.")
    experiment: str = Field(default="", description="Experiment name or id. Empty = default.")
    last_n: int = Field(default=20, ge=1, le=100, description="Consider only the most recent N runs.")
    higher_is_better: bool = Field(default=True)


class CompareRunsArgs(ToolArgs):
    run_ids: list[str] = Field(
        default_factory=list, description="Run ids to compare. Empty = use the last_n recent runs."
    )
    last_n: int = Field(default=5, ge=2, le=20)
    experiment: str = Field(default="", description="Experiment name or id. Empty = default.")
    metric: str = Field(default="f1", description="Metric used to order the comparison.")


class CompareExperimentsArgs(ToolArgs):
    experiments: list[str] = Field(min_length=2, max_length=6, description="Experiment names/ids.")
    metric: str = Field(default="f1")


class RegistryArgs(ToolArgs):
    model_name: str = Field(default="", description="Registered model name. Empty = all models.")


def list_experiments(args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
    client = _client()
    experiments = []
    for exp in _call(client.search_experiments):
        runs = _call(client.search_runs, [exp.experiment_id], max_results=1000)
        experiments.append(
            {
                "experiment_id": exp.experiment_id,
                "name": exp.name,
                "lifecycle_stage": exp.lifecycle_stage,
                "run_count": len(runs),
                "last_run_started_at": _iso(max((r.info.start_time for r in runs), default=None)),
            }
        )
    return {"tracking_uri": get_settings().mlflow_tracking_uri, "experiments": experiments}


def list_runs(args: ListRunsArgs, ctx: ToolContext) -> dict[str, Any]:
    exp, runs = _recent_runs(_client(), args.experiment, args.max_results)
    return {
        "experiment": {"id": exp.experiment_id, "name": exp.name},
        "order": "most recent first",
        "runs": [_summary(r) for r in runs],
    }


def get_run(args: GetRunArgs, ctx: ToolContext) -> dict[str, Any]:
    client = _client()
    run = _find_run(client, args.run)
    detail = _summary(run)
    detail["experiment_id"] = run.info.experiment_id
    detail["artifact_uri"] = run.info.artifact_uri
    detail["artifacts"] = _artifacts(client, run.info.run_id)
    detail["tags"] = {k: v for k, v in run.data.tags.items() if not k.startswith("mlflow.")}
    if not detail["provenance"].get("dvc.md5"):
        detail["provenance_note"] = "This run has no recorded DVC dataset reference."
    return detail


def best_run(args: BestRunArgs, ctx: ToolContext) -> dict[str, Any]:
    exp, runs = _recent_runs(_client(), args.experiment, args.last_n)
    scored = [r for r in runs if args.metric in r.data.metrics]
    if not scored:
        raise ToolError(f"none of the last {len(runs)} runs in '{exp.name}' recorded metric '{args.metric}'")
    ranked = sorted(scored, key=lambda r: r.data.metrics[args.metric], reverse=args.higher_is_better)
    best = ranked[0]
    best_value = best.data.metrics[args.metric]
    margin = abs(best_value - ranked[1].data.metrics[args.metric]) if len(ranked) > 1 else None
    tied = [r.info.run_id for r in ranked[1:] if r.data.metrics[args.metric] == best_value]
    return {
        "experiment": {"id": exp.experiment_id, "name": exp.name},
        "metric": args.metric,
        "higher_is_better": args.higher_is_better,
        "runs_considered": len(runs),
        "runs_without_metric": len(runs) - len(scored),
        "best_run": _summary(best),
        "margin_over_runner_up": round(margin, 5) if margin is not None else None,
        "tied_run_ids": tied,
        "ranking": [
            {
                "run_id": r.info.run_id,
                "run_name": r.info.run_name,
                args.metric: round(r.data.metrics[args.metric], 5),
                "quality_gate": r.data.tags.get("quality_gate", "not recorded"),
                "dataset_version": r.data.tags.get("dvc.dataset_version", "not recorded"),
            }
            for r in ranked
        ],
        "selection_caveats": [
            f"Ranked on '{args.metric}' only; a top score on one metric measured on one held-out "
            "split does not by itself make a model production-ready.",
            "Check the quality_gate tag, the other metrics, the size of the test split and "
            "whether the compared runs used the same dataset version before promoting.",
        ],
    }


def compare_runs(args: CompareRunsArgs, ctx: ToolContext) -> dict[str, Any]:
    client = _client()
    if args.run_ids:
        runs = [_find_run(client, rid) for rid in args.run_ids]
    else:
        _, runs = _recent_runs(client, args.experiment, args.last_n)
    if len(runs) < 2:
        raise ToolError(f"need at least two runs to compare, found {len(runs)}")
    summaries = [_summary(r) for r in runs]
    metric_names = sorted({m for s in summaries for m in s["metrics"]})
    param_names = sorted({p for s in summaries for p in s["params"]})
    differing_params = [p for p in param_names if len({s["params"].get(p) for s in summaries}) > 1]
    with_metric = [s for s in summaries if args.metric in s["metrics"]]
    ordered = sorted(with_metric, key=lambda s: s["metrics"][args.metric], reverse=True)
    return {
        "compared_run_count": len(summaries),
        "ordered_by": f"{args.metric} descending",
        "metric_table": [{"run_id": s["run_id"], "run_name": s["run_name"], **s["metrics"]} for s in ordered],
        "runs_missing_metric": [s["run_id"] for s in summaries if args.metric not in s["metrics"]],
        "metric_ranges": {
            m: {
                "min": min(s["metrics"][m] for s in summaries if m in s["metrics"]),
                "max": max(s["metrics"][m] for s in summaries if m in s["metrics"]),
            }
            for m in metric_names
        },
        "differing_params": {
            p: {s["run_id"][:8]: s["params"].get(p) for s in summaries} for p in differing_params
        },
        "dataset_versions": {
            s["run_id"][:8]: s["provenance"].get("dvc.dataset_version", "not recorded") for s in summaries
        },
        "quality_gates": {
            s["run_id"][:8]: s["provenance"].get("quality_gate", "not recorded") for s in summaries
        },
        "runs": summaries,
    }


def compare_experiments(args: CompareExperimentsArgs, ctx: ToolContext) -> dict[str, Any]:
    client = _client()
    rows = []
    for ref in args.experiments:
        exp = _experiment(client, ref)
        runs = _call(client.search_runs, [exp.experiment_id], max_results=1000)
        values = [r.data.metrics[args.metric] for r in runs if args.metric in r.data.metrics]
        best = max(
            (r for r in runs if args.metric in r.data.metrics),
            key=lambda r: r.data.metrics[args.metric],
            default=None,
        )
        rows.append(
            {
                "experiment_id": exp.experiment_id,
                "name": exp.name,
                "run_count": len(runs),
                "runs_with_metric": len(values),
                f"best_{args.metric}": round(max(values), 5) if values else None,
                f"mean_{args.metric}": round(mean(values), 5) if values else None,
                "best_run": _summary(best) if best else None,
            }
        )
    return {"metric": args.metric, "experiments": rows}


def model_registry(args: RegistryArgs, ctx: ToolContext) -> dict[str, Any]:
    client = _client()
    if args.model_name:
        safe = args.model_name.replace("'", "")
        models = _call(client.search_registered_models, filter_string=f"name = '{safe}'")
    else:
        models = _call(client.search_registered_models)
    out = []
    for model in models:
        aliases: dict[str, list[str]] = {}
        for alias, version in dict(model.aliases or {}).items():
            aliases.setdefault(str(version), []).append(alias)
        versions = _call(client.search_model_versions, f"name = '{model.name}'")
        out.append(
            {
                "name": model.name,
                "versions": [
                    {
                        "version": str(v.version),
                        "aliases": aliases.get(str(v.version), []),
                        "source_run_id": v.run_id,
                        "status": v.status,
                        "created_at": _iso(v.creation_timestamp),
                        "tags": dict(v.tags or {}),
                    }
                    for v in sorted(versions, key=lambda v: int(v.version), reverse=True)
                ],
            }
        )
    return {"registered_models": out, "registry_empty": not out}


TOOLS = [
    Tool(
        name="mlflow_list_experiments",
        description="List MLflow experiments with run counts and last activity.",
        args_model=NoArgs,
        func=list_experiments,
        category="mlflow",
    ),
    Tool(
        name="mlflow_list_runs",
        description=(
            "List the most recent training runs of an experiment with metrics, parameters and "
            "the recorded dataset provenance (dvc.md5, dvc.dataset_version, git.commit)."
        ),
        args_model=ListRunsArgs,
        func=list_runs,
        category="mlflow",
    ),
    Tool(
        name="mlflow_get_run",
        description=(
            "Full detail of one run by id or name: metrics, params, artifacts, quality gate and "
            "the DVC dataset reference recorded at training time."
        ),
        args_model=GetRunArgs,
        func=get_run,
        category="mlflow",
    ),
    Tool(
        name="mlflow_best_run",
        description=(
            "Rank recent runs by a metric and return the best one with its margin, quality gate "
            "and dataset provenance."
        ),
        args_model=BestRunArgs,
        func=best_run,
        category="mlflow",
    ),
    Tool(
        name="mlflow_compare_runs",
        description="Compare specific runs, or the last N runs, side by side.",
        args_model=CompareRunsArgs,
        func=compare_runs,
        category="mlflow",
    ),
    Tool(
        name="mlflow_compare_experiments",
        description="Compare two or more experiments on a metric (run counts, best, mean).",
        args_model=CompareExperimentsArgs,
        func=compare_experiments,
        category="mlflow",
    ),
    Tool(
        name="mlflow_model_registry",
        description="Registered models, their versions, aliases, tags and source runs.",
        args_model=RegistryArgs,
        func=model_registry,
        category="mlflow",
    ),
]
