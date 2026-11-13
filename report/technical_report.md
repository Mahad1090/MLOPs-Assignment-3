# Technical Report — MLOps AI Agent (Assignment 03)

All values in this report were produced by the running system on 13 November 2026. Raw
exports are in [`evidence/`](evidence/) and full conversation transcripts with raw tool
results are in [`conversations/`](conversations/).

## 1. System architecture

The complete architecture, request flow, provenance chain and safety model are in
[architecture.md](../architecture.md). In short:

```
user ─► FastAPI /chat (or CLI) ─► Orchestrator ◄─► LLM (Groq, openai/gpt-oss-120b, tool calling)
                                       │
                 tool registry: validate ─► permission check ─► timeout ─► structured log
                                       │
        ┌──────────────┬───────────────┼────────────────┬──────────────────┐
     DVC tools     MLflow tools    Airflow tools    Kubernetes tools   retraining policy
   (git/dvc CLI,  (MlflowClient)  (REST v2 + JWT)  (python client,    (YAML rules over
    allowlisted)                                    1 namespace)       MLflow + DVC)
```

| Layer | What runs | Version |
|---|---|---|
| Data | DVC, local remote, dataset tags `data-v1.0` (400 rows) and `data-v2.0` (569 rows) | DVC 3.67.1 |
| Tracking | MLflow tracking server + model registry (SQLite backend) | MLflow 3.18.0 |
| Orchestration | Airflow standalone, DAG `training_pipeline` (6 tasks) | Airflow 3.4.0 |
| Serving | kind cluster, namespace `mlops`, Deployment `prediction-api` (2 replicas), ClusterIP Service, ConfigMap | Kubernetes 1.37 |
| Agent | FastAPI + direct tool calling, 30 tools | Python 3.12 |

## 2. Tool definitions and permissions

| Tool | Module | Inputs | Changes state |
|---|---|---|---|
| `dvc_list_tracked_datasets` | dvc | none | no |
| `dvc_current_version` | dvc | `path` | no |
| `dvc_compare_versions` | dvc | `rev_a`, `rev_b`, `path` | no |
| `dvc_status` | dvc | none | no |
| `dvc_find_version` | dvc | `md5`, `rev`, `path` | no |
| `dvc_reproduce_instructions` | dvc | `rev`, `path`, `verify` | no |
| `mlflow_list_experiments` | mlflow | none | no |
| `mlflow_list_runs` | mlflow | `experiment`, `max_results` | no |
| `mlflow_get_run` | mlflow | `run` | no |
| `mlflow_best_run` | mlflow | `metric`, `experiment`, `last_n`, `higher_is_better` | no |
| `mlflow_compare_runs` | mlflow | `run_ids`, `last_n`, `experiment`, `metric` | no |
| `mlflow_compare_experiments` | mlflow | `experiments`, `metric` | no |
| `mlflow_model_registry` | mlflow | `model_name` | no |
| `airflow_list_dags` | airflow | none | no |
| `airflow_list_dag_runs` | airflow | `dag_id`, `limit` | no |
| `airflow_get_dag_run` | airflow | `dag_id`, `run_id` | no |
| `airflow_failed_tasks` | airflow | `dag_id`, `run_id` | no |
| `airflow_task_logs` | airflow | `dag_id`, `task_id`, `run_id`, `try_number`, `tail_lines` | no |
| `airflow_trigger_dag` | airflow | `dag_id`, `model_type`, `n_estimators`, `max_depth`, `min_f1` | **yes, needs user confirmation** |
| `airflow_wait_for_dag_run` | airflow | `dag_id`, `run_id`, `timeout_seconds` | no |
| `k8s_list_workloads` | kubernetes | `namespace` | no |
| `k8s_pod_status` | kubernetes | `pod_name`, `namespace` | no |
| `k8s_pod_logs` | kubernetes | `pod_name`, `namespace`, `tail_lines`, `previous` | no |
| `k8s_services` | kubernetes | `namespace` | no |
| `k8s_unhealthy_pods` | kubernetes | `namespace` | no |
| `k8s_recent_events` | kubernetes | `namespace`, `warnings_only` | no |
| `k8s_deployed_version` | kubernetes | `deployment`, `namespace` | no |
| `k8s_restart_deployment` | kubernetes | `deployment`, `namespace` | **yes, needs user confirmation** |
| `k8s_scale_deployment` | kubernetes | `deployment`, `namespace`, `replicas` | **yes, needs user confirmation** |
| `evaluate_retraining_need` | policy | `run`, `observed_live_f1` | no |

