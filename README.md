# MLOps AI Agent — Assignment 03

A conversational agent that answers operational questions about a machine-learning system by
calling real tools, not by guessing:

| System | Used for | How the agent talks to it |
|---|---|---|
| **DVC** | dataset versions and lineage | allowlisted `git` / `dvc` commands (subprocess, no shell) |
| **MLflow** | runs, metrics, model registry | `MlflowClient` |
| **Apache Airflow 3** | training pipeline | REST API v2 with JWT authentication |
| **Kubernetes** | deployed prediction API | official Python client |

The LLM (Groq, through the OpenAI-compatible API) only selects tools and words the answer.
Every answer is returned together with the raw tool results it is based on.

- Design, tool choices and safety model: [architecture.md](architecture.md)
- Technical report with evidence, conversations and failure investigations: [report/technical_report.md](report/technical_report.md)

## Repository layout

```
agent/                 the agent
  main.py              FastAPI interface (POST /chat, GET /tools)
  cli.py               terminal chat
  orchestrator.py      agent loop, confirmation flow, runtime notices
  llm.py               LLM client (OpenAI-compatible)
  schemas.py           request / response / tool-result models
  tools/               dvc_tools, mlflow_tools, airflow_tools, kubernetes_tools, policy_tools, base
api/main.py            FastAPI prediction service (runs in Kubernetes)
dags/training_pipeline.py   Airflow 3 training DAG
training/              pipeline stages: validate, preprocess, train, evaluate, quality_gate, register
data/raw/dataset.csv.dvc    DVC pointer to the dataset (versions tagged data-v1.0, data-v2.0)
dvc.yaml, params.yaml  DVC data pipeline and parameters
kubernetes/            namespace, configmap, deployment, service, rbac
config/retraining_policy.yaml   explicit retraining policy the agent evaluates
scripts/               start services, build cluster, inject failures, make dataset versions
tests/                 pytest suite (everything external is mocked)
.github/workflows/ci.yml    lint + tests on every pull request
```

## 1. Installation

Requirements: Linux or macOS, Python 3.12, Docker, git. No root access is needed.

```bash
git clone https://github.com/Mahad1090/MLOPs-Assignment-3.git
cd MLOPs-Assignment-3

# project environment (agent, training, tests) — pinned versions
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e . --no-deps

# Airflow gets its own environment because it pins its own dependency set
python3.12 -m venv .venv-airflow
.venv-airflow/bin/pip install "apache-airflow==3.4.0" \
  --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.4.0/constraints-3.12.txt"

# kubectl and kind into ~/.local/bin (skip if you already have them)
mkdir -p ~/.local/bin
curl -Lo ~/.local/bin/kubectl "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
curl -Lo ~/.local/bin/kind https://kind.sigs.k8s.io/dl/latest/kind-linux-amd64
chmod +x ~/.local/bin/kubectl ~/.local/bin/kind
```

## 2. Environment configuration

```bash
cp .env.example .env      # .env is git-ignored
```

| Variable | Meaning |
|---|---|
| `GROQ_API_KEY` | LLM key, free at <https://console.groq.com/keys>. Optional `GROQ_API_KEY1`, `GROQ_API_KEY2`, … are used as fallbacks when a key is rate limited |
| `LLM_MODEL`, `LLM_BASE_URL` | model and endpoint (any OpenAI-compatible provider works) |
| `MLFLOW_TRACKING_URI` | default `http://127.0.0.1:5000` |
| `AIRFLOW_BASE_URL`, `AIRFLOW_USERNAME`, `AIRFLOW_PASSWORD` | Airflow API login. Choose any password; `scripts/start_airflow.sh` creates the user with it |
| `AIRFLOW_APPROVED_DAGS` | DAG ids the agent may trigger (default `training_pipeline`) |
| `K8S_CONTEXT`, `K8S_NAMESPACE`, `K8S_APPROVED_DEPLOYMENTS` | the only context, namespace and Deployments the agent may touch |

No secret is stored in the repository.

## 3. Launching each service

Run each in its own terminal, from the repository root.

### 3.1 DVC (dataset)

```bash
source .venv/bin/activate
dvc pull                 # fetches data/raw/dataset.csv (remote: ../mlops-a3-dvc-remote, a local folder)
dvc repro                # validate + preprocess (cached when nothing changed)
```

The dataset has two versions, tagged in git as `data-v1.0` (400 rows) and `data-v2.0`
(569 rows). They are generated deterministically by `scripts/make_dataset.py`, so they can
be recreated on a machine that does not have the remote folder:

```bash
python -m scripts.make_dataset --version v2.0     # md5 matches data/raw/dataset.csv.dvc
```

