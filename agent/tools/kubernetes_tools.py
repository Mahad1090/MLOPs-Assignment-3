"""Kubernetes operations tools (official Python client).

Permission model
----------------
* Every call, read or write, is restricted to the single approved namespace.
* The two state-changing tools (rollout restart, scale) are limited to approved
  Deployments and only run after the user confirmed that exact action.
* There is no tool for delete, exec, apply or secret access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from agent.config import get_settings
from agent.schemas import ToolArgs
from agent.tools.base import (
    PermissionDeniedError,
    Tool,
    ToolContext,
    ToolError,
    ToolUnavailableError,
    tail_within,
)

_NAME = r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$"
_TIMEOUT = 10  # seconds per API request


@dataclass
class Apis:
    core: Any
    apps: Any
    discovery: Any


def _apis() -> Apis:
    """Build API clients from the configured kubeconfig context (or in-cluster config)."""
    from kubernetes.config.config_exception import ConfigException

    from kubernetes import client, config

    cfg = client.Configuration()
    try:
        config.load_kube_config(context=get_settings().k8s_context, client_configuration=cfg)
    except ConfigException:
        try:
            config.load_incluster_config(client_configuration=cfg)
        except ConfigException as exc:
            raise ToolUnavailableError(
                f"no usable Kubernetes credentials for context '{get_settings().k8s_context}'"
            ) from exc
    cfg.retries = 0
    api_client = client.ApiClient(cfg)
    return Apis(
        core=client.CoreV1Api(api_client),
        apps=client.AppsV1Api(api_client),
        discovery=client.DiscoveryV1Api(api_client),
    )


def _call(fn, *args, **kwargs):
    """Run a Kubernetes API call, mapping failures onto the agent's error types."""
    from kubernetes.client.exceptions import ApiException
    from urllib3.exceptions import HTTPError

    kwargs.setdefault("_request_timeout", _TIMEOUT)
    try:
        return fn(*args, **kwargs)
    except ApiException as exc:
        if exc.status == 404:
            raise ToolError("Kubernetes: resource not found") from exc
        if exc.status in (401, 403):
            raise PermissionDeniedError(
                f"Kubernetes API refused the request (HTTP {exc.status} {exc.reason})"
            ) from exc
        raise ToolError(f"Kubernetes API error HTTP {exc.status}: {exc.reason}") from exc
    except (HTTPError, ConnectionError, OSError) as exc:
        raise ToolUnavailableError(f"Kubernetes API server is unreachable ({type(exc).__name__})") from exc


def approved_namespace(requested: str = "") -> str:
    approved = get_settings().k8s_namespace
    if requested and requested != approved:
        raise PermissionDeniedError(f"namespace '{requested}' is outside the approved namespace '{approved}'")
    return approved


def _approved_deployment(name: str) -> str:
    name = name or sorted(get_settings().approved_deployments)[0]
    if name not in get_settings().approved_deployments:
        raise PermissionDeniedError(
            f"deployment '{name}' is not approved (approved: {sorted(get_settings().approved_deployments)})"
        )
    return name


def _ts(value: Any) -> str | None:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else None


