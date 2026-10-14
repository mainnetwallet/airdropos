"""Regression tests for ordinary arithmetic and challenge classification (Phase 5 / 10: Test F).

Deterministic by design: no LLM, no network, no guessing. The ambiguity tests matter as
much as the correct answers -- submitting a guess to a form that mutates real state is
an irreversible side effect.
"""

from __future__ import annotations

import pytest

from backend.app.tools.verification_tools import (
    AUTOMATICALLY_SOLVABLE,
    SECURITY_CAPTCHA,
    classify_verification_challenge,
    solve_arithmetic,
)


# ======================================================================
# TEST F - Ordinary arithmetic
# ======================================================================
@pytest.mark.parametrize(
    "question,expected",
    [
        ("5 + 5 = ?", "10"),
        ("5 + 5", "10"),
        ("12 - 4", "8"),
        ("12 - 4 = ?", "8"),
        ("3 × 6 = ?", "18"),
        ("3 x 6", "18"),
        ("3 * 6", "18"),
        ("10 / 2 = ?", "5"),
        ("10 ÷ 2", "5"),
        ("7 + 8 = ?", "15"),
        ("20 - 35", "-15"),
        ("0 + 0", "0"),
        ("what is 4 + 9?", "13"),
        ("What is five plus three?", "8"),
        ("2.5 + 2.5", "5"),
    ],
)
def test_simple_arithmetic_is_solved_locally(question, expected):
    """The three worked examples from the spec must produce exact answers."""
    result = solve_arithmetic(question)
    assert result.answered is True, f"{question!r} not answered: {result.reason}"
    assert result.display == expected


def test_division_by_zero_is_not_answered():
    """Division by zero has no answer; the agent must not invent one."""
    result = solve_arithmetic("8 / 0 = ?")
    assert result.answered is False
    assert result.ambiguous is True
    assert "zero" in result.reason.lower()


@pytest.mark.parametrize(
    "question",
    [
        "",                              # empty
        "Which wallet did you use?",      # not arithmetic
        "2 + 3 * 4 = ?",                 # multiple operations
        "What is the capital of France?",
        "Select all squares with cars",
        "answer: 42",                    # no operator
    ],
)
def test_ambiguous_or_non_arithmetic_questions_are_not_guessed(question):
    """No answer is produced when the question is unclear or is not arithmetic."""
    result = solve_arithmetic(question)
    assert result.answered is False, f"{question!r} was wrongly answered"
    assert result.ambiguous is True
    assert result.display == ""


def test_answer_is_deterministic():
    answers = {solve_arithmetic("5 + 5 = ?").display for _ in range(50)}
    assert answers == {"10"}


def test_answer_is_rendered_sensibly():
    assert solve_arithmetic("10 / 4 = ?").display == "2.5"
    assert solve_arithmetic("10 / 5 = ?").display == "2"


# ======================================================================
# Challenge classification (Phase 5.1)
# ======================================================================
@pytest.mark.parametrize(
    "prompt,expected_category",
    [
        ("5 + 5 = ?", "arithmetic"),
        ("What is 12 - 4?", "arithmetic"),
        ("Prove you are human - select all squares with traffic lights", SECURITY_CAPTCHA),
        ("I'm not a robot", SECURITY_CAPTCHA),
        ("reCAPTCHA challenge", SECURITY_CAPTCHA),
        ("Click each image with a bicycle", SECURITY_CAPTCHA),
        ("Enter the verification code we sent to your phone", "otp_challenge"),
        ("Confirm payment with card number and CVV", "payment_approval"),
        ("Which of the following is the correct answer?", "knowledge_question"),
        ("!!!something entirely unknown!!!", "unsupported"),
    ],
)
def test_challenges_are_classified(prompt, expected_category):
    """Verification prompts are not all the same kind of challenge."""
    assert classify_verification_challenge(prompt)["category"] == expected_category


def test_security_captcha_is_never_automatically_solvable():
    """No path may produce an answer for a security CAPTCHA."""
    for prompt in (
        "Prove you are human",
        "reCAPTCHA",
        "select all squares with crosswalks",
        "press and hold to verify",
    ):
        result = classify_verification_challenge(prompt)
        assert result["category"] == SECURITY_CAPTCHA
        assert result["automatic"] is False
        assert result["requires_human"] is True
        # The critical invariant: no answer value is ever produced.
        assert "answer" not in result


def test_ordinary_arithmetic_is_automatically_solvable():
    result = classify_verification_challenge("5 + 5 = ?")
    assert result["category"] == "arithmetic"
    assert result["automatic"] is True
    assert result["requires_human"] is False
    assert result["answer"] == "10"


def test_payment_requests_require_a_human():
    result = classify_verification_challenge("Confirm transaction and pay now")
    assert result["category"] == "payment_approval"
    assert result["requires_human"] is True
    assert result["automatic"] is False


def test_unrecognized_challenges_default_to_needing_a_human():
    """Failing safe: an unknown challenge is escalated, never attempted."""
    result = classify_verification_challenge("glorb the flooben")
    assert result["category"] == "unsupported"
    assert result["requires_human"] is True


def test_empty_prompt_is_reported_not_assumed():
    result = classify_verification_challenge("")
    assert result["requires_human"] is True
    assert result["category"] == "unsupported"


def test_automatic_categories_are_a_deliberately_small_set():
    assert AUTOMATICALLY_SOLVABLE == {"arithmetic", "ordinary_form", "knowledge_question"}


# ======================================================================
# The registered tools
# ======================================================================
async def test_arithmetic_tool_returns_computed_evidence():
    from backend.app.tools.arithmetic_tools import SolveSimpleArithmeticTool

    result = await SolveSimpleArithmeticTool().execute({"question": "5 + 5 = ?"})

    assert result.status.value == "success"
    assert result.data["answer"] == "10"
    assert result.data["computed_locally"] is True
    assert result.evidence["kind"] == "arithmetic_computed"
    assert result.evidence["authoritative"] is True
    assert result.evidence["source"] == "local_computation"


async def test_arithmetic_tool_refuses_a_security_captcha():
    """A CAPTCHA wearing an arithmetic prompt must not be answered."""
    from backend.app.tools.arithmetic_tools import SolveSimpleArithmeticTool

    result = await SolveSimpleArithmeticTool().execute(
        {"question": "Select all squares with 5 + 5 traffic lights"}
    )
    assert result.status.value == "needs_user_action"
    assert result.data.get("answer") is None


async def test_arithmetic_tool_does_not_guess_an_ambiguous_question():
    from backend.app.tools.arithmetic_tools import SolveSimpleArithmeticTool

    result = await SolveSimpleArithmeticTool().execute({"question": "Which wallet?"})

    assert result.status.value == "failure"
    assert result.error_category == "AMBIGUOUS_INPUT"
    assert result.retryable is False
    assert "guess" in (result.data.get("guidance") or "").lower()


async def test_classify_tool_escalates_security_challenges():
    from backend.app.tools.arithmetic_tools import ClassifyVerificationChallengeTool

    result = await ClassifyVerificationChallengeTool().execute(
        {"prompt_text": "Prove you are human"}
    )

    assert result.status.value == "needs_user_action"
    assert result.data["requires_human"] is True
    assert "answer" not in result.data


async def test_classify_tool_allows_ordinary_questions():
    from backend.app.tools.arithmetic_tools import ClassifyVerificationChallengeTool

    result = await ClassifyVerificationChallengeTool().execute({"prompt_text": "3 x 6 = ?"})

    assert result.status.value == "success"
    assert result.data["answer"] == "18"
    assert result.data["requires_human"] is False
