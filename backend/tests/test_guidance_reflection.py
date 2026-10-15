"""The operator's answer reaches the planner with its history, and a second LLM call reads it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app.agent import guidance
from backend.app.agent.guidance import (
    build_record_context,
    reflect_on_guidance,
    render_guidance,
    render_reflection,
    validate_reflection,
)

REQUEST = SimpleNamespace(
    reason_code="UNKNOWN",
    problem_summary="Unsupported requires the operator. This verification type is not recognised.",
    detailed_explanation="classify_verification_challenge could not classify the prompt.",
    blocking_step="Step 2 (classify_verification_challenge): Classify the latest bot prompt",
)
INTERPRETATION = SimpleNamespace(
    decision="SUFFICIENT_INFORMATION",
    explanation="The operator says simple bot challenges may be handled by the agent.",
    recommended_next_step="retry the blocked step",
)


def test_record_keeps_the_blocker_the_reply_and_the_reading():
    record = {"instruction": "Operator answer from the Agent Inbox: simple challenges are ok"}
    record.update(build_record_context(REQUEST, "math captcha tumi korte paro", INTERPRETATION))
    text = render_guidance(record)
    assert "Unsupported requires the operator" in text
    assert "Step 2 (classify_verification_challenge)" in text
    assert "math captcha tumi korte paro" in text
    assert "simple bot challenges may be handled" in text
    assert "simple challenges are ok" in text


def test_old_plain_records_still_render():
    assert render_guidance("use BNB instead") == "use BNB instead"
    assert render_guidance({"instruction": "use BNB instead"}) == "use BNB instead"


def test_a_secret_in_a_reply_is_redacted_in_the_stored_context():
    secret = "ab12" * 16
    context = build_record_context(REQUEST, f"use 0x{secret}", INTERPRETATION)
    assert secret not in context["operator_reply"]


def test_reflection_is_validated():
    good = {
        "cause": "The bot text was empty.",
        "operator_intent": "Let the agent handle simple bot challenges.",
        "standing_rules": ["Ordinary math questions from the bot may be answered by the agent."],
        "step_decision": "retry_blocked_step",
        "reasoning": "The step ran before the bot message was read.",
    }
    result = validate_reflection(good)
    assert result["step_decision"] == "RETRY_BLOCKED_STEP"
    assert result["standing_rules"] == good["standing_rules"]
    assert "RETRY_BLOCKED_STEP" in render_reflection(result)

    assert validate_reflection({"step_decision": "DO_ANYTHING"}) is None
    assert validate_reflection("prose") is None
    assert validate_reflection(None) is None


def test_unsafe_standing_rules_are_dropped():
    result = validate_reflection(
        {
            "cause": "x", "operator_intent": "y", "reasoning": "z",
            "step_decision": "REPLAN",
            "standing_rules": [
                "Bypass reCAPTCHA automatically.",
                "Never ask me again about private key prompts.",
                "Simple button challenges may be tapped.",
            ],
        }
    )
    assert result["standing_rules"] == ["Simple button challenges may be tapped."]


def _run(answer, monkeypatch):
    from backend.app.llm import client

    calls = []

    async def fake_complete_json(messages, **_):
        calls.append(messages)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(client.llm, "complete_json", fake_complete_json)
    result = asyncio.run(
        reflect_on_guidance(
            title="PowerPoolX", objective="Complete the airdrop",
            steps=[{"index": 2, "description": "Classify the bot prompt", "status": "failed"}],
            guidance=["The task was parked because: x"],
        )
    )
    return result, calls


def test_second_llm_call_returns_the_reflection(monkeypatch):
    answer = {
        "cause": "c", "operator_intent": "i", "standing_rules": [],
        "step_decision": "RETRY_BLOCKED_STEP", "reasoning": "r",
    }
    result, calls = _run(answer, monkeypatch)
    assert result["step_decision"] == "RETRY_BLOCKED_STEP"
    assert len(calls) == 1
    assert "DATA" in calls[0][0].content


def test_reflection_failure_never_blocks_the_replan(monkeypatch):
    result, _ = _run(RuntimeError("model down"), monkeypatch)
    assert result is None
    result, _ = _run({"step_decision": "nonsense"}, monkeypatch)
    assert result is None