def _state(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    if getattr(state, "running", None):
        return {"state": "running", "started_at": _ts(state.running.started_at)}
    if getattr(state, "waiting", None):
        return {
            "state": "waiting",
            "reason": state.waiting.reason,
            "message": (state.waiting.message or "")[:300],
        }
    if getattr(state, "terminated", None):
        t = state.terminated
        return {
            "state": "terminated",
            "reason": t.reason,
            "exit_code": t.exit_code,
            "finished_at": _ts(t.finished_at),
        }
    return None


def _pod_view(pod: Any) -> dict[str, Any]:
    statuses = pod.status.container_statuses or []
    waiting = [s.state.waiting.reason for s in statuses if s.state and s.state.waiting]
    return {
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "ready": bool(statuses) and all(s.ready for s in statuses),
        "containers_ready": f"{sum(1 for s in statuses if s.ready)}/{len(pod.spec.containers)}",
        "restarts": sum(s.restart_count for s in statuses),
        "waiting_reasons": waiting,
        "terminating": pod.metadata.deletion_timestamp is not None,
        "node": pod.spec.node_name,
        "pod_ip": pod.status.pod_ip,
        "started_at": _ts(pod.status.start_time),
        "images": [c.image for c in pod.spec.containers],
    }


def _deployment_view(dep: Any) -> dict[str, Any]:
    status = dep.status
    labels = dep.metadata.labels or {}
    return {
        "name": dep.metadata.name,
        "desired_replicas": dep.spec.replicas,
        "ready_replicas": status.ready_replicas or 0,
        "updated_replicas": status.updated_replicas or 0,
        "available_replicas": status.available_replicas or 0,
        "unavailable_replicas": status.unavailable_replicas or 0,
        "images": [c.image for c in dep.spec.template.spec.containers],
        "version_label": labels.get("app.kubernetes.io/version"),
        "revision": (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (status.conditions or [])
        ],
    }


def _event_view(event: Any) -> dict[str, Any]:
    obj = event.involved_object
    return {
        "type": event.type,
        "reason": event.reason,
        "object": f"{obj.kind}/{obj.name}",
        "message": (event.message or "")[:200],
        "count": event.count,
        "last_seen": _ts(event.last_timestamp or event.event_time or event.metadata.creation_timestamp),
    }


def _events(apis: Apis, namespace: str, warnings_only: bool, obj_name: str = "", limit: int = 8):
    selector = "type=Warning" if warnings_only else None
    events = _call(apis.core.list_namespaced_event, namespace, field_selector=selector).items
    views = [_event_view(e) for e in events if not obj_name or e.involved_object.name == obj_name]
    return sorted(views, key=lambda e: e["last_seen"] or "")[-limit:]


def _probe(probe: Any) -> dict[str, Any] | None:
    if probe is None:
        return None
    http = probe.http_get
    return {
        "http_get": f"{http.path} on port {http.port}" if http else None,
        "period_seconds": probe.period_seconds,
        "failure_threshold": probe.failure_threshold,
        "initial_delay_seconds": probe.initial_delay_seconds,
    }


# --------------------------------------------------------------------------- tools


class NamespaceArgs(ToolArgs):
    namespace: str = Field(default="", max_length=63, description="Empty = approved namespace.")


class PodArgs(ToolArgs):
    pod_name: str = Field(pattern=_NAME, description="Exact pod name from k8s_list_workloads.")
    namespace: str = Field(default="", max_length=63, description="Empty = approved namespace.")


class PodLogArgs(PodArgs):
    tail_lines: int = Field(default=15, ge=1, le=200)
    previous: bool = Field(default=False, description="Logs of the previous (crashed) container instance.")


class EventArgs(NamespaceArgs):
    warnings_only: bool = Field(default=True)


class DeploymentArgs(ToolArgs):
    deployment: str = Field(default="", max_length=253, description="Empty = the application.")
    namespace: str = Field(default="", max_length=63, description="Empty = approved namespace.")


class ScaleArgs(DeploymentArgs):
    replicas: int = Field(ge=1, le=20, description="Desired replica count.")


def list_workloads(args: NamespaceArgs, ctx: ToolContext) -> dict[str, Any]:
    namespace = approved_namespace(args.namespace)
    apis = _apis()
    deployments = _call(apis.apps.list_namespaced_deployment, namespace).items
    pods = _call(apis.core.list_namespaced_pod, namespace).items
    return {
        "namespace": namespace,
        "deployments": [_deployment_view(d) for d in deployments],
        "pods": [_pod_view(p) for p in pods],
    }


def pod_status(args: PodArgs, ctx: ToolContext) -> dict[str, Any]:
    namespace = approved_namespace(args.namespace)
    apis = _apis()
    pod = _call(apis.core.read_namespaced_pod, args.pod_name, namespace)
    containers = []
    statuses = {s.name: s for s in (pod.status.container_statuses or [])}
    for container in pod.spec.containers:
        status = statuses.get(container.name)
        resources = container.resources
        containers.append(
            {
                "name": container.name,
                "image": container.image,
                "ready": bool(status and status.ready),
                "restart_count": status.restart_count if status else 0,
                "state": _state(status.state) if status else None,
                "last_terminated_state": _state(status.last_state) if status else None,
                "readiness_probe": _probe(container.readiness_probe),
                "liveness_probe": _probe(container.liveness_probe),
                "resources": {
                    "requests": dict(resources.requests or {}) if resources else {},
                    "limits": dict(resources.limits or {}) if resources else {},
                },
            }
        )
    return {
        **_pod_view(pod),
        "namespace": namespace,
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in (pod.status.conditions or [])
        ],
        "containers": containers,
        "events": _events(apis, namespace, warnings_only=False, obj_name=args.pod_name, limit=6),
    }


def pod_logs(args: PodLogArgs, ctx: ToolContext) -> dict[str, Any]:
    namespace = approved_namespace(args.namespace)
    text = _call(
        _apis().core.read_namespaced_pod_log,
        args.pod_name,
        namespace,
        tail_lines=args.tail_lines,
        previous=args.previous,
    )
    lines = tail_within((text or "").splitlines())
    return {
        "pod": args.pod_name,
        "namespace": namespace,
        "previous_container": args.previous,
        "line_count": len(lines),
        "lines": lines,
    }


