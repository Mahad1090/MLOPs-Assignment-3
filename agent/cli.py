"""Terminal chat with the agent (no server needed).

python -m agent.cli                      # interactive
python -m agent.cli -q "question"        # one question
python -m agent.cli -q "..." --transcript report/conversations/01.md
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from agent.config import get_settings
from agent.llm import OpenAICompatibleLLM
from agent.logging_utils import configure_logging
from agent.orchestrator import Orchestrator
from agent.schemas import ChatResponse
from agent.tools import build_registry


def _evidence(response: ChatResponse, full: bool) -> str:
    lines = []
    for call in response.tool_calls:
        lines.append(
            f"  - {call.tool}({json.dumps(call.arguments)}) -> {call.status} [{call.duration_ms:.0f} ms]"
        )
        if full:
            payload = call.result if call.status == "ok" else {"error": call.error}
            body = json.dumps(payload, indent=2, default=str)
            lines.append("    " + body.replace("\n", "\n    "))
    return "\n".join(lines) or "  (no tools called)"


def _write_transcript(path: Path, turns: list[tuple[str, ChatResponse]]) -> None:
    out = []
    for question, response in turns:
        out += [
            f"**User:** {question}",
            "",
            f"**Agent** (request id `{response.request_id}`):",
            "",
            response.answer,
            "",
            "**Tool calls (actual evidence):**",
            "",
            "```text",
            _evidence(response, full=False),
            "```",
            "",
            "<details><summary>Raw tool results</summary>",
            "",
            "```json",
            json.dumps([c.model_dump() for c in response.tool_calls], indent=1, default=str),
            "```",
            "",
            "</details>",
            "",
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out))


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the MLOps agent")
    parser.add_argument("-q", "--question", action="append", help="ask this and exit (repeatable)")
    parser.add_argument("--yes", action="store_true", help="auto-confirm pending actions (demo)")
    parser.add_argument("--full", action="store_true", help="print full tool results")
    parser.add_argument("--transcript", type=Path, help="write the conversation to a markdown file")
    parser.add_argument("--quiet-logs", action="store_true", help="hide structured logs")
    opts = parser.parse_args()

    configure_logging(logging.WARNING if opts.quiet_logs else logging.INFO)
    settings = get_settings()
    agent = Orchestrator(
        OpenAICompatibleLLM(settings),
        build_registry(default_timeout=settings.tool_timeout_seconds),
        max_steps=settings.agent_max_steps,
    )
    session_id = "cli"
    turns: list[tuple[str, ChatResponse]] = []
    scripted = list(opts.question or [])

    def ask(text: str, confirm: str | None = None) -> ChatResponse:
        response = agent.chat(text, session_id=session_id, confirm_action_id=confirm)
        turns.append((text + (f"  _(confirming action `{confirm}`)_" if confirm else ""), response))
        print(f"\nAgent> {response.answer}\n\nTools used:\n{_evidence(response, opts.full)}\n")
        return response

    while True:
        if scripted:
            question = scripted.pop(0)
            print(f"You> {question}")
        elif opts.question:
            break
        else:
            try:
                question = input("You> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if question.lower() in {"exit", "quit"}:
                break
            if not question:
                continue
        response = ask(question)
        for action in response.pending_actions:
            approve = opts.yes or (
                not opts.question and input(f"Confirm '{action.summary}'? [y/N] ").strip().lower() == "y"
            )
            if approve:
                print(f"You> yes, confirmed ({action.action_id})")
                ask("Yes, I confirm. Go ahead and report the outcome.", confirm=action.action_id)

    if opts.transcript:
        _write_transcript(opts.transcript, turns)
        print(f"transcript written to {opts.transcript}")


if __name__ == "__main__":
    main()
