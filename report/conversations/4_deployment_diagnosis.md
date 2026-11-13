**User:** Why is my prediction API unavailable?

**Agent** (request id `e84b199e87aa48a99b3306c621346057`):

**Known facts**

- **Kubernetes deployment** (from `k8s_list_workloads`):  
  - Deployment **prediction-api** desired = 2, ready = 0, unavailable = 2.  
  - Condition *Available* is **False** – “MinimumReplicasUnavailable”.

- **Pod health** (from `k8s_unhealthy_pods`):  
  - Both pods `prediction-api-59d79779d-8gtq9` and `prediction-api-59d79779d-srx7v` are **Running** but **not Ready**.  
  - Readiness‑probe warnings: “Readiness probe failed: connection refused” and “HTTP probe failed with statuscode: 503”.

- **Pod logs** (from `k8s_pod_logs` for pod `prediction-api-59d79779d-8gtq9`):  
  - Startup succeeds, but the application logs an error:  

    ```
    model_load_failed … FileNotFoundError: No such file or directory: '/app/model/model-v2-missing.pkl'
    readiness_failed … same FileNotFoundError
    ```

  - Consequently every `/health/ready` request returns **503 Service Unavailable** while `/health/live` returns **200 OK**.

- **Service routing** (from `k8s_services`):  
  - Service **prediction-api** (ClusterIP, port 8000) has **no ready endpoints**; both pods are listed under `not_ready_endpoints`.  
  - `can_serve_traffic` is **false**.

**Uncertainties**

- The exact cause of the missing model file (e.g., build artifact not copied, wrong model version tag, or storage mount issue) is not known from the current data.  
- No information about the model registry entry or the expected model artifact version is available; we would need to query MLflow to confirm which model file the deployment should load.

**Recommended actions**

1. **Identify the expected model artifact**  
   - Query the MLflow model registry (`mlflow_model_registry`) for the model version currently referenced by the production deployment (e.g., via the `/version` endpoint or deployment config).  
   - Verify that the artifact `model-v2-missing.pkl` exists in the model registry or artifact store.

2. **Fix the missing artifact**  
   - If the artifact was not built/pushed, rebuild the model and register it with the correct name (`model-v2-missing.pkl`).  
   - If the artifact exists but the container image does not include it, update the Dockerfile / CI pipeline to copy the model into `/app/model/` and rebuild the image (e.g., `prediction-api:1.0.0` → new tag).

3. **Redeploy the corrected image**  
   - After the image contains the required model file, trigger a rollout restart (`k8s_restart_deployment` – *not executed yet*).  
   - Verify that pods become Ready and the service shows ready endpoints.

4. **Validate**  
   - Run `k8s_pod_logs` and `k8s_unhealthy_pods` again after redeployment to confirm the readiness probe succeeds.  
   - Test the API endpoint (`curl http://<cluster_ip>:8000/health/ready`) to ensure it returns **200 OK**.

Until the model file issue is resolved, the prediction API will remain unavailable because the application cannot load its model and therefore fails its readiness probe.

**Tool calls (actual evidence):**

```text
  - k8s_list_workloads({"namespace": ""}) -> ok [992 ms]
  - k8s_unhealthy_pods({"namespace": ""}) -> ok [44 ms]
  - k8s_pod_logs({"namespace": "", "pod_name": "prediction-api-59d79779d-8gtq9", "tail_lines": 50}) -> ok [45 ms]
  - k8s_services({"namespace": ""}) -> ok [39 ms]
```

<details><summary>Raw tool results</summary>

