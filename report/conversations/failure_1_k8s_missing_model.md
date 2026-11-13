**User:** The prediction API pods are not ready. Check the pod logs and the deployed configuration (ConfigMap) and tell me the root cause.

**Agent** (request id `be1f8fefadee4662aaab43eda290d398`):

**Known facts**

- **Pod readiness** – Both `prediction-api-59d79779d-8gtq9` and `prediction-api-59d79779d-srx7v` are in *Running* phase but `ready: false` (0/1 containers ready) and the Deployment reports `MinimumReplicasUnavailable` (k8s_list_workloads).  
- **Pod logs** – The logs repeatedly contain entries like:  

  ```
  {"event":"readiness_failed","level":"ERROR","reason":"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'"}
  ```  

  (k8s_pod_logs). The readiness probe therefore fails on every check.  
- **Deployed configuration** – The Deployment’s ConfigMap sets  

  ```json
  {"MODEL_PATH":"/app/model/model-v2-missing.pkl"}
  ```  

  (k8s_deployed_version). The same path that the container tries to load is missing, causing the `FileNotFoundError`.  

**Uncertainties**

- Whether the expected model artifact (`model‑v2‑missing.pkl`) exists in the container image or a mounted volume. The system reports it as missing, but we have not inspected the image or volume contents.  
- If a newer model version is available in the model registry that should be used instead.

**Recommended actions**

1. **Fix the model path** – Update the ConfigMap (or the Deployment’s environment) to point to a valid model file that actually exists in the container image or mounted volume (e.g., `/app/model/model-v2.pkl`).  
2. **Deploy the correct model artifact** – Ensure the required model file is baked into the Docker image or provided via a volume/secret before the pod starts.  
3. **Restart the Deployment** – After correcting the ConfigMap (or image), trigger a rollout restart (`k8s_restart_deployment`) so that new pods pick up the corrected configuration.  
4. **Verify** – Once pods restart, check `kubectl get pods` (via `k8s_list_workloads`) and the readiness endpoint (`/health/ready`) to confirm the pods become ready.  

The root cause is a mismatched ConfigMap entry that points to a non‑existent model file, causing the readiness probe to fail and the pods to remain unready.

**Tool calls (actual evidence):**

```text
  - k8s_list_workloads({"namespace": ""}) -> ok [2154 ms]
  - k8s_pod_logs({"namespace": "", "pod_name": "prediction-api-59d79779d-8gtq9", "previous": false, "tail_lines": 50}) -> ok [65 ms]
  - k8s_deployed_version({"deployment": "prediction-api", "namespace": ""}) -> ok [72 ms]
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
  "duration_ms": 2153.7,
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
  "tool": "k8s_pod_logs",
  "arguments": {
   "namespace": "",
   "pod_name": "prediction-api-59d79779d-8gtq9",
   "previous": false,
   "tail_lines": 50
  },
  "status": "ok",
  "duration_ms": 64.7,
  "result": {
   "pod": "prediction-api-59d79779d-8gtq9",
   "namespace": "mlops",
   "previous_container": false,
   "line_count": 18,
   "lines": [
    "INFO:     10.244.0.1:42602 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "{\"ts\": \"2026-11-13T18:10:48+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:42612 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "INFO:     10.244.0.1:51674 - \"GET /health/live HTTP/1.1\" 200 OK",
    "{\"ts\": \"2026-11-13T18:10:53+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:51688 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "{\"ts\": \"2026-11-13T18:10:58+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:51690 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "INFO:     10.244.0.1:60732 - \"GET /health/live HTTP/1.1\" 200 OK",
    "{\"ts\": \"2026-11-13T18:11:03+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:60736 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "{\"ts\": \"2026-11-13T18:11:08+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:60748 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "INFO:     10.244.0.1:43634 - \"GET /health/live HTTP/1.1\" 200 OK",
    "{\"ts\": \"2026-11-13T18:11:13+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:43644 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable",
    "{\"ts\": \"2026-11-13T18:11:18+0000\", \"event\": \"readiness_failed\", \"level\": \"ERROR\", \"reason\": \"FileNotFoundError: [Errno 2] No such file or directory: '/app/model/model-v2-missing.pkl'\"}",
    "INFO:     10.244.0.1:43652 - \"GET /health/ready HTTP/1.1\" 503 Service Unavailable"
   ]
  },
  "error": null
 },
 {
  "tool": "k8s_deployed_version",
  "arguments": {
   "deployment": "prediction-api",
   "namespace": ""
  },
  "status": "ok",
  "duration_ms": 71.9,
  "result": {
   "namespace": "mlops",
   "deployment": {
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
   },
   "image": "prediction-api:1.0.0",
   "image_tag": "1.0.0",
   "rollout_complete": false,
   "configmap": {
    "APP_VERSION": "1.0.0",
    "LOG_LEVEL": "INFO",
    "MODEL_PATH": "/app/model/model-v2-missing.pkl"
   },
   "live_version_endpoint": null,
   "live_version_error": "application did not answer /version: Kubernetes API error HTTP 503: Service Unavailable"
  },
  "error": null
 }
]
```

</details>
