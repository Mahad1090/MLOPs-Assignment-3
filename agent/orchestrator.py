"""The agent loop: LLM tool selection, guarded execution, confirmation and tracing."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent.llm import LLMClient, LLMError, ToolCall
from agent.logging_utils import get_logger, log_event
from agent.schemas import (
    FAILED_STATUSES,
    ChatResponse,
    PendingAction,
    PendingActionView,
    ToolCallRecord,
    ToolResult,
)
from agent.tools.base import PermissionDeniedError, ToolContext, ToolRegistry

logger = get_logger("orchestrator")

MAX_TOOL_RESULT_CHARS = 2600
# All tool results of one turn together; keeps a request within small per-minute token limits.
MAX_TURN_TOOL_CHARS = 8000
_TRUNCATED = '..."[TRUNCATED by runtime: ask a narrower question for the rest]'
PENDING_ACTION_TTL_SECONDS = 600
MAX_HISTORY_MESSAGES = 12

SYSTEM_PROMPT = """You are an MLOps operations agent. You answer by calling tools that query the \
real systems: DVC (dataset versions), MLflow (experiments, runs, model registry), Apache Airflow \
(training pipeline) and Kubernetes (the deployed prediction API).

Evidence rules
- Every operational statement (ids, versions, metrics, states, counts, log lines) must come from \
a tool result in this conversation. Never answer such questions from memory and never estimate.
- If a tool returns a status other than "ok", you have NO data from it. Say which system could \
not be checked and why, and do not guess what it would have shown.
- Quote identifiers exactly as returned (run ids, commits, md5, pod names).

Tool use
- Work out which systems the request involves and call every tool you need; call independent \
tools in the same step.
- Dataset used by a model or run: read the provenance recorded on the MLflow run (dvc.md5, \
dvc.dataset_version), then confirm it with dvc_find_version using the md5. git.commit is the \
code revision, not the dataset revision.
- Best model: use mlflow_best_run and report the comparison. A top metric alone does not make a \
model production-ready; mention the quality gate and the caveats returned.
- Deployment health or "API unavailable": k8s_list_workloads and k8s_unhealthy_pods, then \
k8s_pod_status and k8s_pod_logs of an affected pod, and k8s_services for endpoints.
- Pipeline failures: airflow_failed_tasks, then airflow_task_logs if more detail is needed.
- Whether to retrain: you MUST call all of mlflow_best_run, dvc_find_version, \
k8s_deployed_version (which model run is serving), airflow_list_dag_runs (recent training runs) \
and evaluate_retraining_need for the deployed model's run. If the top-ranked run was trained on \
an older dataset version, say so and name the best run on the current dataset version. Recommend retraining \
only if it returns retraining_recommended=true, and cite its reasons. Otherwise say retraining \
is not indicated by the policy and list its unknowns.

Safety
- Tools that change state (airflow_trigger_dag, k8s_restart_deployment, k8s_scale_deployment) are \
not executed when you call them: they return "confirmation_required". Then tell the user exactly \
what would be done and that it has NOT been done yet. Never claim it happened.
- Only call a state-changing tool when the user explicitly asked for that action in their latest \
message. Being asked whether retraining is necessary is not a request to start training.

