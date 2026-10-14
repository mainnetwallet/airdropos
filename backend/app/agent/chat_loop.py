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


def _render_tool_result(record: ToolCallRecord) -> str:
    """What the model sees next: the real result, or the real failure."""
    if record.status == "success":
        return f"TOOL {record.tool} -> SUCCESS\n{json.dumps(record.data, default=str)}"
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
        messages.append(Message("user", message))

        # Appended so the final answer is produced with the tool results in context.
        transcript: List[str] = []
        spec: Optional[Dict[str, Any]] = None
        usage: Dict[str, Any] = {}

        repaired = False  # one re-ask is allowed when the model breaks the JSON contract
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
                if not repaired:
                    # Ask once for the contract again before giving up. Nothing was
                    # run, so this cannot repeat a side effect.
                    repaired = True
                    messages.append(Message("assistant", response.content or ""))
                    messages.append(
                        Message(
                            "user",
                            "Your last message was not a single valid JSON object. Reply again "
                            "with ONLY the JSON object described in the system prompt (keys: "
                            "reply, tool_calls, create_task). No text before or after it, and "
                            "no code fences.",
                        )
                    )
                    continue
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