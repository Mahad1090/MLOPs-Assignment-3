"""Runtime configuration, read from environment variables (and a local .env file).

No secret has a usable default: credentials must be supplied through the
environment and are never written to logs or to the repository.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    # --- LLM (any OpenAI-compatible chat-completions endpoint; Groq by default) ---
    llm_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="GROQ_API_KEY")
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_timeout_seconds: float = 60.0
    agent_max_steps: int = 12

    # --- repository / DVC ---
    repo_root: Path = REPO_ROOT
    dvc_bin: str = str(REPO_ROOT / ".venv" / "bin" / "dvc")
    dataset_path: str = "data/raw/dataset.csv"

    # --- MLflow ---
    mlflow_tracking_uri: str = "http://127.0.0.1:5000"
    mlflow_experiment: str = "breast-cancer-classifier"
    mlflow_model_name: str = "breast-cancer-classifier"

    # --- Airflow 3 ---
    airflow_base_url: str = "http://127.0.0.1:8080"
    airflow_username: str = "admin"
    airflow_password: SecretStr = SecretStr("")
    airflow_approved_dags: str = "training_pipeline"

    # --- Kubernetes ---
    k8s_context: str = "kind-mlops"
    k8s_namespace: str = "mlops"  # the only namespace the agent may read or change
    k8s_app_label: str = "app.kubernetes.io/name=prediction-api"
    k8s_approved_deployments: str = "prediction-api"
    k8s_max_replicas: int = 4

    # --- reliability ---
    tool_timeout_seconds: float = 30.0
    retraining_policy_path: Path = REPO_ROOT / "config" / "retraining_policy.yaml"

    @property
    def llm_api_keys(self) -> list[str]:
        """All configured LLM keys: GROQ_API_KEY plus GROQ_API_KEY1, GROQ_API_KEY2, ...

        The first is used; the others are fallbacks when a key is rate limited.
        """
        from dotenv import dotenv_values

        env_file = self.model_config.get("env_file")
        merged = {**(dotenv_values(env_file) if env_file else {}), **os.environ}
        numbered = sorted(k for k in merged if re.fullmatch(r"GROQ_API_KEY\d+", k))
        keys = [self.llm_api_key.get_secret_value(), *(merged[k] or "" for k in numbered)]
        return list(dict.fromkeys(k.strip() for k in keys if k and k.strip()))

    @property
    def approved_dags(self) -> set[str]:
        return {d.strip() for d in self.airflow_approved_dags.split(",") if d.strip()}

    @property
    def approved_deployments(self) -> set[str]:
        return {d.strip() for d in self.k8s_approved_deployments.split(",") if d.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