Every tool input is a Pydantic model that rejects unknown fields. `GET /tools` returns the
full JSON schemas.

**Permission rules**

- **Read tools** run immediately. Kubernetes reads are confined to the namespace `mlops`;
  DVC/git access is limited to an allowlist of read-only sub-commands, run without a shell.
- **The three state-changing tools are never executed from an LLM tool call.** The call is
  parked as a pending action and only a later request carrying its `confirm_action_id`
  executes it. The id is single-use, bound to the session and expires after 10 minutes.
- `airflow_trigger_dag` accepts only DAG ids in `AIRFLOW_APPROVED_DAGS`.
- `k8s_restart_deployment` / `k8s_scale_deployment` accept only the approved Deployment in
  the approved namespace, with at most 4 replicas.
- There is no tool for delete, exec, apply, secrets or DVC/git writes.
- `kubernetes/rbac.yaml` defines a matching least-privilege ServiceAccount and Role.

## 3. Evidence from the four systems

| System | File | What it shows |
|---|---|---|
| DVC | [evidence/dvc.txt](evidence/dvc.txt) | version tags, pointer file, clean `dvc status`, remote in sync, `dvc diff` between versions, and v1.0 fetched again with md5 `ac465752f88767fa7b7a2a45b3338f81` equal to the pointer at `data-v1.0` |
| MLflow | [evidence/mlflow.txt](evidence/mlflow.txt), [../screenshots/mlflow_runs.png](../screenshots/mlflow_runs.png) | 6 runs with metrics, quality gate, dataset version, DVC md5 and Airflow run id; 5 registered model versions |
| Airflow | [evidence/airflow.txt](evidence/airflow.txt) | 4 DAG runs fetched through the authenticated API with every task state, including the failed quality gate |
| Kubernetes | [evidence/kubernetes.txt](evidence/kubernetes.txt) | Deployment 2/2, Pods, ClusterIP Service, EndpointSlices, probes and resource limits, and the live `/version` answer |
| CI | [evidence/ci.txt](evidence/ci.txt) | lint-and-test job passing on pull request #1 |

Recorded runs (from MLflow):

| Run | Model | F1 | Gate | Dataset | Started by |
|---|---|---|---|---|---|
| `eaf859d5` | random_forest | 0.9645 | passed | data-v2.0 | Airflow, triggered by the agent |
| `61c5953c` | random_forest | 0.9645 | **failed** (threshold 0.999) | data-v2.0 | Airflow, injected failure |
| `f1396c4a` | logistic_regression | 0.9930 | passed | data-v2.0 | Airflow — **deployed** (model version 4) |
| `08aca101` | random_forest | 0.9718 | passed | data-v2.0 | Airflow |
| `7c87e1ed` | random_forest | 0.9650 | passed | data-v2.0 | local pipeline |
| `e59819ab` | logistic_regression | 1.0000 | passed | data-v1.0 | local pipeline |

## 4. Five tested agent conversations

Each transcript contains the question, the answer, the tool calls with status and duration,
and the raw JSON every statement is based on.

