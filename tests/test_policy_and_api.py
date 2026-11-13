"""Retraining policy, the agent's HTTP interface, and pipeline/inference components."""

from __future__ import annotations

import joblib
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sklearn.linear_model import LogisticRegression

from agent import main as agent_main
from agent.orchestrator import Orchestrator
from agent.tools import build_registry, policy_tools
from agent.tools.base import ToolContext
from tests.conftest import MD5_V1, FakeLLM, calls, make_run, say
from training.common import FEATURES, TARGET
from training.validate import validate

CTX = ToolContext(request_id="test")


# ------------------------------------------------------------------ retraining policy


def _assess(run_id: str, **kwargs):
    return policy_tools.evaluate_retraining_need(policy_tools.RetrainingArgs(run=run_id, **kwargs), CTX)


def test_retraining_not_recommended_without_evidence(fake_mlflow, fake_dvc):
    fake_mlflow(runs=[make_run("r1", 0.97, age_days=2)])
    result = _assess("r1")
    assert result["retraining_recommended"] is False and result["reasons"] == []
    assert any("no live/production performance data" in u for u in result["unknowns"])


@pytest.mark.parametrize(
    ("run_kwargs", "assess_kwargs", "expected_reason"),
    [
        ({"age_days": 45}, {}, "days ago"),
        ({"gate": "failed"}, {}, "quality_gate tag = failed"),
        ({"md5": MD5_V1, "version": "data-v1.0"}, {}, "current dataset is"),
        ({}, {"observed_live_f1": 0.80}, "supplied live f1 0.8"),
    ],
)
def test_retraining_recommended_only_for_documented_policy_reasons(
    fake_mlflow, fake_dvc, run_kwargs, assess_kwargs, expected_reason
):
    fake_mlflow(runs=[make_run("r1", 0.97, **run_kwargs)])
    result = _assess("r1", **assess_kwargs)
    assert result["retraining_recommended"] is True
    assert len(result["reasons"]) == 1 and expected_reason in result["reasons"][0]
    assert "No training run was started" in result["note"]


# ------------------------------------------------------------------ agent HTTP interface


@pytest.fixture
def client(fake_mlflow):
    fake_mlflow(runs=[make_run("r1", 0.97)])
    llm = FakeLLM(calls(("mlflow_list_runs", {})), say("There is one run: r1."))
    orchestrator = Orchestrator(llm, build_registry(), max_steps=4)
    agent_main.app.dependency_overrides[agent_main.get_orchestrator] = lambda: orchestrator
    yield TestClient(agent_main.app)
    agent_main.app.dependency_overrides.clear()


def test_chat_endpoint_returns_answer_with_tool_evidence(client):
    response = client.post("/chat", json={"message": "list runs"}, headers={"X-Request-ID": "abc123"})
    body = response.json()
    assert response.status_code == 200 and body["request_id"] == "abc123"
    assert body["answer"] == "There is one run: r1."
    assert body["tool_calls"][0]["tool"] == "mlflow_list_runs"
    assert body["tool_calls"][0]["result"]["runs"][0]["run_id"] == "r1"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "x" * 5000},
        {"message": "hi", "confirm": True},
        {"message": "hi", "session_id": "../../etc"},
    ],
)
def test_chat_endpoint_rejects_invalid_requests(client, payload):
    assert client.post("/chat", json=payload).status_code == 422


def test_every_module_exposes_at_least_three_tools_and_mutations_are_flagged(client):
    tools = client.get("/tools").json()["tools"]
    per_category: dict[str, int] = {}
    for tool in tools:
        per_category[tool["category"]] = per_category.get(tool["category"], 0) + 1
    assert all(per_category[c] >= 3 for c in ("dvc", "mlflow", "airflow", "kubernetes"))
    assert {t["name"] for t in tools if t["changes_state"]} == {
        "airflow_trigger_dag",
        "k8s_restart_deployment",
        "k8s_scale_deployment",
    }
    assert all(t["input_schema"].get("additionalProperties") is False for t in tools)


# ------------------------------------------------------------------ pipeline + inference


def _frame(rows: int = 300) -> pd.DataFrame:
    frame = pd.DataFrame({feature: [float(i % 7) for i in range(rows)] for feature in FEATURES})
    frame[TARGET] = [i % 2 for i in range(rows)]
    return frame


def test_validation_accepts_good_data_and_names_each_problem():
    assert validate(_frame()) == []
    assert "need at least 200" in validate(_frame(50))[0]
    broken = _frame()
    broken.loc[:10, FEATURES[0]] = None
    broken[TARGET] = 1
    problems = " ".join(validate(broken))
    assert "null values" in problems and "single class" in problems
    assert "missing columns" in validate(_frame().drop(columns=[TARGET]))[0]


def test_prediction_api_is_not_ready_without_a_model_and_serves_with_one(tmp_path, monkeypatch):
    from api import main as api_main

    monkeypatch.setattr(api_main, "MODEL_PATH", tmp_path / "missing.pkl")
    with TestClient(api_main.app) as http:
        assert http.get("/health/live").status_code == 200
        assert http.get("/health/ready").status_code == 503
        assert http.post("/predict", json={"instances": [{"a": 1.0}]}).status_code == 503

    frame = _frame()
    model = LogisticRegression(max_iter=500).fit(frame[FEATURES], frame[TARGET])
    joblib.dump(model, tmp_path / "model.pkl")
    monkeypatch.setattr(api_main, "MODEL_PATH", tmp_path / "model.pkl")
    with TestClient(api_main.app) as http:
        assert http.get("/health/ready").status_code == 200
        row = {feature: 1.0 for feature in FEATURES}
        body = http.post("/predict", json={"instances": [row]}).json()
        assert body["predictions"][0] in (0, 1) and 0 <= body["probabilities"][0] <= 1
        assert http.post("/predict", json={"instances": [{"only": 1.0}]}).status_code == 422
