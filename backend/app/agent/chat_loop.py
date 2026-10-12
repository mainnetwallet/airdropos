"""Natural-language control loop for the AI Agent Console.

This is the missing link between "the operator typed a sentence" and "the application
actually did something". Before it existed, `POST /agent/chat` asked the fixed model
for a JSON object containing a reply and an optional task spec, and that was the end
of it: no tool was ever invoked, so "show my wallet address" produced prose rather
than an address, and "transfer 0.01 ETH" produced advice rather than a quote.

The loop is deliberately small and entirely deterministic where it matters:

    message -> intent -> context -> tool selection -> authorization -> execution
            -> evidence -> verification -> progress -> report

* The model may only name tools that the authorization policy has already put in the
  catalog it is shown. A tool it invents is dropped before anything runs.
* Every invocation goes through `ToolRegistry.invoke`, which re-validates the schema,
  re-checks permissions, the network/wallet allowlists and the emergency stop. The
  model cannot route around any of it by constructing the call itself.
* Results are fed back so the model can chain a second read (for example balance
  after address) or stop. The turn is bounded by iteration, tool-call and time limits.
* Value-moving tools stay behind their own approval: the loop records that approval is
  required and returns the prepared proposal. It never approves on the operator's
  behalf and never invents an approval id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.prompts import build_chat_system_prompt
from backend.app.llm.client import LLMError, Message, llm
from backend.app.tools.base import ToolResultStatus
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry

logger = logging.getLogger(__name__)

# Bounds. One operator message must not become an unbounded agent run.
MAX_TOOL_ROUNDS = 6
MAX_TOOL_CALLS_PER_TURN = 12
TURN_TIME_BUDGET_SECONDS = 150.0
MAX_TOOL_RESULT_CHARS = 6000

# Side effects that may never be taken on a single ambiguous sentence. The model is
# asked for an approval request instead; the deterministic policy is what enforces it.
IRREVERSIBLE_SIDE_EFFECTS = {"wallet_signing", "transaction_submission"}


@dataclass
class ToolCallRecord:
    """One executed tool call, as reported to the operator."""

    tool: str
    status: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_category: Optional[str] = None
    retryable: bool = False
    duration_ms: int = 0
    evidence: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "tool": self.tool,
            "status": self.status,
            "arguments": self.arguments,
            "data": self.data,
            "duration_ms": self.duration_ms,
        }
        if self.error:
            payload["error"] = self.error
            payload["error_category"] = self.error_category
            payload["retryable"] = self.retryable
        if self.evidence:
            payload["evidence"] = self.evidence
        return payload


@dataclass
class ChatTurnResult:
    """Everything the frontend needs to render an honest answer."""

    reply: str
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    task: Optional[Dict[str, Any]] = None
    approvals_required: List[Dict[str, Any]] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    needs_user_action: List[str] = field(default_factory=list)
    verified: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    model: Optional[str] = None
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reply": self.reply,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "task": self.task,
            "approvals_required": self.approvals_required,
            "blockers": self.blockers,
            "needs_user_action": self.needs_user_action,
            "verified": self.verified,
            "usage": self.usage,
            "model": self.model,
            "truncated": self.truncated,
        }


def _clip(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def _render_tool_result(record: ToolCallRecord) -> str:
    """What the model sees next: the real result, or the real failure."""
    if record.status == "success":
        return f"TOOL {record.tool} -> SUCCESS\n{_clip(json.dumps(record.data, default=str))}"
    if record.status == "needs_user_action":
        return (
            f"TOOL {record.tool} -> NEEDS_USER_ACTION\n"
            f"{record.error or 'The operator must do something before this can continue.'}"
        )
    if record.status == "not_authorized":
        return (
            f"TOOL {record.tool} -> REFUSED (authorization)\n"
            f"{record.error or 'Not permitted by the current policy.'}\n"
            f"Tell the operator exactly which capability is missing; do not retry or work around it."
        )
    if record.status == "unavailable":
        return (
            f"TOOL {record.tool} -> UNAVAILABLE\n{record.error or 'The dependency is down.'}\n"
            f"Do not retry this tool in a loop."
        )
    return (
        f"TOOL {record.tool} -> FAILED ({record.error_category or 'TOOL_FAILURE'})\n"
        f"{record.error or 'No detail supplied.'}\n"
        f"retryable={record.retryable}"
    )


def _parse_tool_calls(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Read the model's tool requests defensively.

    The model is told to emit `tool_calls: [{"tool": ..., "arguments": {...}}]`. Anything
    malformed is discarded here rather than half-executed: a tool call with no name, or
    with arguments that are not an object, cannot be validated, and an unvalidated call
    must never reach the registry.
    """
    raw = parsed.get("tool_calls")
    if not isinstance(raw, list):
        return []
    calls: List[Dict[str, Any]] = []
    for entry in raw[:MAX_TOOL_CALLS_PER_TURN]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("tool") or entry.get("name") or "").strip()
        if not name:
            continue
        arguments = entry.get("arguments")
        if arguments is None:
            arguments = entry.get("parameters")
        calls.append({"tool": name, "arguments": arguments if isinstance(arguments, dict) else {}})
    return calls