Reproducible checkout of the older version:

```bash
git checkout data-v1.0 -- data/raw/dataset.csv.dvc && dvc checkout data/raw/dataset.csv.dvc
md5sum data/raw/dataset.csv                        # equals the md5 in the .dvc file
git checkout HEAD -- data/raw/dataset.csv.dvc && dvc checkout data/raw/dataset.csv.dvc
```

### 3.2 MLflow

```bash
scripts/start_mlflow.sh          # http://127.0.0.1:5000
```

### 3.3 Airflow 3

```bash
scripts/start_airflow.sh         # http://127.0.0.1:8080  (admin / $AIRFLOW_PASSWORD)
```

The DAG `training_pipeline` appears automatically:

```
validate_dataset → preprocess_data → train_model → evaluate_and_log_mlflow → quality_gate → register_model
```

Run it once so there is a model to deploy (UI ▸ Trigger, or `python -m training.pipeline`
to run the same stages without Airflow).

### 3.4 Kubernetes

```bash
scripts/setup_cluster.sh         # kind cluster "mlops", image build + load, kubectl apply
kubectl --context kind-mlops -n mlops get deploy,pods,svc
```

This exports the registered `candidate` model from MLflow into `serving_model/`, builds
`prediction-api:1.0.0`, loads it into kind and applies `kubernetes/`.

Try the API:

```bash
kubectl --context kind-mlops -n mlops port-forward svc/prediction-api 8000:8000 &
curl -s localhost:8000/version
```

### 3.5 The agent

```bash
source .venv/bin/activate
uvicorn agent.main:app --port 8001      # HTTP interface
# or
python -m agent.cli                     # terminal chat
```

## 4. Talking to the agent

### Terminal

```text
$ python -m agent.cli --quiet-logs
You> Which dataset version was used for the best run?
You> Why is my prediction API unavailable?
You> Trigger the model training pipeline and report whether execution succeeded.
Confirm 'Trigger Airflow DAG 'training_pipeline' with conf (defaults)'? [y/N] y
```

`-q "question"` asks one question and exits, `--full` prints full tool results,
`--transcript file.md` saves the conversation with its evidence.

### HTTP

```bash
curl -s localhost:8001/chat -H 'Content-Type: application/json' \
  -d '{"message": "Compare the last five training runs and recommend the best candidate based on F1 score.", "session_id": "demo"}' | python -m json.tool
```

Response fields: `answer`, `tool_calls` (name, arguments, status, duration, raw result),
`failed_tools`, `pending_actions`, `request_id`, `session_id`.

**State-changing actions need a second, explicit request.** When the agent wants to trigger
the DAG, restart or scale the Deployment, nothing is executed; the response lists a pending
action:

```bash
curl -s localhost:8001/chat -H 'Content-Type: application/json' \
  -d '{"message": "Restart the prediction API", "session_id": "demo"}'
# → "pending_actions": [{"action_id": "3f9c1a2b7d44", "summary": "Rollout-restart deployment ..."}]

curl -s localhost:8001/chat -H 'Content-Type: application/json' \
  -d '{"message": "Yes, go ahead", "session_id": "demo", "confirm_action_id": "3f9c1a2b7d44"}'
```

`GET /tools` lists every tool with its input schema and whether it changes state.

## 5. Running the tests

```bash
source .venv/bin/activate
ruff check . && ruff format --check .
pytest -v
```

The suite needs no running service, no cluster and no API key: the LLM, MLflow client,
git/DVC runner, Airflow HTTP transport and Kubernetes clients are all replaced by fakes.
It covers tool selection and execution, a multi-tool request across all four systems, tool
timeouts, invalid requests, service failures, the "does not invent results" guarantee,
rejection of unauthorized Kubernetes changes, the confirmation flow, and secret redaction in
logs. GitHub Actions runs the same lint and tests on every pull request.

## 6. Injected failures (for the demonstration)

```bash
scripts/inject_failure_k8s.sh            # ConfigMap points at a missing model → Pods never Ready
python -m agent.cli -q "Why is my prediction API unavailable?"
scripts/inject_failure_k8s.sh restore

scripts/inject_failure_airflow.sh        # quality gate threshold 0.999 → gate fails, nothing registered
python -m agent.cli -q "Did the last training pipeline run succeed? If not, why?"
```

Both investigations are written up in the technical report.

## 7. Evidence

- `report/evidence/` — exported output from DVC, MLflow, Airflow, Kubernetes and CI
- `report/conversations/` — the five agent conversations and two failure investigations, each
  with the raw tool results
- `screenshots/` — MLflow UI