Answer style
- Be concise and concrete. Use plain ASCII hyphens in identifiers. For diagnostic or \
multi-system questions use three sections: \
"Known facts" (each with the tool it came from), "Uncertainties", "Recommended actions".
"""


@dataclass
class Session:
    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, PendingAction] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Orchestrator:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, max_steps: int = 12):
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps
        self._sessions: dict[str, Session] = {}
        self._sessions_lock = threading.Lock()

    # ------------------------------------------------------------------ sessions

    def _session(self, session_id: str | None) -> Session:
        with self._sessions_lock:
            sid = session_id or uuid.uuid4().hex[:16]
            return self._sessions.setdefault(sid, Session(session_id=sid))

    @staticmethod
    def _expire_pending(session: Session) -> None:
        cutoff = time.time() - PENDING_ACTION_TTL_SECONDS
        for action_id in [a for a, p in session.pending.items() if p.created_at < cutoff]:
            del session.pending[action_id]

    # ------------------------------------------------------------------ tool dispatch

    def _dispatch(self, call: ToolCall, session: Session, request_id: str) -> ToolResult:
        """Run one tool call requested by the LLM. State-changing tools are parked, not run."""
        ctx = ToolContext(request_id=request_id, confirmed=False)
        if call.parse_error:
            log_event(
                logger,
                "tool_call",
                request_id=request_id,
                tool=call.name,
                status="invalid_input",
                duration_ms=0.0,
                error=call.parse_error,
            )
            return ToolResult(tool=call.name, status="invalid_input", error=call.parse_error)

        tool, args, error = self.registry.validate(call.name, call.arguments)
        if tool is None or error is not None or not tool.mutating:
            return self.registry.execute(call.name, call.arguments, ctx)

        # Valid request for a state-changing tool: check allowlists, then ask the user.
        try:
            if tool.precheck is not None:
                tool.precheck(args)
        except PermissionDeniedError:
            return self.registry.execute(call.name, call.arguments, ctx)  # logs + returns "denied"

        summary = tool.summarize(args) if tool.summarize else f"Run {tool.name}"
        action = PendingAction(
            session_id=session.session_id,
            tool=tool.name,
            arguments=args.model_dump(),
            summary=summary,
        )
        session.pending[action.action_id] = action
        log_event(
            logger,
            "tool_call",
            request_id=request_id,
            tool=tool.name,
            category=tool.category,
            status="confirmation_required",
            duration_ms=0.0,
            mutating=True,
            confirmed=False,
            arguments=call.arguments,
            pending_action_id=action.action_id,
        )
        return ToolResult(
            tool=tool.name,
            status="confirmation_required",
            data={"action_id": action.action_id, "would_do": summary, "executed": False},
            pending_action_id=action.action_id,
        )

    @staticmethod
    def _record(result: ToolResult, arguments: dict[str, Any]) -> ToolCallRecord:
        return ToolCallRecord(
            tool=result.tool,
            arguments=arguments,
            status=result.status,
            duration_ms=result.duration_ms,
            result=result.data,
            error=result.error,
        )

    @staticmethod
    def _tool_message(call_id: str, result: ToolResult) -> dict[str, Any]:
        content = json.dumps(result.for_llm(), default=str, separators=(",", ":"))
        if len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + _TRUNCATED
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    @staticmethod
    def _missing_retraining_evidence(records: list[ToolCallRecord]) -> str | None:
        """A retraining verdict must rest on all four systems; name any not yet queried."""
        # Listing DAGs says nothing about runs, so it does not count as Airflow evidence.
        used = {r.tool.split("_")[0] for r in records if r.tool != "airflow_list_dags"}
        if "evaluate" not in used:
            return None
        missing = sorted({"dvc", "mlflow", "airflow", "k8s"} - used)
        if not missing:
            return None
        return (
            f"[Runtime note: before answering about retraining you must also query: {missing}. "
            "Call those tools now (airflow_list_dag_runs for the state of recent training runs), "
            "then give the complete answer including what they showed.]"
        )

    @staticmethod
    def _fit_budget(messages: list[dict[str, Any]]) -> None:
        """Shrink the largest tool results until the turn fits the context budget.

        Only what the LLM sees is shortened; the full results stay in the response.
        """
        tool_messages = [m for m in messages if m["role"] == "tool"]
        while sum(len(m["content"]) for m in tool_messages) > MAX_TURN_TOOL_CHARS:
            largest = max(tool_messages, key=lambda m: len(m["content"]))
            keep = max(600, int(len(largest["content"]) * 0.6))
            if keep >= len(largest["content"]):
                break
            largest["content"] = largest["content"][:keep] + _TRUNCATED

    # ------------------------------------------------------------------ main entry

    def chat(
        self,
        message: str,
        session_id: str | None = None,
        confirm_action_id: str | None = None,
        request_id: str | None = None,
    ) -> ChatResponse:
        request_id = request_id or uuid.uuid4().hex
        session = self._session(session_id)
        with session.lock:
            return self._chat(message, session, confirm_action_id, request_id)

    def _chat(
        self, message: str, session: Session, confirm_action_id: str | None, request_id: str
    ) -> ChatResponse:
        started = time.perf_counter()
        self._expire_pending(session)
        records: list[ToolCallRecord] = []
        user_content = message
        log_event(
            logger,
            "request_started",
            request_id=request_id,
            session_id=session.session_id,
            confirming=bool(confirm_action_id),
            message_chars=len(message),
        )

        if confirm_action_id:
            action = session.pending.pop(confirm_action_id, None)
            if action is None:
                return self._finish(
                    session,
                    request_id,
                    records,
                    started,
                    "There is no pending action with that id for this session (it may have "
                    "expired or already been used). Nothing was executed.",
                    remember=False,
                )
            # The approval came from the API caller, so this is the one place a
            # state-changing tool runs with confirmed=True.
            result = self.registry.execute(
                action.tool, action.arguments, ToolContext(request_id=request_id, confirmed=True)
            )
            records.append(self._record(result, action.arguments))
            user_content = (
                f"{message}\n\n[Runtime note: the user confirmed pending action "
                f"{action.action_id} ({action.summary}) and the runtime executed it. Result: "
                f"{json.dumps(result.for_llm(), default=str)[:MAX_TOOL_RESULT_CHARS]}]"
            )

        turn_messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        base = [{"role": "system", "content": SYSTEM_PROMPT}, *session.messages]
        specs = self.registry.llm_specs()
        answer: str | None = None
        nudged = False

        try:
            for _ in range(self.max_steps):
                turn = self.llm.complete(base + turn_messages, specs)
                if not turn.tool_calls:
                    missing = self._missing_retraining_evidence(records)
                    if missing and not nudged:
                        nudged = True  # one reminder only; the model cannot be forced
                        turn_messages.append({"role": "assistant", "content": turn.content or ""})
                        turn_messages.append({"role": "user", "content": missing})
                        continue
                    answer = turn.content or ""
                    break
                turn_messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content or "",
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                            }
                            for c in turn.tool_calls
                        ],
                    }
                )
                for call in turn.tool_calls:
                    result = self._dispatch(call, session, request_id)
                    records.append(self._record(result, call.arguments))
                    turn_messages.append(self._tool_message(call.id, result))
                self._fit_budget(turn_messages)
            else:
                turn_messages.append(
                    {
                        "role": "user",
                        "content": "[Runtime note: tool budget exhausted. Answer now using only "
                        "the evidence gathered above and say what remains unchecked.]",
                    }
                )
                answer = self.llm.complete(base + turn_messages, specs, allow_tools=False).content
        except LLMError as exc:
            log_event(logger, "llm_error", request_id=request_id, error=str(exc))
            answer = (
                "The language model could not be reached, so no analysis was produced "
                f"({exc}). The raw tool evidence collected so far is attached to this response."
            )

        return self._finish(session, request_id, records, started, answer or "", user_content)

    def _finish(
        self,
        session: Session,
        request_id: str,
        records: list[ToolCallRecord],
        started: float,
        answer: str,
        user_content: str = "",
        remember: bool = True,
    ) -> ChatResponse:
        failed = [r for r in records if r.status in FAILED_STATUSES]
        pending = [
            PendingActionView(action_id=p.action_id, tool=p.tool, arguments=p.arguments, summary=p.summary)
            for p in session.pending.values()
        ]
        if remember:
            # Only the conversational text is kept between turns; raw tool output is not
            # replayed, which bounds prompt size and forces fresh evidence for new questions.
            session.messages.append({"role": "user", "content": user_content})
            session.messages.append({"role": "assistant", "content": answer})
            del session.messages[:-MAX_HISTORY_MESSAGES]

        # Deterministic notices appended by the runtime, independent of what the LLM wrote.
        if failed:
            lines = [f"- {r.tool}: {r.status} ({r.error})" for r in failed]
            answer += (
                "\n\n---\nTool availability notice (added by the agent runtime):\n"
                + "\n".join(lines)
                + "\nNo data was obtained from the calls above. Statements about those systems "
                "in this answer are not backed by tool evidence."
            )
        if pending:
            lines = [f"- {p.summary} (action id: {p.action_id})" for p in pending]
            answer += "\n\n---\nAwaiting your confirmation (nothing has been executed):\n" + "\n".join(lines)

        log_event(
            logger,
            "request_finished",
            request_id=request_id,
            session_id=session.session_id,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            tool_calls=[r.tool for r in records],
            failed_tools=[r.tool for r in failed],
            pending_actions=len(pending),
        )
        return ChatResponse(
            request_id=request_id,
            session_id=session.session_id,
            answer=answer,
            tool_calls=records,
            failed_tools=sorted({r.tool for r in failed}),
            pending_actions=pending,
        )