# ---------------------------------------------------------------------------
# Dynamic task gating
#
# Not every chat message is a job. A task is created only when (a) the operator
# explicitly asks for one, or (b) the work cannot be finished inside this turn.
# The model makes the call (b); this module only reads the operator's own words for
# the explicit cases so the model is told, and the loop can enforce, the one thing
# that must never be guessed: "yes, create a task" and "do NOT create a task".
# Covers English, romanized Bangla (Banglish) and Bangla script.
# ---------------------------------------------------------------------------
_TASK_WORD = re.compile(r"(task|টাস্ক|\btaask\b|\btasks\b)", re.IGNORECASE)
_CREATE_WORD = re.compile(
    r"(create|make|add|queue|new|schedule|banao|banaw|bana\b|koro\b|kor\b|kore\b|dao\b|de\b|"
    r"ekta|তৈরি|বানাও|বানা|করো|কর\b|যোগ|দাও)",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"(don'?t|do not|dont|no need|without (a )?task|never|stop|cancel|"
    r"koro ?na|kor ?na|korona|korbe ?na|korba ?na|kore ?na|korio ?na|"
    r"lagbe ?na|lagena|dorkar ?nai|dorkar ?nei|proyojon ?nai|"
    r"banao ?na|dio ?na|dis ?na|"
    r"(করো|কর|কোরো|বানাও|বানিও|দাও|দিও|করবে|করিস|বানাবে) ?না|লাগবে ?না|দরকার ?নেই)",
    re.IGNORECASE,
)
_AFFIRM = re.compile(
    r"^\s*(yes|yep|yeah|y|ok|okay|sure|do it|go ahead|please do|haan|han|ha|hya|hyan|hum|"
    r"koro|kor|kore dao|kore felo|korey? dao|thik ache|hoye jak|হ্যাঁ|হা|হ্যা|করো|কর|ঠিক আছে|ঠিকাছে)"
    r"[\s.!]*$",
    re.IGNORECASE,
)


def operator_task_signal(message: str, history: List[Any]) -> str:
    """Read the operator's explicit intent about tasks: 'yes', 'no' or 'none'.

    'no' beats 'yes'. A bare "yes / koro / do it" only counts when the agent's
    previous turn offered to create a task.
    """
    text = (message or "").strip()
    if _TASK_WORD.search(text) and _NEGATION.search(text):
        return "no"
    if _TASK_WORD.search(text) and _CREATE_WORD.search(text):
        return "yes"
    if _AFFIRM.match(text) and history:
        last = history[-1]
        if getattr(last, "role", "") == "assistant":
            body = str(getattr(last, "content", ""))[-600:]
            if _TASK_WORD.search(body) and "?" in body:
                return "yes"
    return "none"


def _task_signal_note(signal: str) -> str:
    if signal == "yes":
        return (
            "[OPERATOR TASK SIGNAL: EXPLICIT. The operator asked for a task in this "
            "message. Set create_task with a self-contained description.]"
        )
    if signal == "no":
        return (
            "[OPERATOR TASK SIGNAL: FORBIDDEN. The operator said not to create a task. "
            "create_task must be null.]"
        )
    return (
        "[OPERATOR TASK SIGNAL: NONE. The operator did not mention a task. Set "
        "create_task only if finishing the request truly needs background work that "
        "this turn's tools cannot do; otherwise null and just answer.]"
    )