```json
[
 {
  "tool": "k8s_list_workloads",
  "arguments": {
   "namespace": ""
  },
  "status": "ok",
  "duration_ms": 992.4,
  "result": {
   "namespace": "mlops",
   "deployments": [
    {
     "name": "prediction-api",
     "desired_replicas": 2,
     "ready_replicas": 0,
     "updated_replicas": 2,
     "available_replicas": 0,
     "unavailable_replicas": 2,
     "images": [
      "prediction-api:1.0.0"
     ],
     "version_label": "1.0.0",
     "revision": "1",
     "conditions": [
      {
       "type": "Progressing",
       "status": "True",
       "reason": "NewReplicaSetAvailable",
       "message": "ReplicaSet \"prediction-api-59d79779d\" has successfully progressed."
      },
      {
       "type": "Available",
       "status": "False",
       "reason": "MinimumReplicasUnavailable",
       "message": "Deployment does not have minimum availability."
      }
     ]
    }
   ],
   "pods": [
    {
     "name": "prediction-api-59d79779d-8gtq9",
     "phase": "Running",
     "ready": false,
     "containers_ready": "0/1",
     "restarts": 0,
     "waiting_reasons": [],
     "terminating": false,
     "node": "mlops-control-plane",
     "pod_ip": "10.244.0.8",
     "started_at": "2026-11-13T18:09:31+00:00",
     "images": [
      "prediction-api:1.0.0"
     ]
    },
    {
     "name": "prediction-api-59d79779d-srx7v",
     "phase": "Running",
     "ready": false,
     "containers_ready": "0/1",
     "restarts": 0,
     "waiting_reasons": [],
     "terminating": false,
     "node": "mlops-control-plane",
     "pod_ip": "10.244.0.7",
     "started_at": "2026-11-13T18:09:31+00:00",
     "images": [
      "prediction-api:1.0.0"
     ]
    }
   ]
  },
  "error": null
 },
 {
  "tool": "k8s_unhealthy_pods",
  "arguments": {
   "namespace": ""
  },
  "status": "ok",
  "duration_ms": 44.3,
  "result": {
   "namespace": "mlops",
   "total_pods": 2,
   "healthy_pods": 0,
   "unhealthy_pods": [
    {
     "name": "prediction-api-59d79779d-8gtq9",
     "phase": "Running",
     "ready": false,
     "containers_ready": "0/1",
     "restarts": 0,
     "waiting_reasons": [],
     "terminating": false,
     "node": "mlops-control-plane",
     "pod_ip": "10.244.0.8",
     "started_at": "2026-11-13T18:09:31+00:00",
     "images": [
      "prediction-api:1.0.0"
     ],
     "container_states": {
      "prediction-api": {
       "state": "running",
       "started_at": "2026-11-13T18:09:32+00:00"
      }
     },
     "not_ready_conditions": [
      {
       "type": "Ready",
       "reason": "ContainersNotReady",
       "message": "containers with unready status: [prediction-api]"
      },
      {
       "type": "ContainersReady",
       "reason": "ContainersNotReady",
       "message": "containers with unready status: [prediction-api]"
      }
     ]
    },
    {
     "name": "prediction-api-59d79779d-srx7v",
     "phase": "Running",
     "ready": false,
     "containers_ready": "0/1",
     "restarts": 0,
     "waiting_reasons": [],
     "terminating": false,
     "node": "mlops-control-plane",
     "pod_ip": "10.244.0.7",
     "started_at": "2026-11-13T18:09:31+00:00",
     "images": [
      "prediction-api:1.0.0"
     ],
     "container_states": {
      "prediction-api": {
       "state": "running",
       "started_at": "2026-11-13T18:09:31+00:00"
      }
     },
     "not_ready_conditions": [
      {
       "type": "Ready",
       "reason": "ContainersNotReady",
       "message": "containers with unready status: [prediction-api]"
      },
      {
       "type": "ContainersReady",
       "reason": "ContainersNotReady",
       "message": "containers with unready status: [prediction-api]"
      }
     ]
    }
   ],
   "container_restarts": [],
   "warning_events": [
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-59d79779d-75b7m",
     "message": "Readiness probe failed: Get \"http://10.244.0.5:8000/health/ready\": dial tcp 10.244.0.5:8000: connect: connection refused",
     "count": 2,
     "last_seen": "2026-11-13T18:09:31+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-59d79779d-dqtp8",
     "message": "Readiness probe failed: Get \"http://10.244.0.6:8000/health/ready\": dial tcp 10.244.0.6:8000: connect: connection refused",
     "count": 2,
     "last_seen": "2026-11-13T18:09:31+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-59d79779d-srx7v",
     "message": "Readiness probe failed: Get \"http://10.244.0.7:8000/health/ready\": dial tcp 10.244.0.7:8000: connect: connection refused",
     "count": 1,
     "last_seen": "2026-11-13T18:09:36+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-59d79779d-srx7v",
     "message": "Readiness probe failed: HTTP probe failed with statuscode: 503",
     "count": 4,
     "last_seen": "2026-11-13T18:09:56+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-59d79779d-8gtq9",
     "message": "Readiness probe failed: HTTP probe failed with statuscode: 503",
     "count": 5,
     "last_seen": "2026-11-13T18:09:58+00:00"
    }
   ]
  },
  "error": null
 },
 {
  "tool": "k8s_pod_logs",
  "arguments": {
   "namespace": "",
   "pod_name": "prediction-api-59d79779d-8gtq9",
   "tail_lines": 50
  },
  "status": "ok",
  "duration_ms": 45.4,
  "result": {
   "pod": "prediction-api-59d79779d-8gtq9",
   "namespace": "mlops",
   "previous_container": false,
   "line_count": 19,
   "lines": [
    "INFO:     Waiting for application startup.",
    "{\"ts\": \"2026-11-13T18:09:38+0000\", \"event\": \"startup\", \"app_version\": \"1.0.0\"}",
    "{\"ts\": \"2026-11-13T18:09:38+0000\", \"event\": \"model_load_failed\", \"level\": \"ERROR\", \"model_path\": \"/app/model/model-v2-missing.pkl\", \"error\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     Application startup complete.",
    "INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)",
    "{\"ts\": \"2026-11-13T18:09:38+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:60310 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "{\"ts\": \"2026-11-13T18:09:43+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:42586 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "{\"ts\": \"2026-11-13T18:09:48+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:42588 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "INFO:     10.244.0.1:36474 - \"GET /health/live HTTP/1.1\" 200 OK",
    "{\"ts\": \"2026-11-13T18:09:53+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:36490 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "{\"ts\": \"2026-11-13T18:09:58+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:36500 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "INFO:     10.244.0.1:53750 - \"GET /health/live HTTP/1.1\" 200 OK",
    "{\"ts\": \"2026-11-13T18:10:03+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:53764 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable"
   ]
  },
  "error": null
 },
 {
  "tool": "k8s_services",
  "arguments": {
   "namespace": ""
  },
  "status": "ok",
  "duration_ms": 39.3,
  "result": {
   "namespace": "mlops",
   "services": [
    {
     "name": "prediction-api",
     "type": "ClusterIP",
     "cluster_ip": "10.96.146.159",
     "ports": [
      {
       "name": "http",
       "port": 8000,
       "target_port": "http"
      }
     ],
     "selector": {
      "app.kubernetes.io/name": "prediction-api"
     },
     "ready_endpoints": [],
     "not_ready_endpoints": [
      {
       "addresses": [
        "10.244.0.7"
       ],
       "pod": "prediction-api-59d79779d-srx7v"
      },
      {
       "addresses": [
        "10.244.0.8"
       ],
       "pod": "prediction-api-59d79779d-8gtq9"
      }
     ],
     "can_serve_traffic": false
    }
   ]
  },
  "error": null
 }
]
```

</details>
