**User:** My prediction API pods keep restarting. Which pods restarted, how many times, why were the containers restarted, and what is the root cause?

**Agent** (request id `30af56096d1449fd99e089d4ac87ce67`):

**Known facts**

- **Pods that have restarted** (from `k8s_list_workloads`):
  - `prediction-api-5cccb6b6f8-dr59d` – restarts = 5  
  - `prediction-api-5cccb6b6f8-pljh6` – restarts = 4  

- **Container state & reason** (from `k8s_unhealthy_pods` and `k8s_pod_status`):
  - Both pods show `state: waiting` with `reason: CrashLoopBackOff`.  
  - The container `prediction-api` in each pod has `restart_count` matching the values above.  

- **Pod logs** (from `k8s_pod_logs`):
  - Each pod starts, loads the model (`mlflow_run_id: f1396c4a86da44fb9988b39207fd7270`), serves health‑ready requests, then logs “Shutting down … Finished server process [1]”.  
  - The process exits cleanly (`exit_code: 0` shown in `k8s_pod_status`), after which the pod is immediately restarted by the Deployment controller.  

- **Probe configuration** (from `k8s_pod_status` container spec):
  - **Readiness probe**: GET `/health/ready` → returns 200 OK (pods become ready).  
  - **Liveness probe**: GET `/health/missing` → returns 404 Not Found.  

**Uncertainties**

- The logs do not show an explicit crash or exception; the container exits with code 0, so the termination is intentional (application shutdown) rather than a runtime error.  
- No other Kubernetes events (e.g., OOMKilled, node issues) were reported in the data retrieved.

**Root cause**

The **liveness probe is mis‑configured** to query `/health/missing`, which always returns 404. The probe’s failure threshold is reached, causing the kubelet to kill the container. The container then exits cleanly, the Deployment restarts it, and after several attempts the pod enters `CrashLoopBackOff`. This explains the repeated restarts and the “CrashLoopBackOff” reason.

**Recommended actions**

- Update the Deployment (or the pod spec) so the liveness probe points to a healthy endpoint, e.g.:

  ```yaml
  livenessProbe:
    httpGet:
      path: /health/ready   # or another endpoint that returns 200
      port: 8000
    initialDelaySeconds: 10
    periodSeconds: 10
    failureThreshold: 3
  ```

- Apply the corrected manifest (or use `k8s_restart_deployment` after confirming the change). This will stop the CrashLoopBackOff and allow the pods to stay running.

**Tool calls (actual evidence):**