| # | Scenario | Question | Tools the agent chose | Outcome |
|---|---|---|---|---|
| 1 | Dataset provenance | "Which dataset version was used to train the registered model version 1, and how can I reproduce that exact dataset?" | `mlflow_model_registry`, `dvc_find_version`, `dvc_reproduce_instructions` | `data-v1.0`, md5 `ac465752…`; the tool re-fetched that version and the md5 matched. [Transcript](conversations/1_dataset_provenance.md) |
| 2 | Model selection | "Compare the last five training runs and recommend the best candidate based on F1 score." | `mlflow_best_run` | Ranked table; top F1 is run `e59819ab` (1.0), reported with the caveats that it was trained on the older `data-v1.0` and that one metric on one split does not make it production-ready. [Transcript](conversations/2_model_selection.md) |
| 3 | Pipeline execution | "Trigger the model training pipeline and report whether execution succeeded." | `airflow_list_dags`, `airflow_trigger_dag` (parked), then after confirmation `airflow_trigger_dag`, `airflow_get_dag_run` | First turn: nothing executed, confirmation requested. Second turn: run `manual__2026-11-13T18:07:47` succeeded, all six tasks `success`. [Transcript](conversations/3_pipeline_execution.md) |
| 4 | Deployment diagnosis | "Why is my prediction API unavailable?" | `k8s_list_workloads`, `k8s_unhealthy_pods`, `k8s_pod_logs`, `k8s_services` | 0/2 ready, readiness probe 503, log line `FileNotFoundError … model-v2-missing.pkl`, Service has no ready endpoints. [Transcript](conversations/4_deployment_diagnosis.md) |
| 5 | Multi-tool reasoning | "Find the best recent model, identify the dataset version used to train it, check the deployment status, and explain whether retraining is necessary." | `mlflow_best_run`, `dvc_find_version`, `k8s_deployed_version`, `evaluate_retraining_need`, `airflow_list_dags`, `airflow_list_dag_runs` | Best F1 run is on `data-v1.0`; the deployed model is run `f1396c4a` on the current `data-v2.0`, 2/2 ready; latest DAG run succeeded; policy says retraining **not** recommended; live-performance data is listed as unknown. No training was started. [Transcript](conversations/5_multi_tool_reasoning.md) |

## 5. Failure investigations

### 5.1 Injected failure 1 — prediction API unavailable (Kubernetes)

- **Injection:** `scripts/inject_failure_k8s.sh` changed `MODEL_PATH` in the ConfigMap to
  `/app/model/model-v2-missing.pkl` and replaced the Pods.
- **Symptom:** both Pods `Running` but `0/1` ready ([evidence](evidence/k8s_injected_failure_pods.txt)).
- **How the agent diagnosed it:** `k8s_list_workloads` showed 0 of 2 replicas ready with
  `MinimumReplicasUnavailable`; `k8s_pod_logs` returned
  `readiness_failed … FileNotFoundError: … '/app/model/model-v2-missing.pkl'`;
  `k8s_deployed_version` showed the ConfigMap value `MODEL_PATH=/app/model/model-v2-missing.pkl`;
  `k8s_services` showed no ready endpoints.
- **Root cause:** the ConfigMap points to a model file that is not in the image. The process
  stays alive (liveness passes) but cannot load a model, so readiness returns 503 and the
  Service has no endpoints.
- **Resolution:** `scripts/inject_failure_k8s.sh restore` re-applied
  `kubernetes/configmap.yaml` and replaced the Pods; both returned to `1/1`
  ([evidence](evidence/k8s_after_restore_pods.txt)).
- Transcripts: [conversation 4](conversations/4_deployment_diagnosis.md),
  [root-cause follow-up](conversations/failure_1_k8s_missing_model.md).

### 5.2 Injected failure 2 — training pipeline failed (Airflow quality gate)

- **Injection:** `scripts/inject_failure_airflow.sh` triggered the DAG with `min_f1 = 0.999`.
- **How the agent diagnosed it:** `airflow_list_dag_runs` showed the latest run `failed`;
  `airflow_failed_tasks` separated the root-cause task `quality_gate` from the blocked
  `register_model` (`upstream_failed`) and returned the log line
  `QUALITY GATE FAILED: f1 0.9645 is below the required 0.9990`; `mlflow_get_run` confirmed
  the run's `quality_gate` tag is `failed`.
- **Root cause:** the model's F1 (0.9645) was below the threshold passed in the run's conf.
- **Effect on promotion:** `register_model` never ran, so no model version was created for
  that run. The gate is not retried (it exits with the "rejected" code).
