"""Apache Airflow 3 workflow tools.

Talks to the Airflow 3 public REST API (``/api/v2``) using a JWT obtained from
``POST /auth/token`` with credentials taken from the environment. Triggering is
limited to an allowlist of DAG ids and requires explicit user confirmation.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Literal

import httpx
from pydantic import Field

from agent.config import get_settings
from agent.schemas import NoArgs, ToolArgs
from agent.tools.base import (
    PermissionDeniedError,
    Tool,
    ToolContext,
    ToolError,
    ToolUnavailableError,
    tail_within,
)

TERMINAL_STATES = {"success", "failed"}
# Airflow bookkeeping lines that carry no diagnostic value.
_LOG_NOISE = ("::group::", "::endgroup::", "DAG bundles loaded", "Filling up the DagBag", "parse complete")
FAILED_TASK_STATES = {"failed", "upstream_failed"}
_ID = r"^[A-Za-z0-9_.:+\-]+$"

# Tests replace this with an httpx.MockTransport so no network is needed.
_transport: httpx.BaseTransport | None = None
_token_lock = threading.Lock()
_token: dict[str, Any] = {"value": None, "base_url": None}


def _http() -> httpx.Client:
    return httpx.Client(base_url=get_settings().airflow_base_url, timeout=15.0, transport=_transport)


def _unavailable(exc: Exception) -> ToolUnavailableError:
    return ToolUnavailableError(
        f"Airflow API at {get_settings().airflow_base_url} is unreachable ({type(exc).__name__})"
    )


def _login(client: httpx.Client) -> str:
    settings = get_settings()
    password = settings.airflow_password.get_secret_value()
    if not password:
        raise ToolUnavailableError("AIRFLOW_PASSWORD is not configured")
    response = client.post("/auth/token", json={"username": settings.airflow_username, "password": password})
    if response.status_code >= 400:
        raise ToolError(f"Airflow authentication failed (HTTP {response.status_code})")
    return response.json()["access_token"]


def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Authenticated request against /api/v2, re-authenticating once on HTTP 401."""
    settings = get_settings()
    try:
        with _http() as client:
            for attempt in (1, 2):
                with _token_lock:
                    if _token["value"] is None or _token["base_url"] != settings.airflow_base_url:
                        _token.update(value=_login(client), base_url=settings.airflow_base_url)
                    token = _token["value"]
                response = client.request(
                    method,
                    f"/api/v2{path}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    **kwargs,
                )
                if response.status_code == 401 and attempt == 1:
                    _token["value"] = None  # expired token: log in again and retry once
                    continue
                break
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _unavailable(exc) from exc
    if response.status_code == 404:
        raise ToolError(f"Airflow: not found ({path})")
    if response.status_code >= 400:
        detail = response.text[:300].replace("\n", " ")
        raise ToolError(f"Airflow API returned HTTP {response.status_code}: {detail}")
    return response.json()


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "dag_run_id": run.get("dag_run_id"),
        "state": run.get("state"),
        "run_type": run.get("run_type"),
        "triggered_by": run.get("triggered_by"),
        "logical_date": run.get("logical_date"),
        "start_date": run.get("start_date"),
        "end_date": run.get("end_date"),
        "duration_seconds": run.get("duration"),
        "conf": run.get("conf") or {},
    }


def _task_view(ti: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": ti.get("task_id"),
        "state": ti.get("state"),
        "try_number": ti.get("try_number"),
        "max_tries": ti.get("max_tries"),
        "start_date": ti.get("start_date"),
        "end_date": ti.get("end_date"),
        "duration_seconds": ti.get("duration"),
        "operator": ti.get("operator"),
    }


def _latest_run_id(dag_id: str) -> str:
    runs = _request("GET", f"/dags/{dag_id}/dagRuns", params={"limit": 1, "order_by": "-run_after"}).get(
        "dag_runs", []
    )
    if not runs:
        raise ToolError(f"DAG '{dag_id}' has no runs yet")
    return runs[0]["dag_run_id"]