def services(args: NamespaceArgs, ctx: ToolContext) -> dict[str, Any]:
    namespace = approved_namespace(args.namespace)
    apis = _apis()
    slices = _call(apis.discovery.list_namespaced_endpoint_slice, namespace).items
    out = []
    for svc in _call(apis.core.list_namespaced_service, namespace).items:
        ready, not_ready = [], []
        for item in slices:
            if (item.metadata.labels or {}).get("kubernetes.io/service-name") != svc.metadata.name:
                continue
            for endpoint in item.endpoints or []:
                is_ready = endpoint.conditions is None or endpoint.conditions.ready is not False
                target = endpoint.target_ref.name if endpoint.target_ref else None
                entry = {"addresses": endpoint.addresses, "pod": target}
                (ready if is_ready else not_ready).append(entry)
        out.append(
            {
                "name": svc.metadata.name,
                "type": svc.spec.type,
                "cluster_ip": svc.spec.cluster_ip,
                "ports": [
                    {"name": p.name, "port": p.port, "target_port": str(p.target_port)}
                    for p in (svc.spec.ports or [])
                ],
                "selector": svc.spec.selector,
                "ready_endpoints": ready,
                "not_ready_endpoints": not_ready,
                "can_serve_traffic": bool(ready),
            }
        )
    return {"namespace": namespace, "services": out}


def unhealthy_pods(args: NamespaceArgs, ctx: ToolContext) -> dict[str, Any]:
    namespace = approved_namespace(args.namespace)
    apis = _apis()
    pods = _call(apis.core.list_namespaced_pod, namespace).items
    unhealthy, restarted = [], []
    for pod in pods:
        view = _pod_view(pod)
        statuses = pod.status.container_statuses or []
        if not view["ready"] and pod.status.phase != "Succeeded":
            view["container_states"] = {s.name: _state(s.state) for s in statuses}
            view["not_ready_conditions"] = [
                {"type": c.type, "reason": c.reason, "message": c.message}
                for c in (pod.status.conditions or [])
                if c.status != "True"
            ]
            unhealthy.append(view)
        for status in statuses:
            if status.restart_count:
                restarted.append(
                    {
                        "pod": pod.metadata.name,
                        "container": status.name,
                        "restart_count": status.restart_count,
                        "last_terminated_state": _state(status.last_state),
                    }
                )
    return {
        "namespace": namespace,
        "total_pods": len(pods),
        "healthy_pods": len(pods) - len(unhealthy),
        "unhealthy_pods": unhealthy,
        "container_restarts": restarted,
        "warning_events": _events(apis, namespace, warnings_only=True),
    }


def recent_events(args: EventArgs, ctx: ToolContext) -> dict[str, Any]:
    namespace = approved_namespace(args.namespace)
    events = _events(_apis(), namespace, warnings_only=args.warnings_only, limit=12)
    return {"namespace": namespace, "warnings_only": args.warnings_only, "events": events}


def deployed_version(args: DeploymentArgs, ctx: ToolContext) -> dict[str, Any]:
    namespace = approved_namespace(args.namespace)
    name = _approved_deployment(args.deployment)
    apis = _apis()
    dep = _call(apis.apps.read_namespaced_deployment, name, namespace)
    view = _deployment_view(dep)
    container = dep.spec.template.spec.containers[0]
    image = container.image
    result: dict[str, Any] = {
        "namespace": namespace,
        "deployment": view,
        "image": image,
        "image_tag": image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else "latest",
        "rollout_complete": view["ready_replicas"] == view["desired_replicas"]
        and view["updated_replicas"] == view["desired_replicas"],
    }
    config: dict[str, str] = {}
    for source in container.env_from or []:
        if source.config_map_ref:
            try:
                cm = _call(apis.core.read_namespaced_config_map, source.config_map_ref.name, namespace)
                config.update(cm.data or {})
            except ToolError as exc:
                result["configmap_error"] = str(exc)
    result["configmap"] = config
    try:  # ask the running application itself, through the API-server service proxy
        port = container.ports[0].container_port if container.ports else 8000
        raw = _call(
            apis.core.connect_get_namespaced_service_proxy_with_path,
            f"{name}:{port}",
            namespace,
            path="version",
            _preload_content=False,
        )
        result["live_version_endpoint"] = json.loads(raw.data)
    except (ToolError, ValueError) as exc:
        result["live_version_endpoint"] = None
        result["live_version_error"] = f"application did not answer /version: {exc}"[:300]
    return result


