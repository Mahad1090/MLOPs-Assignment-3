"""Shared fixtures and fakes. No test talks to a real service or a real LLM."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from agent.config import get_settings
from agent.llm import LLMTurn, ToolCall
from agent.logging_utils import configure_logging
from agent.orchestrator import Orchestrator
from agent.tools import airflow_tools, build_registry, dvc_tools, kubernetes_tools, mlflow_tools

FAKE_AIRFLOW_PASSWORD = "unit-test-airflow-pw"
MD5_V1 = "1" * 32
MD5_V2 = "2" * 32
COMMIT_V1 = "a" * 40
COMMIT_V2 = "b" * 40


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    """Isolated settings: fake credentials, temp repo root, no dependency on a local .env."""
    data_dir = tmp_path / "data" / "raw"
    data_dir.mkdir(parents=True)
    (data_dir / "dataset.csv.dvc").write_text(
        f"outs:\n- md5: {MD5_V2}\n  size: 2000\n  hash: md5\n  path: dataset.csv\n"
    )
    policy = tmp_path / "retraining_policy.yaml"
    policy.write_text(
        "policy_version: 1\nfreshness: {max_model_age_days: 30}\n"
        "dataset: {retrain_when_dataset_version_differs: true}\n"
        "quality: {retrain_when_quality_gate_failed: true}\ndegradation: {max_f1_drop: 0.05}\n"
    )
    for key, value in {
        "GROQ_API_KEY": "gsk_unit_test_key_0000000000",
        "AIRFLOW_PASSWORD": FAKE_AIRFLOW_PASSWORD,
        "AIRFLOW_BASE_URL": "http://airflow.test",
        "AIRFLOW_APPROVED_DAGS": "training_pipeline",
        "MLFLOW_TRACKING_URI": "http://mlflow.test",
        "K8S_NAMESPACE": "mlops",
        "K8S_APPROVED_DEPLOYMENTS": "prediction-api",
        "REPO_ROOT": str(tmp_path),
        "RETRAINING_POLICY_PATH": str(policy),
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    airflow_tools._token.update(value=None, base_url=None)
    configure_logging()
    yield get_settings()
    get_settings.cache_clear()


# ----------------------------------------------------------------------------- LLM


class FakeLLM:
    """Scripted model: returns the given turns in order and records what it was sent."""

    def __init__(self, *turns: LLMTurn | Callable[[list[dict[str, Any]]], LLMTurn]):
        self.turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools, allow_tools=True) -> LLMTurn:
        self.requests.append(messages)
        if not self.turns:
            return LLMTurn(content="(no more scripted turns)")
        turn = self.turns.pop(0)
        return turn(messages) if callable(turn) else turn

    def tool_messages(self) -> list[dict[str, Any]]:
        """Every tool result the model has been shown."""
        return [m for m in self.requests[-1] if m["role"] == "tool"]


def calls(*specs: tuple[str, dict[str, Any]]) -> LLMTurn:
    return LLMTurn(
        tool_calls=[ToolCall(id=f"call_{i}", name=n, arguments=a) for i, (n, a) in enumerate(specs)]
    )


def say(text: str) -> LLMTurn:
    return LLMTurn(content=text)


@pytest.fixture
def make_agent():
    def _make(*turns, max_steps: int = 8, timeout: float = 5.0):
        llm = FakeLLM(*turns)
        return Orchestrator(llm, build_registry(default_timeout=timeout), max_steps=max_steps), llm

    return _make


# ----------------------------------------------------------------------------- MLflow


def make_run(
    run_id: str,
    f1: float,
    *,
    age_days: float = 1,
    gate: str = "passed",
    md5: str = MD5_V2,
    version: str = "data-v2.0",
    name: str | None = None,
    extra_metrics: dict[str, float] | None = None,
):
    ended = datetime.now(UTC) - timedelta(days=age_days)
    tags = {"quality_gate": gate, "git.commit": COMMIT_V2, "mlflow.user": "x"}
    if md5:
        tags |= {"dvc.md5": md5, "dvc.dataset_version": version}
    return SimpleNamespace(
        info=SimpleNamespace(
            run_id=run_id,
            run_name=name or f"run-{run_id}",
            status="FINISHED",
            start_time=int((ended - timedelta(minutes=1)).timestamp() * 1000),
            end_time=int(ended.timestamp() * 1000),
            experiment_id="1",
            artifact_uri=f"mlflow-artifacts:/1/{run_id}/artifacts",
        ),
        data=SimpleNamespace(
            metrics={"f1": f1, "accuracy": f1 - 0.01, **(extra_metrics or {})},
            params={"model_type": "random_forest", "n_estimators": "100"},
            tags=tags,
        ),
    )


class FakeMlflowClient:
    def __init__(self, runs: list[Any], fail_with: Exception | None = None):
        self.runs = runs
        self.fail_with = fail_with

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def get_experiment_by_name(self, name):
        self._maybe_fail()
        return SimpleNamespace(experiment_id="1", name=name, lifecycle_stage="active")

    def search_experiments(self):
        self._maybe_fail()
        return [self.get_experiment_by_name("breast-cancer-classifier")]

    def search_runs(self, experiment_ids, filter_string=None, order_by=None, max_results=1000):
        self._maybe_fail()
        ordered = sorted(self.runs, key=lambda r: r.info.start_time, reverse=True)
        return ordered[:max_results]

    def get_run(self, run_id):
        self._maybe_fail()
        from mlflow.exceptions import MlflowException

        for run in self.runs:
            if run.info.run_id == run_id:
                return run
        raise MlflowException(f"Run '{run_id}' not found")

    def list_artifacts(self, run_id, path=None):
        return []

    def search_registered_models(self, filter_string=None):
        self._maybe_fail()
        return []


@pytest.fixture
def fake_mlflow(monkeypatch):
    def _install(runs=None, fail_with=None) -> FakeMlflowClient:
        client = FakeMlflowClient(runs or [], fail_with)
        monkeypatch.setattr(mlflow_tools, "_client", lambda: client)
        return client

    return _install


# ----------------------------------------------------------------------------- DVC / git


@pytest.fixture
def fake_dvc(monkeypatch):
    """Replace the subprocess runner with canned git/dvc output for a two-version history."""
    executed: list[list[str]] = []
    pointers = {COMMIT_V1: MD5_V1, COMMIT_V2: MD5_V2}
    revs = {
        "data-v1.0": COMMIT_V1,
        "data-v2.0": COMMIT_V2,
        "HEAD": COMMIT_V2,
        COMMIT_V1: COMMIT_V1,
        COMMIT_V2: COMMIT_V2,
    }

    def run(cmd: list[str], timeout: float = 25.0) -> str:
        executed.append(cmd)
        if (cmd[0], cmd[1]) not in dvc_tools.ALLOWED_COMMANDS:
            raise AssertionError(f"non-allowlisted command reached the runner: {cmd}")
        match cmd[:2]:
            case ["git", "ls-files"]:
                return "data/raw/dataset.csv.dvc\n"
            case ["git", "rev-parse"]:
                rev = cmd[-1].removesuffix("^{commit}")
                if rev not in revs:
                    raise dvc_tools.ToolError(f"unknown revision {rev}")
                return revs[rev] + "\n"
            case ["git", "show"]:
                commit = cmd[2].split(":")[0]
                return f"outs:\n- md5: {pointers[commit]}\n  size: 1000\n  path: dataset.csv\n"
            case ["git", "tag"]:
                return "data-v1.0\ndata-v2.0\n"
            case ["git", "log"]:
                return f"{COMMIT_V2}\n" if "-1" in cmd else f"{COMMIT_V2}\n{COMMIT_V1}\n"
            case ["dvc", "status"]:
                return "{}"
            case ["dvc", "data"]:
                return "{}"
            case ["dvc", "remote"]:
                return "localremote\t/tmp/remote\t(default)\n"
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(dvc_tools, "_run", run)
    return executed


# ----------------------------------------------------------------------------- Airflow


@pytest.fixture
def fake_airflow(monkeypatch):
    """Route the Airflow client through an in-memory handler; records every request."""

    def _install(handler: Callable[[httpx.Request], httpx.Response] | None = None):
        seen: list[httpx.Request] = []

        def default(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/dagRuns") and request.method == "GET":
                run = {"dag_run_id": "manual__1", "state": "success", "run_type": "manual", "conf": {}}
                return httpx.Response(200, json={"dag_runs": [run], "total_entries": 1})
            if path.endswith("/dagRuns") and request.method == "POST":
                return httpx.Response(200, json={"dag_run_id": "manual__new", "state": "queued"})
            return httpx.Response(404, json={"detail": "not found"})

        def transport(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/auth/token":
                return httpx.Response(201, json={"access_token": "jwt-for-tests"})
            return (handler or default)(request)

        monkeypatch.setattr(airflow_tools, "_transport", httpx.MockTransport(transport))
        return seen

    return _install


# ----------------------------------------------------------------------------- Kubernetes


def make_pod(
    name: str,
    *,
    ready: bool = True,
    restarts: int = 0,
    waiting: str | None = None,
    terminated: str | None = None,
):
    state = SimpleNamespace(
        running=None if waiting else SimpleNamespace(started_at=datetime.now(UTC)),
        waiting=SimpleNamespace(reason=waiting, message="back-off") if waiting else None,
        terminated=None,
    )
    last = SimpleNamespace(
        running=None,
        waiting=None,
        terminated=SimpleNamespace(reason=terminated, exit_code=1, finished_at=datetime.now(UTC))
        if terminated
        else None,
    )
    container = SimpleNamespace(
        name="prediction-api",
        image="prediction-api:1.0.0",
        readiness_probe=None,
        liveness_probe=None,
        resources=None,
        ports=[],
        env_from=[],
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, deletion_timestamp=None, labels={}),
        spec=SimpleNamespace(containers=[container], node_name="mlops-control-plane"),
        status=SimpleNamespace(
            phase="Running",
            pod_ip="10.244.0.5",
            start_time=datetime.now(UTC),
            container_statuses=[
                SimpleNamespace(
                    name="prediction-api", ready=ready, restart_count=restarts, state=state, last_state=last
                )
            ],
            conditions=[
                SimpleNamespace(
                    type="Ready",
                    status="True" if ready else "False",
                    reason=None if ready else "ContainersNotReady",
                    message=None if ready else "containers not ready",
                )
            ],
        ),
    )


def make_deployment(ready: int = 2, desired: int = 2):
    container = SimpleNamespace(image="prediction-api:1.0.0", ports=[], env_from=[])
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="prediction-api",
            labels={"app.kubernetes.io/version": "1.0.0"},
            annotations={"deployment.kubernetes.io/revision": "1"},
        ),
        spec=SimpleNamespace(
            replicas=desired, template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))
        ),
        status=SimpleNamespace(
            ready_replicas=ready,
            updated_replicas=desired,
            available_replicas=ready,
            unavailable_replicas=desired - ready,
            conditions=[],
        ),
    )


@pytest.fixture
def fake_k8s(monkeypatch):
    def _install(pods=None, deployment=None, events=None) -> kubernetes_tools.Apis:
        apis = kubernetes_tools.Apis(core=MagicMock(), apps=MagicMock(), discovery=MagicMock())
        apis.core.list_namespaced_pod.return_value = SimpleNamespace(
            items=pods if pods is not None else [make_pod("prediction-api-a"), make_pod("prediction-api-b")]
        )
        apis.core.list_namespaced_event.return_value = SimpleNamespace(items=events or [])
        apis.apps.list_namespaced_deployment.return_value = SimpleNamespace(
            items=[deployment or make_deployment()]
        )
        monkeypatch.setattr(kubernetes_tools, "_apis", lambda: apis)
        return apis

    return _install
