"""Airflow tools against an in-memory HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from agent.tools import airflow_tools
from agent.tools.base import PermissionDeniedError, ToolContext, ToolError, ToolUnavailableError
from tests.conftest import FAKE_AIRFLOW_PASSWORD

CTX = ToolContext(request_id="test")
RUN = "manual__2026-11-13"


def _failed_run_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith(f"/dagRuns/{RUN}"):
        return httpx.Response(200, json={"dag_run_id": RUN, "state": "failed", "conf": {"min_f1": 0.999}})
    if path.endswith("/taskInstances"):
        states = [
            ("validate_dataset", "success"),
            ("quality_gate", "failed"),
            ("register_model", "upstream_failed"),
        ]
        return httpx.Response(
            200,
            json={
                "task_instances": [
                    {
                        "task_id": t,
                        "state": s,
                        "try_number": 1,
                        "max_tries": 2,
                        "start_date": f"2026-11-13T10:0{i}:00Z" if s != "upstream_failed" else None,
                    }
                    for i, (t, s) in enumerate(states)
                ]
            },
        )
    if "/logs/" in path:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"event": "running stage", "level": "info"},
                    {
                        "event": "QUALITY GATE FAILED: f1 0.9722 is below the required 0.9990",
                        "level": "error",
                    },
                ]
            },
        )
    return httpx.Response(404)


def test_requests_are_authenticated_with_a_bearer_token(fake_airflow):
    requests = fake_airflow()
    airflow_tools.list_dag_runs(airflow_tools.DagRunsArgs(dag_id="training_pipeline"), CTX)

    login, api_call = requests
    assert login.url.path == "/auth/token"
    assert json.loads(login.content) == {"username": "admin", "password": FAKE_AIRFLOW_PASSWORD}
    assert api_call.url.path == "/api/v2/dags/training_pipeline/dagRuns"
    assert api_call.headers["Authorization"] == "Bearer jwt-for-tests"


def test_expired_token_is_refreshed_once(fake_airflow):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"dag_runs": [], "total_entries": 0})

    requests = fake_airflow(handler)
    result = airflow_tools.list_dag_runs(airflow_tools.DagRunsArgs(dag_id="training_pipeline"), CTX)
    assert result["runs"] == []
    assert [r.url.path for r in requests].count("/auth/token") == 2


def test_failed_tasks_separates_root_cause_from_blocked_downstream(fake_airflow):
    fake_airflow(_failed_run_handler)
    result = airflow_tools.failed_tasks(airflow_tools.DagRunArgs(dag_id="training_pipeline", run_id=RUN), CTX)
    assert result["run_state"] == "failed"
    assert result["root_cause_tasks"] == ["quality_gate"]
    assert result["blocked_downstream_tasks"] == ["register_model"]
    gate = next(t for t in result["failed_tasks"] if t["task_id"] == "quality_gate")
    assert "QUALITY GATE FAILED: f1 0.9722" in gate["log_tail"][-1]


def test_trigger_requires_confirmation_and_an_approved_dag(fake_airflow):
    requests = fake_airflow()
    with pytest.raises(PermissionDeniedError, match="confirmation"):
        airflow_tools.trigger_dag(airflow_tools.TriggerArgs(dag_id="training_pipeline"), CTX)
    with pytest.raises(PermissionDeniedError, match="not approved"):
        airflow_tools.trigger_dag(
            airflow_tools.TriggerArgs(dag_id="other_dag"), ToolContext("t", confirmed=True)
        )
    assert requests == []


def test_confirmed_trigger_posts_only_overridden_conf(fake_airflow):
    requests = fake_airflow()
    result = airflow_tools.trigger_dag(
        airflow_tools.TriggerArgs(dag_id="training_pipeline", min_f1=0.95),
        ToolContext("t", confirmed=True),
    )
    post = requests[-1]
    assert post.method == "POST" and json.loads(post.content) == {
        "logical_date": None,
        "conf": {"min_f1": 0.95},
    }
    assert result["run"]["dag_run_id"] == "manual__new"


def test_unreachable_airflow_maps_to_unavailable(fake_airflow):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fake_airflow(handler)
    with pytest.raises(ToolUnavailableError, match="unreachable"):
        airflow_tools.list_dags(airflow_tools.NoArgs(), CTX)


def test_missing_run_is_an_error_not_an_empty_answer(fake_airflow):
    fake_airflow(lambda request: httpx.Response(404, json={"detail": "not found"}))
    with pytest.raises(ToolError, match="not found"):
        airflow_tools.get_dag_run(airflow_tools.DagRunArgs(dag_id="training_pipeline", run_id="x"), CTX)


def test_retried_task_is_reported_even_though_the_run_succeeded(fake_airflow):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/dagRuns/{RUN}"):
            return httpx.Response(200, json={"dag_run_id": RUN, "state": "success", "conf": {}})
        if path.endswith("/taskInstances"):
            return httpx.Response(200, json={"task_instances": [
                {"task_id": "train_model", "state": "success", "try_number": 2, "max_tries": 2},
                {"task_id": "validate_dataset", "state": "success", "try_number": 1, "max_tries": 2},
            ]})  # fmt: skip
        if path.endswith("/train_model/tries"):
            return httpx.Response(200, json={"task_instances": [
                {"try_number": 1, "state": "failed", "duration": 9.5},
                {"try_number": 2, "state": "success", "duration": 8.0},
            ]})  # fmt: skip
        if path.endswith("/logs/1"):
            return httpx.Response(200, json={"content": [
                {"event": "stage 'train' crashed with exit code 1 (will retry)", "level": "error"},
            ]})  # fmt: skip
        return httpx.Response(404)

    fake_airflow(handler)
    result = airflow_tools.failed_tasks(airflow_tools.DagRunArgs(dag_id="training_pipeline", run_id=RUN), CTX)

    assert result["run_state"] == "success" and result["failed_task_count"] == 0
    assert result["retried_task_count"] == 1
    retried = result["retried_tasks"][0]
    assert retried["task_id"] == "train_model" and retried["final_state"] == "success"
    assert [a["state"] for a in retried["attempts"]] == ["failed", "success"]
    assert "will retry" in retried["failed_attempt_log_tail"][-1]
