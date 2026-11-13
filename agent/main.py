"""FastAPI interface of the MLOps agent.

Run with:  uvicorn agent.main:app --port 8001
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

from agent.config import get_settings
from agent.llm import LLMError, OpenAICompatibleLLM
from agent.logging_utils import configure_logging
from agent.orchestrator import Orchestrator
from agent.schemas import ChatRequest, ChatResponse
from agent.tools import build_registry

configure_logging()
app = FastAPI(
    title="MLOps AI Agent",
    version="1.0.0",
    description="Conversational agent over DVC, MLflow, Airflow and Kubernetes.",
)


@lru_cache
def _build_orchestrator() -> Orchestrator:
    settings = get_settings()
    registry = build_registry(default_timeout=settings.tool_timeout_seconds)
    return Orchestrator(OpenAICompatibleLLM(settings), registry, max_steps=settings.agent_max_steps)


def get_orchestrator() -> Orchestrator:
    try:
        return _build_orchestrator()
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tools")
def tools() -> dict[str, Any]:
    """Tool definitions and permissions, as given to the LLM."""
    registry = build_registry()
    return {
        "count": len(registry.tools()),
        "tools": [
            {
                "name": t.name,
                "category": t.category,
                "description": t.description,
                "changes_state": t.mutating,
                "requires_user_confirmation": t.mutating,
                "input_schema": t.json_schema(),
            }
            for t in registry.tools()
        ],
    }


@app.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator),  # noqa: B008
    x_request_id: str | None = Header(default=None, max_length=64),
) -> ChatResponse:
    request_id = x_request_id if x_request_id and x_request_id.isalnum() else uuid.uuid4().hex
    return orchestrator.chat(
        request.message,
        session_id=request.session_id,
        confirm_action_id=request.confirm_action_id,
        request_id=request_id,
    )
