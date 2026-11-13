"""LLM access behind a small interface so tests can substitute a scripted model."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.config import Settings


class LLMError(Exception):
    """The language model could not produce a turn."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None


@dataclass
class LLMTurn:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], allow_tools: bool = True
    ) -> LLMTurn: ...


def parse_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    if not raw or not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"tool arguments were not valid JSON ({exc.msg})"
    if parsed is None:
        return {}, None
    if not isinstance(parsed, dict):
        return {}, "tool arguments must be a JSON object"
    return parsed, None


class OpenAICompatibleLLM:
    """Chat-completions client for any OpenAI-compatible endpoint (Groq by default)."""

    def __init__(self, settings: Settings):
        from openai import OpenAI

        keys = settings.llm_api_keys
        if not keys:
            raise LLMError("no LLM API key configured (set GROQ_API_KEY in the environment)")
        self._model = settings.llm_model
        # One client per key. The SDK backs off and honours Retry-After on HTTP 429; if a
        # key stays rate limited the next configured key is tried.
        self._clients = [
            OpenAI(
                api_key=key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout_seconds,
                max_retries=4 if len(keys) == 1 else 1,
            )
            for key in keys
        ]
        self._current = 0

    def _create(self, **kwargs: Any) -> Any:
        from openai import RateLimitError

        last: Exception | None = None
        for attempt in range(4):
            for _ in range(len(self._clients)):
                try:
                    return self._clients[self._current].chat.completions.create(**kwargs)
                except RateLimitError as exc:
                    last = exc
                    self._current = (self._current + 1) % len(self._clients)
            time.sleep(15 * (attempt + 1))  # every key is rate limited: wait for the window
        raise last

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], allow_tools: bool = True
    ) -> LLMTurn:
        from openai import APIError, BadRequestError

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 1500,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto" if allow_tools else "none"
        last_error: Exception | None = None
        for _ in range(2):  # one retry when the model emits a malformed tool call
            try:
                response = self._create(**kwargs)
                break
            except BadRequestError as exc:
                last_error = exc
                if "tool" not in str(exc).lower():
                    raise LLMError(f"LLM rejected the request: {str(exc)[:200]}") from exc
            except APIError as exc:
                raise LLMError(f"LLM request failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
        else:
            raise LLMError(f"LLM produced an invalid tool call twice: {str(last_error)[:200]}")

        message = response.choices[0].message
        calls = []
        for call in message.tool_calls or []:
            arguments, error = parse_arguments(call.function.arguments)
            calls.append(ToolCall(call.id, call.function.name, arguments, error))
        return LLMTurn(content=message.content, tool_calls=calls)