def check_mutation_allowed(args: DeploymentArgs) -> None:
    approved_namespace(args.namespace)
    _approved_deployment(args.deployment)
    replicas = getattr(args, "replicas", None)
    if replicas is not None and replicas > get_settings().k8s_max_replicas:
        raise PermissionDeniedError(
            f"scaling above {get_settings().k8s_max_replicas} replicas is not permitted"
        )


def _require_confirmation(ctx: ToolContext) -> None:
    if not ctx.confirmed:  # defence in depth; the executor already enforces this
        raise PermissionDeniedError("changing cluster state requires explicit user confirmation")


def restart_deployment(args: DeploymentArgs, ctx: ToolContext) -> dict[str, Any]:
    check_mutation_allowed(args)
    _require_confirmation(ctx)
    namespace, name = approved_namespace(args.namespace), _approved_deployment(args.deployment)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    patch = {
        "spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}
    }
    dep = _call(_apis().apps.patch_namespaced_deployment, name, namespace, patch)
    return {"restarted": True, "restarted_at": stamp, "deployment": _deployment_view(dep)}


def scale_deployment(args: ScaleArgs, ctx: ToolContext) -> dict[str, Any]:
    check_mutation_allowed(args)
    _require_confirmation(ctx)
    namespace, name = approved_namespace(args.namespace), _approved_deployment(args.deployment)
    dep = _call(
        _apis().apps.patch_namespaced_deployment_scale,
        name,
        namespace,
        {"spec": {"replicas": args.replicas}},
    )
    return {"scaled": True, "deployment": name, "desired_replicas": dep.spec.replicas}


def _target(args: DeploymentArgs) -> str:
    name = args.deployment or sorted(get_settings().approved_deployments)[0]
    return f"deployment '{name}' in namespace '{args.namespace or get_settings().k8s_namespace}'"


TOOLS = [
    Tool(
        name="k8s_list_workloads",
        description="List Deployments and Pods of the ML application with readiness and restarts.",
        args_model=NamespaceArgs,
        func=list_workloads,
        category="kubernetes",
    ),
    Tool(
        name="k8s_pod_status",
        description=(
            "Detailed status of one Pod: conditions, container states, restart reasons, probe "
            "configuration, resources and its events."
        ),
        args_model=PodArgs,
        func=pod_status,
        category="kubernetes",
    ),
    Tool(
        name="k8s_pod_logs",
        description="Tail the logs of a Pod (optionally of its previous, crashed container).",
        args_model=PodLogArgs,
        func=pod_logs,
        category="kubernetes",
    ),
    Tool(
        name="k8s_services",
        description=(
            "Inspect Services: type, ports, selector and which Pod endpoints are ready, i.e. "
            "whether the Service can actually serve traffic."
        ),
        args_model=NamespaceArgs,
        func=services,
        category="kubernetes",
    ),
    Tool(
        name="k8s_unhealthy_pods",
        description="Find Pods that are not ready, container restarts and recent warning events.",
        args_model=NamespaceArgs,
        func=unhealthy_pods,
        category="kubernetes",
    ),
    Tool(
        name="k8s_recent_events",
        description="Recent Kubernetes events in the application namespace.",
        args_model=EventArgs,
        func=recent_events,
        category="kubernetes",
    ),
    Tool(
        name="k8s_deployed_version",
        description=(
            "Report the deployed application version: image tag, version label, ConfigMap, "
            "rollout state and what the running app reports on /version (model run id, dataset)."
        ),
        args_model=DeploymentArgs,
        func=deployed_version,
        category="kubernetes",
    ),
    Tool(
        name="k8s_restart_deployment",
        description=(
            "Rollout-restart the application Deployment. Changes cluster state: only executed "
            "after the user explicitly confirms."
        ),
        args_model=DeploymentArgs,
        func=restart_deployment,
        category="kubernetes",
        mutating=True,
        precheck=check_mutation_allowed,
        summarize=lambda a: f"Rollout-restart {_target(a)}",
    ),
    Tool(
        name="k8s_scale_deployment",
        description=(
            "Change the replica count of the application Deployment. Changes cluster state: only "
            "executed after the user explicitly confirms."
        ),
        args_model=ScaleArgs,
        func=scale_deployment,
        category="kubernetes",
        mutating=True,
        precheck=check_mutation_allowed,
        summarize=lambda a: f"Scale {_target(a)} to {a.replicas} replicas",
    ),
]