```text
  - k8s_list_workloads({"namespace": ""}) -> ok [1203 ms]
  - k8s_unhealthy_pods({"namespace": ""}) -> ok [74 ms]
  - k8s_pod_logs({"namespace": "", "pod_name": "prediction-api-5cccb6b6f8-dr59d", "previous": false, "tail_lines": 50}) -> ok [62 ms]
  - k8s_pod_logs({"namespace": "", "pod_name": "prediction-api-5cccb6b6f8-pljh6", "previous": false, "tail_lines": 50}) -> ok [52 ms]
  - k8s_pod_status({"namespace": "", "pod_name": "prediction-api-5cccb6b6f8-dr59d"}) -> ok [80 ms]
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
  "duration_ms": 1203.3,
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
     "revision": "3",
     "conditions": [
      {
       "type": "Progressing",
       "status": "True",
       "reason": "NewReplicaSetAvailable",
       "message": "ReplicaSet \"prediction-api-5cccb6b6f8\" has successfully progressed."
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
     "name": "prediction-api-5cccb6b6f8-dr59d",
     "phase": "Running",
     "ready": false,
     "containers_ready": "0/1",
     "restarts": 5,
     "waiting_reasons": [
      "CrashLoopBackOff"
     ],
     "terminating": false,
     "node": "mlops-control-plane",
     "pod_ip": "10.244.0.8",
     "started_at": "2026-11-13T19:58:46+00:00",
     "images": [
      "prediction-api:1.0.0"
     ]
    },
    {
     "name": "prediction-api-5cccb6b6f8-pljh6",
     "phase": "Running",
     "ready": false,
     "containers_ready": "0/1",
     "restarts": 4,
     "waiting_reasons": [
      "CrashLoopBackOff"
     ],
     "terminating": false,
     "node": "mlops-control-plane",
     "pod_ip": "10.244.0.9",
     "started_at": "2026-11-13T19:59:03+00:00",
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
  "duration_ms": 74.5,
  "result": {
   "namespace": "mlops",
   "total_pods": 2,
   "healthy_pods": 0,
   "unhealthy_pods": [
    {
     "name": "prediction-api-5cccb6b6f8-dr59d",
     "phase": "Running",
     "ready": false,
     "containers_ready": "0/1",
     "restarts": 5,
     "waiting_reasons": [
      "CrashLoopBackOff"
     ],
     "terminating": false,
     "node": "mlops-control-plane",
     "pod_ip": "10.244.0.8",
     "started_at": "2026-11-13T19:58:46+00:00",
     "images": [
      "prediction-api:1.0.0"
     ],
     "container_states": {
      "prediction-api": {
       "state": "waiting",
       "reason": "CrashLoopBackOff",
       "message": "back-off 1m20s restarting failed container=prediction-api pod=prediction-api-5cccb6b6f8-dr59d_mlops(4b36de3e-06aa-4282-ac07-71898235e1a3)"
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
     "name": "prediction-api-5cccb6b6f8-pljh6",
     "phase": "Running",
     "ready": false,
     "containers_ready": "0/1",
     "restarts": 4,
     "waiting_reasons": [
      "CrashLoopBackOff"
     ],
     "terminating": false,
     "node": "mlops-control-plane",
     "pod_ip": "10.244.0.9",
     "started_at": "2026-11-13T19:59:03+00:00",
     "images": [
      "prediction-api:1.0.0"
     ],
     "container_states": {
      "prediction-api": {
       "state": "waiting",
       "reason": "CrashLoopBackOff",
       "message": "back-off 40s restarting failed container=prediction-api pod=prediction-api-5cccb6b6f8-pljh6_mlops(da51cc31-df39-4240-906e-772d426d6e5b)"
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
   "container_restarts": [
    {
     "pod": "prediction-api-5cccb6b6f8-dr59d",
     "container": "prediction-api",
     "restart_count": 5,
     "last_terminated_state": {
      "state": "terminated",
      "reason": "Completed",
      "exit_code": 0,
      "finished_at": "2026-11-13T20:02:48+00:00"
     }
    },
    {
     "pod": "prediction-api-5cccb6b6f8-pljh6",
     "container": "prediction-api",
     "restart_count": 4,
     "last_terminated_state": {
      "state": "terminated",
      "reason": "Completed",
      "exit_code": 0,
      "finished_at": "2026-11-13T20:02:25+00:00"
     }
    }
   ],
   "warning_events": [
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-584c858b9b-qzd5w",
     "message": "Readiness probe failed: Get \"http://10.244.0.15:8000/health/ready\": context deadline exceeded (Client.Timeout exceeded while awaiting headers)",
     "count": 1,
     "last_seen": "2026-11-13T19:38:40+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-584c858b9b-cf7jl",
     "message": "Readiness probe failed: Get \"http://10.244.0.3:8000/health/ready\": dial tcp 10.244.0.3:8000: connect: connection refused",
     "count": 1,
     "last_seen": "2026-11-13T19:54:18+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-584c858b9b-qzd5w",
     "message": "Readiness probe failed: Get \"http://10.244.0.2:8000/health/ready\": dial tcp 10.244.0.2:8000: connect: connection refused",
     "count": 3,
     "last_seen": "2026-11-13T19:54:20+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-584c858b9b-pndr4",
     "message": "Readiness probe failed: Get \"http://10.244.0.7:8000/health/ready\": dial tcp 10.244.0.7:8000: connect: connection refused",
     "count": 2,
     "last_seen": "2026-11-13T19:56:31+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-5cccb6b6f8-pljh6",
     "message": "Readiness probe failed: Get \"http://10.244.0.9:8000/health/ready\": dial tcp 10.244.0.9:8000: connect: connection refused",
     "count": 14,
     "last_seen": "2026-11-13T20:01:15+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-5cccb6b6f8-pljh6",
     "message": "Liveness probe failed: HTTP probe failed with statuscode: 404",
     "count": 11,
     "last_seen": "2026-11-13T20:01:33+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-5cccb6b6f8-dr59d",
     "message": "Readiness probe failed: Get \"http://10.244.0.8:8000/health/ready\": dial tcp 10.244.0.8:8000: connect: connection refused",
     "count": 10,
     "last_seen": "2026-11-13T20:01:39+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-5cccb6b6f8-dr59d",
     "message": "Liveness probe failed: HTTP probe failed with statuscode: 404",
     "count": 15,
     "last_seen": "2026-11-13T20:02:06+00:00"
    }
   ]
  },
  "error": null
 },
 {
  "tool": "k8s_pod_logs",
  "arguments": {
   "namespace": "",
   "pod_name": "prediction-api-5cccb6b6f8-dr59d",
   "previous": false,
   "tail_lines": 50
  },
  "status": "ok",
  "duration_ms": 62.3,
  "result": {
   "pod": "prediction-api-5cccb6b6f8-dr59d",
   "namespace": "mlops",
   "previous_container": false,
   "line_count": 18,
   "lines": [
    "INFO:     Started server process [1]",
    "INFO:     Waiting for application startup.",
    "{\"ts\": \"2026-11-13T20:02:15+0000\", \"event\": \"startup\", \"app_version\": \"1.0.0\"}",
    "{\"ts\": \"2026-11-13T20:02:21+0000\", \"event\": \"model_loaded\", \"model_path\": \"/app/model/model.pkl\", \"model\": {\"model_name\": \"breast-cancer-classifier\", \"model_version\": \"4\", \"mlflow_run_id\": \"f1396c4a86da44fb9988b39207fd7270\", \"f1\": 0.993006993006993, \"dataset_version\": \"data-v2.0\", \"dataset_md5\": \"12",
    "INFO:     Application startup complete.",
    "INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)",
    "INFO:     10.244.0.1:44444 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:44450 - \"GET /health/missing HTTP/1.1\" 404 Not Found",
    "INFO:     10.244.0.1:44460 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:59430 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:59436 - \"GET /health/missing HTTP/1.1\" 404 Not Found",
    "INFO:     10.244.0.1:59442 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:45336 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:45346 - \"GET /health/missing HTTP/1.1\" 404 Not Found",
    "INFO:     Shutting down",
    "INFO:     Waiting for application shutdown.",
    "INFO:     Application shutdown complete.",
    "INFO:     Finished server process [1]"
   ]
  },
  "error": null
 },
 {
  "tool": "k8s_pod_logs",
  "arguments": {
   "namespace": "",
   "pod_name": "prediction-api-5cccb6b6f8-pljh6",
   "previous": false,
   "tail_lines": 50
  },
  "status": "ok",
  "duration_ms": 51.5,
  "result": {
   "pod": "prediction-api-5cccb6b6f8-pljh6",
   "namespace": "mlops",
   "previous_container": false,
   "line_count": 19,
   "lines": [
    "INFO:     Started server process [1]",
    "INFO:     Waiting for application startup.",
    "{\"ts\": \"2026-11-13T20:01:51+0000\", \"event\": \"startup\", \"app_version\": \"1.0.0\"}",
    "{\"ts\": \"2026-11-13T20:01:55+0000\", \"event\": \"model_loaded\", \"model_path\": \"/app/model/model.pkl\", \"model\": {\"model_name\": \"breast-cancer-classifier\", \"model_version\": \"4\", \"mlflow_run_id\": \"f1396c4a86da44fb9988b39207fd7270\", \"f1\": 0.993006993006993, \"dataset_version\": \"data-v2.0\", \"dataset_md5\": \"12",
    "INFO:     Application startup complete.",
    "INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)",
    "INFO:     10.244.0.1:42268 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:47080 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:47084 - \"GET /health/missing HTTP/1.1\" 404 Not Found",
    "INFO:     10.244.0.1:47090 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:49322 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:49328 - \"GET /health/missing HTTP/1.1\" 404 Not Found",
    "INFO:     10.244.0.1:49338 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:49632 - \"GET /health/ready HTTP/1.1\" 200 OK",
    "INFO:     10.244.0.1:49636 - \"GET /health/missing HTTP/1.1\" 404 Not Found",
    "INFO:     Shutting down",
    "INFO:     Waiting for application shutdown.",
    "INFO:     Application shutdown complete.",
    "INFO:     Finished server process [1]"
   ]
  },
  "error": null
 },
 {
  "tool": "k8s_pod_status",
  "arguments": {
   "namespace": "",
   "pod_name": "prediction-api-5cccb6b6f8-dr59d"
  },
  "status": "ok",
  "duration_ms": 79.5,
  "result": {
   "name": "prediction-api-5cccb6b6f8-dr59d",
   "phase": "Running",
   "ready": false,
   "containers_ready": "0/1",
   "restarts": 5,
   "waiting_reasons": [
    "CrashLoopBackOff"
   ],
   "terminating": false,
   "node": "mlops-control-plane",
   "pod_ip": "10.244.0.8",
   "started_at": "2026-11-13T19:58:46+00:00",
   "images": [
    "prediction-api:1.0.0"
   ],
   "namespace": "mlops",
   "conditions": [
    {
     "type": "PodReadyToStartContainers",
     "status": "True",
     "reason": null,
     "message": null
    },
    {
     "type": "Initialized",
     "status": "True",
     "reason": null,
     "message": null
    },
    {
     "type": "Ready",
     "status": "False",
     "reason": "ContainersNotReady",
     "message": "containers with unready status: [prediction-api]"
    },
    {
     "type": "ContainersReady",
     "status": "False",
     "reason": "ContainersNotReady",
     "message": "containers with unready status: [prediction-api]"
    },
    {
     "type": "PodScheduled",
     "status": "True",
     "reason": null,
     "message": null
    }
   ],
   "containers": [
    {
     "name": "prediction-api",
     "image": "prediction-api:1.0.0",
     "ready": false,
     "restart_count": 5,
     "state": {
      "state": "waiting",
      "reason": "CrashLoopBackOff",
      "message": "back-off 1m20s restarting failed container=prediction-api pod=prediction-api-5cccb6b6f8-dr59d_mlops(4b36de3e-06aa-4282-ac07-71898235e1a3)"
     },
     "last_terminated_state": {
      "state": "terminated",
      "reason": "Completed",
      "exit_code": 0,
      "finished_at": "2026-11-13T20:02:48+00:00"
     },
     "readiness_probe": {
      "http_get": "/health/ready on port http = 8000",
      "period_seconds": 5,
      "failure_threshold": 3,
      "initial_delay_seconds": 3
     },
     "liveness_probe": {
      "http_get": "/health/missing on port http = 8000",
      "period_seconds": 10,
      "failure_threshold": 3,
      "initial_delay_seconds": 10
     },
     "resources": {
      "requests": {
       "cpu": "100m",
       "memory": "128Mi"
      },
      "limits": {
       "cpu": "500m",
       "memory": "256Mi"
      }
     }
    }
   ],
   "events": [
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-5cccb6b6f8-dr59d",
     "message": "Readiness probe failed: Get \"http://10.244.0.8:8000/health/ready\": dial tcp 10.244.0.8:8000: connect: connection refused",
     "count": 10,
     "last_seen": "2026-11-13T20:01:39+00:00"
    },
    {
     "type": "Warning",
     "reason": "Unhealthy",
     "object": "Pod/prediction-api-5cccb6b6f8-dr59d",
     "message": "Liveness probe failed: HTTP probe failed with statuscode: 404",
     "count": 15,
     "last_seen": "2026-11-13T20:02:06+00:00"
    },
    {
     "type": "Normal",
     "reason": "Pulled",
     "object": "Pod/prediction-api-5cccb6b6f8-dr59d",
     "message": "Container image \"prediction-api:1.0.0\" already present on machine and can be accessed by the pod",
     "count": 6,
     "last_seen": "2026-11-13T20:02:08+00:00"
    },
    {
     "type": "Normal",
     "reason": "Created",
     "object": "Pod/prediction-api-5cccb6b6f8-dr59d",
     "message": "Container created",
     "count": 6,
     "last_seen": "2026-11-13T20:02:08+00:00"
    },
    {
     "type": "Normal",
     "reason": "Started",
     "object": "Pod/prediction-api-5cccb6b6f8-dr59d",
     "message": "Container started",
     "count": 6,
     "last_seen": "2026-11-13T20:02:08+00:00"
    },
    {
     "type": "Normal",
     "reason": "Killing",
     "object": "Pod/prediction-api-5cccb6b6f8-dr59d",
     "message": "Container prediction-api failed liveness probe, will be restarted",
     "count": 6,
     "last_seen": "2026-11-13T20:02:46+00:00"
    }
   ]
  },
  "error": null
 }
]
```

</details>