- **Resolution:** the next run with the default threshold 0.93 passed and registered a model
  (conversation 3).
- Transcript: [failure_2_airflow_quality_gate.md](conversations/failure_2_airflow_quality_gate.md).

### 5.3 Unexpected failures met during development

| Failure | Root cause | Fix |
|---|---|---|
| Random-forest runs crashed in `evaluate` with "untrusted types" | MLflow 3.18 saves sklearn models in the skops format and rejects `sklearn.tree._tree.Tree` by default | Pass `skops_trusted_types` in `log_model` |
| `airflow standalone` exited with `FileNotFoundError: 'airflow'` | It launches its components by calling `airflow` from `PATH`; the venv was not on `PATH` | Export the venv `bin` in `scripts/start_airflow.sh` |
| LLM call rejected with HTTP 413 "Requested 8465, limit 8000" | Tool results from five calls plus 30 tool schemas exceeded the free tier's per-request token limit | Compact tool outputs and a per-turn context budget in the orchestrator |
| `mlflow_model_registry` timed out after 30 s | Machine was swapping heavily while the Docker image was building | None needed: the agent reported the timeout in its availability notice instead of guessing |
| Agent skipped Airflow in the multi-tool scenario | The LLM did not follow the prompt instruction | Runtime rule: a retraining verdict is sent back once until all four systems were queried (unit-tested) |
| A DVC md5 made only of digits was read as an integer | YAML type inference (found by a unit test) | Cast the md5 to `str` when parsing `.dvc` files |

## 6. Testing

0 pytest tests, all external systems mocked; `ruff check` and `ruff format --check` clean;
the same commands run in GitHub Actions on every pull request. Required cases and where they
are covered:

| Requirement | Test |
|---|---|
| Successful tool selection and execution | `test_selected_tool_is_executed_and_its_result_reaches_the_model`, `test_multi_tool_request_gathers_evidence_from_all_four_systems` |
| Tool timeout | `test_tool_timeout_is_reported_not_hung` |
| Invalid requests | `test_invalid_tool_requests_are_rejected_before_execution`, `test_chat_endpoint_rejects_invalid_requests` |
| Service failure / unavailable service (mocked) | `test_service_failure_is_surfaced_as_error`, `test_unreachable_*_maps_to_unavailable` |
| Agent does not invent results | `test_agent_does_not_invent_results_when_a_tool_is_unavailable` |
| Unauthorized Kubernetes modifications rejected | `test_unauthorized_k8s_modifications_are_rejected`, `test_k8s_restart_without_confirmation_is_not_executed` |
| Structured logs without secrets | `test_structured_logs_record_tool_status_duration_and_request_id`, `test_secrets_are_redacted_from_logs` |

## 7. Known limitations and proposed improvements

| Limitation | Improvement |
|---|---|
| Sessions and pending confirmations are held in memory of one process | Store them in Redis so the agent can run with several replicas and survive restarts |
| The LLM free tier allows 8,000 tokens per request, so tool results shown to the model are shortened (the full results are still returned to the caller) | Use a paid tier, or select a subset of tools per request to shrink the prompt |
| The agent's answer text is not checked claim by claim against tool results; the runtime only guarantees notices for failed tools and returns the raw evidence | Add a verification pass that matches ids and numbers in the answer to the evidence |
| The model is baked into the image, so deploying a new model means building a new image by hand | A CD step that builds and rolls out an image when a model version gets the `candidate` alias |
| The DVC remote is a local folder | Use S3/GCS so other machines can `dvc pull` |
| Airflow runs in `standalone` mode with SQLite and the simple auth manager | Postgres, a production auth manager and the KubernetesExecutor |
| The agent API has no authentication of its own | Put it behind OAuth/API keys and map users to permissions |
| The retraining policy is evaluated on recorded metadata only; there is no live monitoring of the prediction API | Export prediction metrics (Prometheus) and feed measured drift into the policy tool |
| Test split is small (80–114 rows), so F1 differences of under 0.01 between runs are not significant | Cross-validation and confidence intervals in the evaluation stage |