def _task_instances(dag_id: str, run_id: str) -> list[dict[str, Any]]:
    tis = _request("GET", f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances").get("task_instances", [])
    return sorted((_task_view(t) for t in tis), key=lambda t: (t["start_date"] or "9", t["task_id"]))


def _log_lines(dag_id: str, run_id: str, task_id: str, try_number: int, tail: int) -> list[str]:
    payload = _request(
        "GET",
        f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances/{task_id}/logs/{try_number}",
        params={"full_content": "true"},
    )
    content = payload.get("content", "")
    if isinstance(content, str):
        lines = content.splitlines()
    else:  # Airflow 3 returns structured log records
        lines = []
        for record in content:
            if not isinstance(record, dict):
                lines.append(str(record))
                continue
            text = str(record.get("event", "")).rstrip()
            if record.get("error_detail"):
                for err in record["error_detail"]:
                    text += f" | {err.get('exc_type')}: {err.get('exc_value')}"
            if text:
                level = str(record.get("level", "")).upper()
                lines.append(f"[{level}] {text}" if level else text)
    lines = [line for line in lines if not any(noise in line for noise in _LOG_NOISE)]
    return tail_within(lines[-tail:], max_chars=1500)


# --------------------------------------------------------------------------- tools


class DagRunsArgs(ToolArgs):
    dag_id: str = Field(pattern=_ID, max_length=120)
    limit: int = Field(default=5, ge=1, le=25)


class DagRunArgs(ToolArgs):
    dag_id: str = Field(pattern=_ID, max_length=120)
    run_id: str = Field(default="", max_length=250, description="Empty = the most recent run.")


class TaskLogArgs(ToolArgs):
    dag_id: str = Field(pattern=_ID, max_length=120)
    task_id: str = Field(pattern=_ID, max_length=120)
    run_id: str = Field(default="", max_length=250, description="Empty = the most recent run.")
    try_number: int = Field(default=0, ge=0, le=20, description="0 = the latest attempt.")
    tail_lines: int = Field(default=25, ge=5, le=200)


class TriggerArgs(ToolArgs):
    dag_id: str = Field(pattern=_ID, max_length=120, description="Must be an approved DAG.")
    model_type: Literal["", "random_forest", "logistic_regression"] = Field(
        default="", description="Empty = DAG default."
    )
    n_estimators: int = Field(default=0, ge=0, le=1000, description="0 = DAG default.")
    max_depth: int = Field(default=0, ge=0, le=50, description="0 = DAG default.")
    min_f1: float = Field(default=0, ge=0, le=1, description="Quality-gate F1. 0 = DAG default.")


class WaitArgs(ToolArgs):
    dag_id: str = Field(pattern=_ID, max_length=120)
    run_id: str = Field(min_length=1, max_length=250)
    timeout_seconds: int = Field(default=240, ge=10, le=280)


def list_dags(args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
    approved = get_settings().approved_dags
    dags = _request("GET", "/dags", params={"limit": 100}).get("dags", [])
    return {
        "dags": [
            {
                "dag_id": d["dag_id"],
                "description": d.get("description"),
                "is_paused": d.get("is_paused"),
                "schedule": d.get("timetable_summary"),
                "tags": [t["name"] if isinstance(t, dict) else t for t in d.get("tags", [])],
                "has_import_errors": d.get("has_import_errors"),
                "agent_may_trigger": d["dag_id"] in approved,
            }
            for d in dags
        ]
    }


def list_dag_runs(args: DagRunsArgs, ctx: ToolContext) -> dict[str, Any]:
    payload = _request(
        "GET",
        f"/dags/{args.dag_id}/dagRuns",
        params={"limit": args.limit, "order_by": "-run_after"},
    )
    runs = [_run_view(r) for r in payload.get("dag_runs", [])]
    return {
        "dag_id": args.dag_id,
        "order": "most recent first",
        "total_runs": payload.get("total_entries"),
        "runs": runs,
    }


def get_dag_run(args: DagRunArgs, ctx: ToolContext) -> dict[str, Any]:
    run_id = args.run_id or _latest_run_id(args.dag_id)
    run = _run_view(_request("GET", f"/dags/{args.dag_id}/dagRuns/{run_id}"))
    tasks = _task_instances(args.dag_id, run_id)
    return {
        "dag_id": args.dag_id,
        "run": run,
        "tasks": tasks,
        "task_state_counts": {
            state: sum(1 for t in tasks if t["state"] == state)
            for state in sorted({str(t["state"]) for t in tasks})
        },
    }


def failed_tasks(args: DagRunArgs, ctx: ToolContext) -> dict[str, Any]:
    run_id = args.run_id or _latest_run_id(args.dag_id)
    run = _run_view(_request("GET", f"/dags/{args.dag_id}/dagRuns/{run_id}"))
    tasks = _task_instances(args.dag_id, run_id)
    failed = [t for t in tasks if t["state"] in FAILED_TASK_STATES]
    for task in failed:
        if task["state"] != "failed":
            task["diagnostic"] = "Not executed: an upstream task failed."
            continue
        try:
            task["log_tail"] = _log_lines(args.dag_id, run_id, task["task_id"], task["try_number"] or 1, 12)
        except ToolError as exc:
            task["log_error"] = str(exc)
    return {
        "dag_id": args.dag_id,
        "dag_run_id": run_id,
        "run_state": run["state"],
        "failed_task_count": len(failed),
        "root_cause_tasks": [t["task_id"] for t in failed if t["state"] == "failed"],
        "blocked_downstream_tasks": [t["task_id"] for t in failed if t["state"] != "failed"],
        "failed_tasks": failed,
    }


def task_logs(args: TaskLogArgs, ctx: ToolContext) -> dict[str, Any]:
    run_id = args.run_id or _latest_run_id(args.dag_id)
    ti = _task_view(_request("GET", f"/dags/{args.dag_id}/dagRuns/{run_id}/taskInstances/{args.task_id}"))
    try_number = args.try_number or ti["try_number"] or 1
    return {
        "dag_id": args.dag_id,
        "dag_run_id": run_id,
        "task": ti,
        "try_number": try_number,
        "log_tail": _log_lines(args.dag_id, run_id, args.task_id, try_number, args.tail_lines),
    }


def _trigger_conf(args: TriggerArgs) -> dict[str, Any]:
    conf: dict[str, Any] = {}
    if args.model_type:
        conf["model_type"] = args.model_type
    if args.n_estimators:
        conf["n_estimators"] = args.n_estimators
    if args.max_depth:
        conf["max_depth"] = args.max_depth
    if args.min_f1:
        conf["min_f1"] = args.min_f1
    return conf


def trigger_dag(args: TriggerArgs, ctx: ToolContext) -> dict[str, Any]:
    check_trigger_allowed(args)
    if not ctx.confirmed:  # defence in depth; the executor already enforces this
        raise PermissionDeniedError("triggering a DAG requires explicit user confirmation")
    run = _request(
        "POST",
        f"/dags/{args.dag_id}/dagRuns",
        json={"logical_date": None, "conf": _trigger_conf(args)},
    )
    return {"triggered": True, "dag_id": args.dag_id, "run": _run_view(run)}


def check_trigger_allowed(args: TriggerArgs) -> None:
    approved = get_settings().approved_dags
    if args.dag_id not in approved:
        raise PermissionDeniedError(
            f"DAG '{args.dag_id}' is not approved for triggering (approved: {sorted(approved)})"
        )


def wait_for_dag_run(args: WaitArgs, ctx: ToolContext) -> dict[str, Any]:
    deadline = time.monotonic() + args.timeout_seconds
    while True:
        run = _run_view(_request("GET", f"/dags/{args.dag_id}/dagRuns/{args.run_id}"))
        if run["state"] in TERMINAL_STATES or time.monotonic() >= deadline:
            break
        time.sleep(5)
    return {
        "dag_id": args.dag_id,
        "finished": run["state"] in TERMINAL_STATES,
        "run": run,
        "tasks": _task_instances(args.dag_id, args.run_id),
    }


TOOLS = [
    Tool(
        name="airflow_list_dags",
        description="List Airflow DAGs, whether they are paused and whether the agent may trigger them.",
        args_model=NoArgs,
        func=list_dags,
        category="airflow",
    ),
    Tool(
        name="airflow_list_dag_runs",
        description="Most recent runs of a DAG with state, timing and the conf they ran with.",
        args_model=DagRunsArgs,
        func=list_dag_runs,
        category="airflow",
    ),
    Tool(
        name="airflow_get_dag_run",
        description="Status of one DAG run (latest by default) with the state of every task.",
        args_model=DagRunArgs,
        func=get_dag_run,
        category="airflow",
    ),
    Tool(
        name="airflow_failed_tasks",
        description=(
            "Identify the failed tasks of a DAG run (latest by default), separating the task that "
            "actually failed from downstream tasks that were blocked, with the failing log tail."
        ),
        args_model=DagRunArgs,
        func=failed_tasks,
        category="airflow",
    ),
    Tool(
        name="airflow_task_logs",
        description="Execution details and the log tail of one task attempt.",
        args_model=TaskLogArgs,
        func=task_logs,
        category="airflow",
    ),
    Tool(
        name="airflow_trigger_dag",
        description=(
            "Start a run of an approved training DAG. Changes state: it is only executed after "
            "the user explicitly confirms. Use only when the user asked to run the pipeline."
        ),
        args_model=TriggerArgs,
        func=trigger_dag,
        category="airflow",
        mutating=True,
        precheck=check_trigger_allowed,
        summarize=lambda a: f"Trigger Airflow DAG '{a.dag_id}' with conf {_trigger_conf(a) or '(defaults)'}",
    ),
    Tool(
        name="airflow_wait_for_dag_run",
        description="Wait (up to a few minutes) for a DAG run to finish and return task states.",
        args_model=WaitArgs,
        func=wait_for_dag_run,
        category="airflow",
        timeout_seconds=300,
    ),
]
