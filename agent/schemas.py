"""Structured inputs and outputs shared by the API, the orchestrator and the tools."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ToolStatus = Literal[
    "ok",  # the tool ran and returned evidence
    "error",  # the backing service answered with an error
    "unavailable",  # the backing service could not be reached
    "timeout",  # the tool exceeded its time budget
    "invalid_input",  # arguments failed schema validation
    "denied",  # blocked by a permission rule
    "confirmation_required",  # state-changing action parked until the user confirms
]

# Statuses that mean "no evidence was obtained from this tool".
FAILED_STATUSES = {"error", "unavailable", "timeout", "invalid_input", "denied"}


class ToolArgs(BaseModel):
    """Base class for tool inputs. Unknown fields are rejected, so the model
    cannot smuggle in flags such as ``confirm=true``."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(ToolArgs):
    pass


class ToolResult(BaseModel):
    tool: str
    status: ToolStatus
    data: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    pending_action_id: str | None = None

    def for_llm(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": self.tool, "status": self.status}
        if self.status == "ok":
            out["data"] = self.data
        else:
            out["error"] = self.error
            out["note"] = (
                "No operational data was obtained from this tool. Do not state or guess "
                "what it would have returned."
            )
        if self.status == "confirmation_required":
            out["data"] = self.data
            out["note"] = (
                "This action has NOT been executed. Tell the user exactly what would be "
                "done and that they must confirm it."
            )
        return out


class ToolCallRecord(BaseModel):
    """One executed (or rejected) tool call, returned to the caller as evidence."""

    tool: str
    arguments: dict[str, Any]
    status: ToolStatus
    duration_ms: float
    result: Any = None
    error: str | None = None


class PendingAction(BaseModel):
    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str
    tool: str
    arguments: dict[str, Any]
    summary: str
    created_at: float = Field(default_factory=time.time)


class PendingActionView(BaseModel):
    action_id: str
    tool: str
    arguments: dict[str, Any]
    summary: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    confirm_action_id: str | None = Field(
        default=None,
        max_length=32,
        description="Id of a pending state-changing action the user explicitly approves.",
    )


class ChatResponse(BaseModel):
    request_id: str
    session_id: str
    answer: str
    tool_calls: list[ToolCallRecord] = []
    failed_tools: list[str] = []
    pending_actions: list[PendingActionView] = []