class ChatToolLoop:
    """Runs one operator turn: decide, execute, verify, report."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: Optional[AuthorizationPolicy] = None,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self.registry = registry
        self.policy = policy if policy is not None else registry.policy
        self.max_rounds = max_rounds

    async def run(
        self,
        *,
        db: AsyncSession,
        message: str,
        history: List[Any],
        create_task: Optional[Any] = None,
    ) -> ChatTurnResult:
        """Execute one turn. `create_task` persists a task the model asked for."""
        started = time.perf_counter()
        result = ChatTurnResult(reply="")

        catalog = self.registry.catalog_for_llm(self.policy)
        system = build_chat_system_prompt(catalog)

        messages: List[Message] = [Message("system", system)]
        for turn in history:
            messages.append(Message(turn.role, turn.content))
        task_signal = operator_task_signal(message, history)
        messages.append(Message("user", f"{message}\n\n{_task_signal_note(task_signal)}"))

        # Appended so the final answer is produced with the tool results in context.
        transcript: List[str] = []
        spec: Optional[Dict[str, Any]] = None
        usage: Dict[str, Any] = {}

        for round_index in range(self.max_rounds):
            if time.perf_counter() - started > TURN_TIME_BUDGET_SECONDS:
                result.truncated = True
                result.blockers.append(
                    "The turn hit its time budget before finishing. Nothing further was run."
                )
                break

            try:
                response = await llm.complete(messages, json_mode=True)
            except LLMError as exc:
                result.blockers.append(
                    f"The configured model is unavailable ({exc.category}): {exc}"
                )
                break

            usage = response.usage or {}
            result.model = response.model

            try:
                parsed = llm.parse_json(response.content)
            except LLMError:
                # The model ignored the JSON contract. Its text is still shown, but no
                # tool is run and no task is created from prose.
                result.reply = response.content or ""
                result.blockers.append(
                    "The model did not return the expected structured response, so no action "
                    "was taken."
                )
                break

            reply = str(parsed.get("reply") or "").strip()
            requested = _parse_tool_calls(parsed)

            if isinstance(parsed.get("create_task"), dict):
                spec = parsed["create_task"]

            if not requested:
                result.reply = reply
                break

            # A model that asks for both tools and a task in one round gets the tools
            # first; the task description is created afterwards and can reference them.
            transcript.append(f"ROUND {round_index + 1} - the agent decided to run tools.")
            exhausted = False
            for call in requested:
                if len(result.tool_calls) >= MAX_TOOL_CALLS_PER_TURN:
                    result.truncated = True
                    result.blockers.append(
                        f"Reached the {MAX_TOOL_CALLS_PER_TURN}-tool limit for one message."
                    )
                    exhausted = True
                    break
                if time.perf_counter() - started > TURN_TIME_BUDGET_SECONDS:
                    result.truncated = True
                    result.blockers.append(
                        "The turn hit its time budget; remaining calls were skipped."
                    )
                    exhausted = True
                    break

                record = await self._invoke(call)
                result.tool_calls.append(record)
                transcript.append(_render_tool_result(record))

            if exhausted:
                result.reply = reply or self._fallback_reply(result)
                break

            messages.append(Message("assistant", response.content))
            messages.append(
                Message(
                    "user",
                    "Here are the real results of the tools you asked for. Continue with what "
                    "you can now prove, and call a tool again only if you still need data you "
                    "do not have.\n\n" + "\n\n".join(transcript),
                )
            )

        if not result.reply:
            result.reply = self._fallback_reply(result)

        if spec:
            allowed, why = self._task_allowed(task_signal, result)
            if not allowed:
                logger.info("Chat task suppressed: %s", why)
                result.blockers.append(f"No task was created: {why}")
                spec = None

        if spec and create_task is not None:
            task_info, task_error = await create_task(spec)
            if task_error:
                result.blockers.append(task_error)
            else:
                result.task = task_info

        # Surface anything value-moving that still needs the operator.
        for record in result.tool_calls:
            proposal = (record.data or {}).get("proposal")
            if record.status == "success" and isinstance(proposal, dict):
                result.approvals_required.append(
                    {
                        "proposal_id": proposal.get("proposal_id"),
                        "kind": proposal.get("kind"),
                        "network": proposal.get("network"),
                        "chain_id": proposal.get("chain_id"),
                        "wallet": proposal.get("wallet_address"),
                        "to": proposal.get("to_address"),
                        "asset": proposal.get("asset"),
                        "amount": proposal.get("amount"),
                        "estimated_fee_native": proposal.get("estimated_fee_native"),
                        "warnings": proposal.get("warnings") or [],
                        "approval_fields": proposal.get("approval_fields"),
                        "state": "AWAITING_OPERATOR_APPROVAL",
                    }
                )
            if record.status == "needs_user_action" and record.error:
                result.needs_user_action.append(record.error)
            if record.status in ("failure", "not_authorized", "unavailable") and record.error:
                result.blockers.append(f"{record.tool}: {record.error}")
            if record.status == "success" and record.evidence:
                result.verified.append(
                    {
                        "tool": record.tool,
                        "evidence": record.evidence,
                    }
                )

        if not result.reply:
            result.reply = self._fallback_reply(result)
        return result

    # ------------------------------------------------------------------
    async def _invoke(self, call: Dict[str, Any]) -> ToolCallRecord:
        name = call["tool"]
        arguments = call.get("arguments") or {}

        tool = self.registry.get(name)
        if tool is None:
            return ToolCallRecord(
                tool=name,
                status="failure",
                error=f"No tool named '{name}' is registered.",
                error_category="UNSUPPORTED_OPERATION",
            )
        if name in self.registry._disabled:
            return ToolCallRecord(
                tool=name,
                status="unavailable",
                error=f"Tool '{name}' is unavailable: {self.registry._disabled[name]}",
                error_category="TOOL_UNAVAILABLE",
            )
        permitted, reason = self.policy.permits(tool)
        if not permitted:
            return ToolCallRecord(
                tool=name,
                status="not_authorized",
                arguments=_redact_arguments(arguments),
                error=reason,
                error_category="MISSING_PERMISSION",
            )

        try:
            outcome = await self.registry.invoke(name, arguments, policy=self.policy)
        except Exception as exc:  # registry.invoke is already defensive; belt and braces
            logger.exception("Chat tool loop failed invoking %s", name)
            return ToolCallRecord(
                tool=name,
                status="failure",
                arguments=_redact_arguments(arguments),
                error=f"{type(exc).__name__}: {exc}",
                error_category="TOOL_FAILURE",
            )

        return ToolCallRecord(
            tool=name,
            status=outcome.status.value,
            arguments=_redact_arguments(arguments),
            data=outcome.data or {},
            error=outcome.error,
            error_category=outcome.error_category,
            retryable=bool(outcome.retryable),
            duration_ms=outcome.duration_ms,
            evidence=outcome.evidence,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _task_allowed(signal: str, result: "ChatTurnResult") -> tuple[bool, str]:
        """Enforce the task policy the prompt describes.

        The operator's explicit word is final in both directions. Without one, a task
        is only legitimate when the turn's own tools did not already answer the
        request: a finished tool answer is never wrapped in a background job.
        """
        if signal == "no":
            return False, "the operator said not to create a task."
        if signal == "yes":
            return True, ""
        if any(c.status == "success" for c in result.tool_calls):
            return False, "the request was already answered with tools, so no background work is needed."
        return True, ""

    # ------------------------------------------------------------------
    @staticmethod
    def _fallback_reply(result: ChatTurnResult) -> str:
        """Honest text when the model produced no usable answer.

        Reports what actually happened rather than an apology, and never implies that
        an operation succeeded.
        """
        lines: List[str] = []
        succeeded = [c for c in result.tool_calls if c.status == "success"]
        failed = [c for c in result.tool_calls if c.status not in ("success",)]
        pending = [c for c in result.tool_calls if c.status == "needs_user_action"]

        if succeeded:
            lines.append(f"Ran {len(succeeded)} tool(s): " + ", ".join(c.tool for c in succeeded) + ".")
        if pending:
            lines.append("I need something from you before continuing: " + " ".join(pending[0].error.split(". ")[:1]))
        if failed:
            lines.append(
                f"{len(failed)} call(s) did not complete: "
                + "; ".join(f"{c.tool}: {c.error}" for c in failed[:3])
            )
        if not lines:
            lines.append(
                "I did not run any action for that message, so nothing has changed."
            )
        return " ".join(lines)


_SENSITIVE_KEYS = ("secret", "private_key", "mnemonic", "seed", "password", "token")


def _redact_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Never echo a secret back into the transcript, even if a tool name matches one."""
    clean: Dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if any(marker in str(key).lower() for marker in _SENSITIVE_KEYS):
            clean[key] = "[REDACTED]"
        elif isinstance(value, str) and len(value) > 512:
            clean[key] = value[:512] + "..."
        else:
            clean[key] = value
    return clean


__all__ = [
    "ChatToolLoop",
    "ChatTurnResult",
    "ToolCallRecord",
    "MAX_TOOL_CALLS_PER_TURN",
    "MAX_TOOL_ROUNDS",
    "ToolResultStatus",
]