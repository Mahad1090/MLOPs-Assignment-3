"""Agent loop: tool selection, multi-tool requests, failures, honesty and safety."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from mlflow.exceptions import MlflowException

from agent.schemas import NoArgs
from agent.tools.base import Tool, ToolContext
from tests.conftest import MD5_V2, calls, make_run, say


def test_selected_tool_is_executed_and_its_result_reaches_the_model(make_agent, fake_mlflow):
    fake_mlflow(runs=[make_run("r1", 0.95), make_run("r2", 0.97)])
    agent, llm = make_agent(calls(("mlflow_best_run", {"metric": "f1"})), say("Best run is r2."))

    response = agent.chat("Which run has the best F1?")

    assert [c.tool for c in response.tool_calls] == ["mlflow_best_run"]
    assert response.tool_calls[0].status == "ok"
    assert response.tool_calls[0].result["best_run"]["run_id"] == "r2"
    shown = json.loads(llm.tool_messages()[0]["content"])
    assert shown["status"] == "ok" and shown["data"]["best_run"]["run_id"] == "r2"
    assert response.failed_tools == [] and response.answer == "Best run is r2."


def test_multi_tool_request_gathers_evidence_from_all_four_systems(
    make_agent, fake_mlflow, fake_dvc, fake_airflow, fake_k8s
):
    fake_mlflow(runs=[make_run("r1", 0.95), make_run("r2", 0.97)])
    requests = fake_airflow()
    apis = fake_k8s()
    agent, _ = make_agent(
        calls(("mlflow_best_run", {})),
        calls(
            ("dvc_find_version", {"md5": MD5_V2}),
            ("k8s_list_workloads", {}),
            ("airflow_list_dag_runs", {"dag_id": "training_pipeline"}),
            ("evaluate_retraining_need", {"run": "r2"}),
        ),
        say("Known facts ... Uncertainties ... Recommended actions ..."),
    )

    response = agent.chat(
        "Find the best recent model, identify the dataset version used to train it, check the "
        "deployment status, and explain whether retraining is necessary."
    )

    by_tool = {c.tool: c for c in response.tool_calls}
    assert all(c.status == "ok" for c in response.tool_calls)
    assert by_tool["dvc_find_version"].result["version_tags"] == ["data-v2.0"]
    assert by_tool["k8s_list_workloads"].result["deployments"][0]["ready_replicas"] == 2
    assert by_tool["airflow_list_dag_runs"].result["runs"][0]["state"] == "success"
    assert by_tool["evaluate_retraining_need"].result["retraining_recommended"] is False
    # Retraining must never be started by this request.
    assert not [r for r in requests if r.method == "POST" and r.url.path.endswith("/dagRuns")]
    apis.apps.patch_namespaced_deployment.assert_not_called()
    assert response.pending_actions == []


def test_agent_does_not_invent_results_when_a_tool_is_unavailable(make_agent, fake_mlflow):
    fake_mlflow(fail_with=MlflowException("API request failed with exception Max retries exceeded"))
    # Even if the model ignores its instructions and makes numbers up...
    agent, llm = make_agent(calls(("mlflow_best_run", {})), say("The best run has F1 0.99."))

    response = agent.chat("What is our best model?")

    record = response.tool_calls[0]
    assert record.status == "unavailable" and record.result is None
    # ...the model was given no data to work from,
    shown = json.loads(llm.tool_messages()[0]["content"])
    assert "data" not in shown and "No operational data" in shown["note"]
    # ...and the runtime marks the answer as unsupported, independent of the model.
    assert response.failed_tools == ["mlflow_best_run"]
    assert "Tool availability notice" in response.answer
    assert "not backed by tool evidence" in response.answer


def test_tool_timeout_is_reported_not_hung(make_agent):
    agent, _ = make_agent(calls(("slow_tool", {})), say("done"), timeout=0.2)
    agent.registry.register(Tool("slow_tool", "sleeps", NoArgs, lambda a, c: time.sleep(3), category="test"))
    started = time.perf_counter()

    response = agent.chat("run the slow thing")

    assert time.perf_counter() - started < 2
    assert response.tool_calls[0].status == "timeout"
    assert response.failed_tools == ["slow_tool"]


@pytest.mark.parametrize(
    ("tool", "arguments", "expected_error"),
    [
        ("mlflow_list_runs", {"max_results": "lots"}, "max_results"),
        ("mlflow_list_runs", {"max_results": 5000}, "max_results"),
        ("dvc_compare_versions", {"rev_a": "--upload-pack=evil", "rev_b": "HEAD"}, "rev_a"),
        ("k8s_pod_logs", {"pod_name": "pod; rm -rf /"}, "pod_name"),
        ("no_such_tool", {}, "unknown tool"),
    ],
)
def test_invalid_tool_requests_are_rejected_before_execution(
    make_agent, fake_mlflow, fake_dvc, fake_k8s, tool, arguments, expected_error
):
    client, apis = fake_mlflow(), fake_k8s()
    client.search_runs = lambda *a, **k: pytest.fail("backend must not be called")
    agent, _ = make_agent(calls((tool, arguments)), say("ok"))

    response = agent.chat("do something invalid")

    record = response.tool_calls[0]
    assert record.status == "invalid_input" and expected_error in record.error
    assert fake_dvc == [] and not apis.core.method_calls


def test_service_failure_is_surfaced_as_error(make_agent, fake_airflow):
    fake_airflow(lambda request: httpx.Response(500, text="scheduler database is locked"))
    agent, _ = make_agent(
        calls(("airflow_list_dag_runs", {"dag_id": "training_pipeline"})), say("could not check")
    )

    response = agent.chat("How did the last pipeline run go?")

    assert response.tool_calls[0].status == "error"
    assert "HTTP 500" in response.tool_calls[0].error
    assert "Tool availability notice" in response.answer


def test_llm_outage_returns_an_honest_message(make_agent):
    from agent.llm import LLMError

    def boom(_messages):
        raise LLMError("rate limited")

    agent, _ = make_agent(boom)
    response = agent.chat("anything")
    assert "language model could not be reached" in response.answer


def test_step_budget_stops_runaway_tool_loops(make_agent, fake_mlflow):
    fake_mlflow(runs=[make_run("r1", 0.9)])
    loop = [calls(("mlflow_list_experiments", {}))] * 10
    agent, llm = make_agent(*loop[:3], say("partial answer"), max_steps=3)

    response = agent.chat("loop forever")

    assert len(response.tool_calls) == 3 and response.answer == "partial answer"
    assert "tool budget exhausted" in llm.requests[-1][-1]["content"]


def test_tool_output_from_earlier_turns_is_not_replayed(make_agent, fake_mlflow):
    fake_mlflow(runs=[make_run("r1", 0.9)])
    agent, llm = make_agent(calls(("mlflow_list_runs", {})), say("one run"), say("hello again"))
    first = agent.chat("list runs")
    agent.chat("thanks", session_id=first.session_id)
    assert [m["role"] for m in llm.requests[-1]] == ["system", "user", "assistant", "user"]


def test_structured_logs_record_tool_status_duration_and_request_id(make_agent, fake_mlflow, caplog):
    fake_mlflow(runs=[make_run("r1", 0.9)])
    agent, _ = make_agent(calls(("mlflow_list_runs", {"max_results": 3})), say("ok"))
    caplog.set_level("INFO", logger="mlops_agent")
    import logging

    logging.getLogger("mlops_agent").addHandler(caplog.handler)
    try:
        agent.chat("list runs", request_id="req123")
    finally:
        logging.getLogger("mlops_agent").removeHandler(caplog.handler)

    events = [r for r in caplog.records if r.getMessage() == "tool_call"]
    assert len(events) == 1
    fields = events[0].fields
    assert fields["tool"] == "mlflow_list_runs" and fields["status"] == "ok"
    assert fields["request_id"] == "req123" and fields["duration_ms"] >= 0
    assert {r.getMessage() for r in caplog.records} >= {"request_started", "request_finished"}


def test_context_passed_to_tools_carries_request_id(make_agent):
    seen = {}
    agent, _ = make_agent(calls(("probe", {})), say("ok"))

    def probe(args: NoArgs, ctx: ToolContext):
        seen["ctx"] = ctx
        return {}

    agent.registry.register(Tool("probe", "probe", NoArgs, probe, category="test"))
    agent.chat("x", request_id="abc")
    assert seen["ctx"].request_id == "abc" and seen["ctx"].confirmed is False


def test_retraining_verdict_is_not_accepted_until_all_four_systems_were_queried(
    make_agent, fake_mlflow, fake_dvc, fake_airflow, fake_k8s
):
    fake_mlflow(runs=[make_run("r1", 0.97)])
    fake_airflow()
    fake_k8s()
    agent, llm = make_agent(
        calls(("mlflow_best_run", {}), ("dvc_find_version", {"md5": MD5_V2}), ("k8s_list_workloads", {})),
        calls(("evaluate_retraining_need", {"run": "r1"})),
        say("premature answer without Airflow"),
        calls(("airflow_list_dag_runs", {"dag_id": "training_pipeline"})),
        say("complete answer"),
    )

    response = agent.chat("is retraining necessary?")

    assert response.answer == "complete answer"
    assert "airflow_list_dag_runs" in [c.tool for c in response.tool_calls]
    assert any("you must also query: ['airflow']" in m["content"] for m in llm.requests[-1])
