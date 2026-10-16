"""Registered tools for ordinary verification arithmetic and challenge triage.

Two tools, both deterministic and both deliberately narrow:

``solve_simple_arithmetic``
    Computes ordinary arithmetic LOCALLY. No LLM call, so the answer is reproducible and
    a wrong answer is never submitted. When the question is ambiguous it reports that
    instead of guessing -- guessing on a challenge that mutates real state is a side
    effect that cannot be undone.

``classify_verification_challenge``
    Decides WHO should handle a verification prompt. Ordinary arithmetic and ordinary
    forms are handled by the agent; security CAPTCHAs, image challenges, OTPs and
    payment requests are handed to the operator. It never produces a CAPTCHA answer,
    because a security CAPTCHA exists precisely to distinguish humans from automation
    and must not be circumvented.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.tools.verification_tools import (
    IMAGE_CAPTCHA,
    OTP_CHALLENGE,
    PAYMENT_APPROVAL,
    SECURITY_CAPTCHA,
    classify_verification_challenge,
    solve_arithmetic,
)

# Categories that must NEVER be answered by the agent. `unsupported` is deliberately
# NOT in this set: "I don't recognise this" is not the same as "a human must do this",
# and conflating them would park a task for no reason.
_HUMAN_GATED_CATEGORIES = frozenset(
    {SECURITY_CAPTCHA, IMAGE_CAPTCHA, OTP_CHALLENGE, PAYMENT_APPROVAL}
)

# Evidence kind for a locally computed answer. The controller maps the verification
# method "arithmetic_computed" to this kind.
_EVIDENCE = {
    "authoritative": True,
    "source": "local_computation",
    "kind": "arithmetic_computed",
}


class SolveSimpleArithmeticTool(BaseTool):
    """Answer an ordinary arithmetic question with deterministic local computation."""

    name = "solve_simple_arithmetic"
    version = "1.0.0"
    description = (
        "Solve an ordinary arithmetic question (for example '5 + 5 = ?', '12 - 4', '3 x 6') with "
        "deterministic local computation, without calling the model. Use it to fill an ordinary "
        "anti-bot arithmetic field. If the question is ambiguous or is not simple arithmetic it "
        "reports that instead of guessing. It never solves a security CAPTCHA."
    )
    required_permissions = ["web.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 10
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "maxLength": 300,
                "description": "The question text exactly as shown, e.g. 'What is 5 + 5?'",
            },
        },
        "required": ["question"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        question = str(params["question"])

        # Refuse up front if this is really a security challenge wearing arithmetic.
        # Only GENUINELY human-gated categories are escalated: an unrecognised prompt is
        # not a human-intervention situation, it simply is not arithmetic. Escalating it
        # would create a needless operator blocker on every ambiguous question.
        triage = classify_verification_challenge(question)
        if triage["category"] in _HUMAN_GATED_CATEGORIES:
            return ToolResult.needs_user(
                f"This is a {triage['category'].replace('_', ' ')} challenge, not ordinary "
                f"arithmetic, so no answer is produced. {triage['reason']}"
            )

        result = solve_arithmetic(question)
        if not result.answered:
            return ToolResult.failure(
                f"No answer produced: {result.reason}",
                category="AMBIGUOUS_INPUT",
                retryable=False,
                data={
                    "answered": False,
                    "ambiguous": result.ambiguous,
                    "reason": result.reason,
                    "guidance": (
                        "Do not retry with a guess. Re-read the question, or hand this step to "
                        "the operator if it cannot be read reliably."
                    ),
                },
            )

        return ToolResult.success(
            data={
                "answered": True,
                "answer": result.display,
                "value": result.value,
                "operation": result.operation,
                "operands": result.operands,
                "computed_locally": True,
            },
            evidence={**_EVIDENCE, "operation": result.operation, "answer": result.display},
        )


class ClassifyVerificationChallengeTool(BaseTool):
    """Decide whether a verification prompt is automatable or human-gated."""

    name = "classify_verification_challenge"
    version = "1.0.0"
    description = (
        "Classify a verification prompt (CAPTCHA, arithmetic, OTP, payment request, ordinary "
        "form, knowledge question) and report whether the agent may handle it or a human must. "
        "Use it BEFORE attempting any verification step, so a security CAPTCHA is never solved "
        "and an ordinary question is never needlessly handed to the operator."
    )
    required_permissions = ["web.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 10
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "prompt_text": {
                "type": "string",
                "maxLength": 2000,
                "description": "The verification prompt text as displayed",
            },
        },
        "required": ["prompt_text"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        prompt_text = str(params.get("prompt_text") or "").strip()
        if not prompt_text:
            # Nothing was read yet. This is the agent's ordering mistake, not a challenge
            # for the operator, so fail retryably instead of parking the task.
            return ToolResult.failure(
                "No prompt text was supplied. Read the bot's latest message first, then "
                "pass its text to this tool.",
                category="MISSING_INPUT",
                retryable=True,
            )
        triage = classify_verification_challenge(prompt_text)
        data: Dict[str, Any] = {
            "category": triage["category"],
            "automatic": triage["automatic"],
            "requires_human": triage["requires_human"],
            "reason": triage["reason"],
        }
        # Only ordinary arithmetic yields an answer here. A security CAPTCHA never does.
        if triage.get("answer"):
            data["answer"] = triage["answer"]
            data["computed_locally"] = True

        if triage["requires_human"]:
            return ToolResult.needs_user(
                f"{triage['category'].replace('_', ' ').capitalize()} requires the operator. "
                f"{triage['reason']}",
                data=data,
            )
        return ToolResult.success(data=data)


VERIFICATION_TOOLS = (SolveSimpleArithmeticTool, ClassifyVerificationChallengeTool)
