"""Tool definition, registry and the guarded executor every tool call goes through."""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from agent.logging_utils import get_logger, log_event
from agent.schemas import ToolArgs, ToolResult

logger = get_logger("tools")


def tail_within(lines: list[str], max_chars: int = 2200, max_line: int = 300) -> list[str]:
    """The most recent lines that fit in a character budget (newest lines are kept)."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        line = line[:max_line]
        if used + len(line) > max_chars and kept:
            break
        kept.append(line)
        used += len(line)
    return kept[::-1]


class ToolError(Exception):
    """The backing service answered, but with an error."""


class ToolUnavailableError(ToolError):
    """The backing service could not be reached at all."""


class PermissionDeniedError(ToolError):
    """The request is outside what the agent is allowed to do."""


@dataclass(frozen=True)
class ToolContext:
    request_id: str
    # True only when the *user* approved this exact action through the API's
    # confirm_action_id field. The LLM has no way to set it.
    confirmed: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[ToolArgs]
    func: Callable[[Any, ToolContext], Any]
    category: str
    mutating: bool = False  # changes external state -> needs user confirmation
    timeout_seconds: float | None = None
    summarize: Callable[[Any], str] | None = None  # human summary for confirmations
    # Permission check that needs no confirmation (allowlists). Raises PermissionDeniedError.
    precheck: Callable[[Any], None] | None = None

    def json_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            # Limits are enforced by validation; leaving them out keeps the prompt small.
            for key in ("title", "pattern", "maxLength", "minLength"):
                prop.pop(key, None)
        schema.setdefault("properties", {})
        return schema

    def llm_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None, default_timeout: float = 30.0):
        self._tools: dict[str, Tool] = {}
        self.default_timeout = default_timeout
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def llm_specs(self) -> list[dict[str, Any]]:
        return [t.llm_spec() for t in self._tools.values()]

    def validate(self, name: str, raw_args: dict[str, Any]) -> tuple[Tool | None, Any, str | None]:
        """Return (tool, parsed_args, error). Exactly one of parsed_args/error is set."""
        tool = self._tools.get(name)
        if tool is None:
            return None, None, f"unknown tool '{name}'. Available: {', '.join(self.names())}"
        try:
            return tool, tool.args_model.model_validate(raw_args or {}), None
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in exc.errors()
            )
            return tool, None, f"invalid arguments: {problems}"

    def execute(self, name: str, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Validate, authorise, run with a time budget, and log one tool call.

        Never raises: every outcome is reported as a ToolResult status so the
        orchestrator can pass an honest result (or failure) to the LLM.
        """
        started = time.perf_counter()
        tool, args, error = self.validate(name, raw_args)
        status = "ok"
        data: Any = None

        if error is None and tool.precheck is not None:
            try:
                tool.precheck(args)
            except PermissionDeniedError as exc:
                status, error = "denied", str(exc)

        if status == "denied":
            pass
        elif error is not None:
            status = "invalid_input"
        elif tool.mutating and not ctx.confirmed:
            status, error = "denied", "state-changing tool called without user confirmation"
        else:
            timeout = tool.timeout_seconds or self.default_timeout
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = pool.submit(tool.func, args, ctx)
            try:
                data = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                status, error = "timeout", f"tool did not finish within {timeout:.0f}s"
            except PermissionDeniedError as exc:
                status, error = "denied", str(exc)
            except ToolUnavailableError as exc:
                status, error = "unavailable", str(exc)
            except ToolError as exc:
                status, error = "error", str(exc)
            except Exception as exc:  # noqa: BLE001 - a tool bug must not crash the agent
                status, error = "error", f"{type(exc).__name__}: {exc}"
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        log_event(
            logger,
            "tool_call",
            request_id=ctx.request_id,
            tool=name,
            category=tool.category if tool else None,
            status=status,
            duration_ms=duration_ms,
            mutating=bool(tool and tool.mutating),
            confirmed=ctx.confirmed,
            arguments=raw_args,
            error=error,
        )
        return ToolResult(tool=name, status=status, data=data, error=error, duration_ms=duration_ms)
