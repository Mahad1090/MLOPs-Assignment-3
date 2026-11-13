"""MLflow tools against a fake MlflowClient."""

from __future__ import annotations

import pytest
from mlflow.exceptions import MlflowException

from agent.tools import mlflow_tools
from agent.tools.base import ToolContext, ToolError, ToolUnavailableError
from tests.conftest import MD5_V1, MD5_V2, make_run

CTX = ToolContext(request_id="test")


def test_best_run_ranks_by_metric_and_keeps_caveats(fake_mlflow):
    fake_mlflow(
        runs=[
            make_run("low", 0.91, age_days=3),
            make_run("top", 0.98, age_days=2, gate="failed"),
            make_run("mid", 0.95, age_days=1),
        ]
    )
    result = mlflow_tools.best_run(mlflow_tools.BestRunArgs(metric="f1"), CTX)

    assert result["best_run"]["run_id"] == "top"
    assert [r["run_id"] for r in result["ranking"]] == ["top", "mid", "low"]
    assert result["margin_over_runner_up"] == pytest.approx(0.03)
    # The highest score is reported together with its failed gate, never as "production-ready".
    assert result["ranking"][0]["quality_gate"] == "failed"
    assert any("production-ready" in c for c in result["selection_caveats"])


def test_best_run_can_minimise_and_limits_to_recent_runs(fake_mlflow):
    fake_mlflow(
        runs=[
            make_run("old-best", 0.99, age_days=30),
            make_run("a", 0.90, age_days=2),
            make_run("b", 0.93, age_days=1),
        ]
    )
    recent = mlflow_tools.best_run(mlflow_tools.BestRunArgs(last_n=2), CTX)
    assert recent["best_run"]["run_id"] == "b" and recent["runs_considered"] == 2
    lowest = mlflow_tools.best_run(mlflow_tools.BestRunArgs(higher_is_better=False), CTX)
    assert lowest["best_run"]["run_id"] == "a"


def test_best_run_fails_clearly_when_metric_was_never_logged(fake_mlflow):
    fake_mlflow(runs=[make_run("r1", 0.9)])
    with pytest.raises(ToolError, match="recorded metric 'auc_pr'"):
        mlflow_tools.best_run(mlflow_tools.BestRunArgs(metric="auc_pr"), CTX)


def test_get_run_returns_recorded_dataset_provenance(fake_mlflow):
    fake_mlflow(runs=[make_run("r1", 0.95, md5=MD5_V1, version="data-v1.0")])
    result = mlflow_tools.get_run(mlflow_tools.GetRunArgs(run="r1"), CTX)
    assert result["provenance"]["dvc.md5"] == MD5_V1
    assert result["provenance"]["dvc.dataset_version"] == "data-v1.0"
    assert "mlflow.user" not in result["tags"]


def test_get_run_says_so_when_no_dataset_reference_was_recorded(fake_mlflow):
    fake_mlflow(runs=[make_run("r1", 0.95, md5="")])
    result = mlflow_tools.get_run(mlflow_tools.GetRunArgs(run="r1"), CTX)
    assert "dvc.md5" not in result["provenance"]
    assert "no recorded DVC dataset reference" in result["provenance_note"]


def test_compare_runs_shows_metrics_and_dataset_differences(fake_mlflow):
    fake_mlflow(
        runs=[
            make_run("r1", 0.95, age_days=2, md5=MD5_V1, version="data-v1.0"),
            make_run("r2", 0.97, age_days=1, md5=MD5_V2, version="data-v2.0"),
        ]
    )
    result = mlflow_tools.compare_runs(mlflow_tools.CompareRunsArgs(last_n=5), CTX)
    assert [row["run_id"] for row in result["metric_table"]] == ["r2", "r1"]
    assert result["metric_ranges"]["f1"] == {"min": 0.95, "max": 0.97}
    assert result["dataset_versions"] == {"r1": "data-v1.0", "r2": "data-v2.0"}


def test_unreachable_server_maps_to_unavailable(fake_mlflow):
    fake_mlflow(fail_with=MlflowException("API request failed with exception Connection refused"))
    with pytest.raises(ToolUnavailableError, match="unreachable"):
        mlflow_tools.list_runs(mlflow_tools.ListRunsArgs(), CTX)


def test_empty_registry_is_reported_as_empty(fake_mlflow):
    fake_mlflow()
    result = mlflow_tools.model_registry(mlflow_tools.RegistryArgs(), CTX)
    assert result == {"registered_models": [], "registry_empty": True}
