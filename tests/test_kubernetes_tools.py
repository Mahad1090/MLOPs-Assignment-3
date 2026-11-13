"""Kubernetes tools against fake API clients."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from urllib3.exceptions import MaxRetryError

from agent.tools import kubernetes_tools as k8s
from agent.tools.base import PermissionDeniedError, ToolContext, ToolUnavailableError
from tests.conftest import make_deployment, make_pod

CTX = ToolContext(request_id="test")


def _warning(reason: str, pod: str, message: str):
    return SimpleNamespace(
        type="Warning",
        reason=reason,
        message=message,
        count=7,
        event_time=None,
        last_timestamp=datetime.now(UTC),
        involved_object=SimpleNamespace(kind="Pod", name=pod),
        metadata=SimpleNamespace(creation_timestamp=datetime.now(UTC)),
    )


def test_list_workloads_reports_replicas_and_version(fake_k8s):
    fake_k8s(deployment=make_deployment(ready=1, desired=2))
    result = k8s.list_workloads(k8s.NamespaceArgs(), CTX)
    deployment = result["deployments"][0]
    assert (deployment["desired_replicas"], deployment["ready_replicas"]) == (2, 1)
    assert deployment["version_label"] == "1.0.0"
    assert [p["name"] for p in result["pods"]] == ["prediction-api-a", "prediction-api-b"]


def test_unhealthy_pods_restarts_and_warning_events_are_identified(fake_k8s):
    fake_k8s(
        pods=[
            make_pod("api-ok"),
            make_pod("api-crash", ready=False, restarts=4, waiting="CrashLoopBackOff", terminated="Error"),
        ],
        events=[
            _warning(
                "Unhealthy", "api-crash", "Readiness probe failed: HTTP probe failed with statuscode: 503"
            )
        ],
    )
    result = k8s.unhealthy_pods(k8s.NamespaceArgs(), CTX)

    assert result["total_pods"] == 2 and result["healthy_pods"] == 1
    bad = result["unhealthy_pods"][0]
    assert bad["name"] == "api-crash" and bad["waiting_reasons"] == ["CrashLoopBackOff"]
    assert result["container_restarts"][0]["restart_count"] == 4
    assert result["container_restarts"][0]["last_terminated_state"]["reason"] == "Error"
    assert "statuscode: 503" in result["warning_events"][0]["message"]


def test_all_healthy_cluster_reports_nothing_unhealthy(fake_k8s):
    fake_k8s()
    result = k8s.unhealthy_pods(k8s.NamespaceArgs(), CTX)
    assert result["unhealthy_pods"] == [] and result["container_restarts"] == []


def test_pod_logs_are_tailed_from_the_approved_namespace(fake_k8s):
    apis = fake_k8s()
    apis.core.read_namespaced_pod_log.return_value = "line1\nmodel_load_failed FileNotFoundError\n"
    result = k8s.pod_logs(k8s.PodLogArgs(pod_name="api-crash", tail_lines=20), CTX)
    assert result["lines"][-1] == "model_load_failed FileNotFoundError"
    args, kwargs = apis.core.read_namespaced_pod_log.call_args
    assert args == ("api-crash", "mlops") and kwargs["tail_lines"] == 20


@pytest.mark.parametrize("namespace", ["kube-system", "default", "MLOPS", "mlops "])
def test_every_tool_is_confined_to_the_approved_namespace(fake_k8s, namespace):
    apis = fake_k8s()
    for func, args in [
        (k8s.list_workloads, k8s.NamespaceArgs(namespace=namespace)),
        (k8s.services, k8s.NamespaceArgs(namespace=namespace)),
        (k8s.pod_logs, k8s.PodLogArgs(pod_name="x", namespace=namespace)),
        (k8s.restart_deployment, k8s.DeploymentArgs(namespace=namespace)),
    ]:
        with pytest.raises(PermissionDeniedError, match="approved namespace"):
            func(args, ToolContext("t", confirmed=True))
    assert not apis.core.method_calls and not apis.apps.method_calls


def test_scaling_is_capped_even_when_confirmed(fake_k8s):
    apis = fake_k8s()
    with pytest.raises(PermissionDeniedError, match="not permitted"):
        k8s.scale_deployment(k8s.ScaleArgs(replicas=10), ToolContext("t", confirmed=True))
    apis.apps.patch_namespaced_deployment_scale.assert_not_called()


def test_unreachable_api_server_maps_to_unavailable(fake_k8s):
    apis = fake_k8s()
    apis.core.list_namespaced_pod.side_effect = MaxRetryError(None, "/api/v1/pods")
    with pytest.raises(ToolUnavailableError, match="unreachable"):
        k8s.unhealthy_pods(k8s.NamespaceArgs(), CTX)


def test_probe_named_ports_are_resolved_to_numbers():
    probe = SimpleNamespace(
        http_get=SimpleNamespace(path="/health/ready", port="http"),
        period_seconds=5,
        failure_threshold=3,
        initial_delay_seconds=3,
    )
    ports = [SimpleNamespace(name="http", container_port=8000)]
    assert k8s._probe(probe, ports)["http_get"] == "/health/ready on port http = 8000"
    assert k8s._probe(None, ports) is None
