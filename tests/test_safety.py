"""Permission model: confirmation for state changes, namespace and DAG allowlists, secrets."""

from __future__ import annotations

import json
import logging

from agent.logging_utils import REDACTED, JsonFormatter, redact
from agent.tools import kubernetes_tools
from agent.tools.base import ToolContext
from tests.conftest import FAKE_AIRFLOW_PASSWORD, calls, say


def _post_dag_runs(requests):
    return [r for r in requests if r.method == "POST" and r.url.path.endswith("/dagRuns")]


def test_k8s_restart_without_confirmation_is_not_executed(make_agent, fake_k8s):
    apis = fake_k8s()
    agent, llm = make_agent(calls(("k8s_restart_deployment", {})), say("Please confirm."))

    response = agent.chat("restart the prediction api")

    apis.apps.patch_namespaced_deployment.assert_not_called()
    assert response.tool_calls[0].status == "confirmation_required"
    assert len(response.pending_actions) == 1
    assert "nothing has been executed" in response.answer
    assert json.loads(llm.tool_messages()[0]["content"])["data"]["executed"] is False


def test_k8s_restart_runs_exactly_once_after_user_confirmation(make_agent, fake_k8s):
    apis = fake_k8s()
    apis.apps.patch_namespaced_deployment.return_value = (
        apis.apps.list_namespaced_deployment.return_value.items[0]
    )
    agent, _ = make_agent(calls(("k8s_restart_deployment", {})), say("Confirm?"), say("Restarted."))
    first = agent.chat("restart the prediction api")
    action_id = first.pending_actions[0].action_id

    second = agent.chat("yes", session_id=first.session_id, confirm_action_id=action_id)

    apis.apps.patch_namespaced_deployment.assert_called_once()
    name, namespace, _patch = apis.apps.patch_namespaced_deployment.call_args.args
    assert (name, namespace) == ("prediction-api", "mlops")
    assert second.tool_calls[0].status == "ok" and second.pending_actions == []
    # The approval is single-use.
    third = agent.chat("again", session_id=first.session_id, confirm_action_id=action_id)
    assert "no pending action" in third.answer
    apis.apps.patch_namespaced_deployment.assert_called_once()


def test_unauthorized_k8s_modifications_are_rejected(make_agent, fake_k8s):
    apis = fake_k8s()
    agent, _ = make_agent(
        calls(
            ("k8s_restart_deployment", {"namespace": "kube-system", "deployment": "coredns"}),
            ("k8s_scale_deployment", {"deployment": "some-other-app", "replicas": 2}),
            ("k8s_scale_deployment", {"replicas": 15}),
            ("k8s_restart_deployment", {"confirm": True}),  # the model cannot approve itself
            ("k8s_list_workloads", {"namespace": "kube-system"}),  # reads are confined too
        ),
        say("denied"),
    )

    response = agent.chat("restart coredns in kube-system and scale everything up")

    statuses = [c.status for c in response.tool_calls]
    assert statuses == ["denied", "denied", "denied", "invalid_input", "denied"]
    assert "approved namespace" in response.tool_calls[0].error
    assert response.pending_actions == []  # nothing is even offered for confirmation
    assert not apis.apps.method_calls and not apis.core.method_calls


def test_mutating_tool_function_refuses_unconfirmed_context(fake_k8s):
    """Defence in depth: even called directly, the tool checks the confirmation flag."""
    apis = fake_k8s()
    result = kubernetes_tools.TOOLS[-2].func  # k8s_restart_deployment
    try:
        result(kubernetes_tools.DeploymentArgs(), ToolContext(request_id="t", confirmed=False))
    except kubernetes_tools.PermissionDeniedError:
        pass
    else:
        raise AssertionError("expected PermissionDeniedError")
    apis.apps.patch_namespaced_deployment.assert_not_called()


def test_confirmation_cannot_be_used_from_another_session(make_agent, fake_k8s):
    apis = fake_k8s()
    agent, _ = make_agent(calls(("k8s_restart_deployment", {})), say("Confirm?"))
    first = agent.chat("restart", session_id="alice")

    other = agent.chat("yes", session_id="mallory", confirm_action_id=first.pending_actions[0].action_id)

    assert "no pending action" in other.answer
    apis.apps.patch_namespaced_deployment.assert_not_called()


def test_training_is_never_triggered_without_confirmation(make_agent, fake_airflow):
    requests = fake_airflow()
    agent, _ = make_agent(
        calls(("airflow_trigger_dag", {"dag_id": "training_pipeline"})), say("Confirm?"), say("Done")
    )

    first = agent.chat("is retraining necessary?")
    assert first.tool_calls[0].status == "confirmation_required"
    assert _post_dag_runs(requests) == []

    agent.chat("yes", session_id=first.session_id, confirm_action_id=first.pending_actions[0].action_id)
    assert len(_post_dag_runs(requests)) == 1


def test_unapproved_dag_cannot_be_triggered(make_agent, fake_airflow):
    requests = fake_airflow()
    agent, _ = make_agent(calls(("airflow_trigger_dag", {"dag_id": "drop_database"})), say("no"))

    response = agent.chat("trigger drop_database")

    assert response.tool_calls[0].status == "denied"
    assert response.pending_actions == [] and requests == []


def test_secrets_are_redacted_from_logs():
    payload = {
        "tool": "airflow_list_dags",
        "arguments": {"password": FAKE_AIRFLOW_PASSWORD, "nested": {"api_key": "gsk_abcdefgh12345678"}},
        "error": "request with Authorization: Bearer eyJhbGciOi.payload.sig failed",
    }
    cleaned = json.dumps(redact(payload))
    assert FAKE_AIRFLOW_PASSWORD not in cleaned and "gsk_abcdefgh" not in cleaned
    assert "eyJhbGciOi" not in cleaned and REDACTED in cleaned

    record = logging.LogRecord("mlops_agent.tools", logging.INFO, "", 0, "tool_call", None, None)
    record.fields = payload
    line = JsonFormatter().format(record)
    assert FAKE_AIRFLOW_PASSWORD not in line and json.loads(line)["tool"] == "airflow_list_dags"


def test_airflow_credentials_never_appear_in_agent_logs(make_agent, fake_airflow, caplog):
    fake_airflow()
    agent, _ = make_agent(calls(("airflow_list_dag_runs", {"dag_id": "training_pipeline"})), say("ok"))
    logging.getLogger("mlops_agent").addHandler(caplog.handler)
    caplog.set_level("INFO", logger="mlops_agent")
    try:
        response = agent.chat("runs?")
    finally:
        logging.getLogger("mlops_agent").removeHandler(caplog.handler)

    dumped = " ".join(str(getattr(r, "fields", "")) for r in caplog.records)
    assert caplog.records and FAKE_AIRFLOW_PASSWORD not in dumped and "jwt-for-tests" not in dumped
    assert FAKE_AIRFLOW_PASSWORD not in response.model_dump_json()
