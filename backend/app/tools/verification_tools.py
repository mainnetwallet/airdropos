"""Deterministic verification helpers: ordinary arithmetic and challenge classification.
Why a separate module:

* Ordinary arithmetic must be computed LOCALLY and deterministically. Asking the LLM
  "what is 3 x 6" wastes a call and is a guess, and a wrong answer submitted to a
  campaign is a real-world side effect that cannot be undone. A parser gives the same
  answer every time and can say "ambiguous" honestly.

* CAPTCHA classification must be deterministic too. Treating every verification prompt
  as the same kind of challenge is what made the old behaviour either attempt to solve a
  security CAPTCHA (wrong) or park an ordinary form (needless). Classifying first means
  ordinary questions are answered automatically and only genuinely human-gated
  challenges are handed to the operator.

Nothing here bypasses a security control. See `classify_verification_challenge`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ======================================================================
# Ordinary arithmetic
# ======================================================================
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000,
}

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
# "5 + 5 = ?", "12 - 4 =", "3 × 6 ?", "10 / 2 = ?"
_ARITH_RE = re.compile(
    r"(?P<a>-?\d+(?:\.\d+)?)\s*(?P<op>[+\-*/x×÷])\s*(?P<b>-?\d+(?:\.\d+)?)"
)
# "what is five plus three?"
_NUM = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand"
)
_WORD_ARITH_RE = re.compile(
    rf"\b(?P<a>{_NUM})\s+(?P<op>plus|add|minus|subtract|times|multiplied\s+by|x|divided\s+by|over)\s+(?P<b>{_NUM})\b",
    re.I,
)

# Any operator character left over after the first match. Used to detect a multi-step
# expression (e.g. "2 + 3 * 4") that must NOT be answered by taking the first pair.
_RESIDUAL_OPERATOR_RE = re.compile(r"[+\-*/×÷]")

_OP_ALIASES = {
    "+": "add", "plus": "add", "add": "add", "sum": "add",
    "-": "subtract", "minus": "subtract", "subtract": "subtract", "less": "subtract",
    "*": "multiply", "x": "multiply", "×": "multiply", "times": "multiply",
    "multiply": "multiply", "multiplied": "multiply", "product": "multiply",
    "/": "divide", "÷": "divide", "divided": "divide", "divide": "divide",
    "over": "divide",
}

_WORD_OPS = (
    "plus", "minus", "times", "multiplied", "divided", "add", "subtract",
    "multiply", "divide", "over",
)


@dataclass
class ArithmeticResult:
    """Outcome of parsing an ordinary arithmetic question.

    ``ambiguous`` is a first-class result. Guessing when the question is unclear is
    exactly what the specification forbids, so an unparseable or multi-operator
    expression is reported as ambiguous rather than answered.
    """

    answered: bool
    value: Optional[float] = None
    operation: Optional[str] = None
    operands: List[float] = field(default_factory=list)
    ambiguous: bool = False
    reason: str = ""

    @property
    def display(self) -> str:
        """Render the answer the way a form expects it."""
        if not self.answered or self.value is None:
            return ""
        if float(self.value).is_integer():
            return str(int(self.value))
        return f"{self.value:g}"


def _word_to_number(word: str) -> Optional[float]:
    return _NUMBER_WORDS.get(str(word).strip().lower())


def _apply(operation: str, a: float, b: float) -> Optional[float]:
    if operation == "add":
        return a + b
    if operation == "subtract":
        return a - b
    if operation == "multiply":
        return a * b
    if operation == "divide":
        if b == 0:
            return None  # Division by zero has no answer; do not invent one.
        return a / b
    return None


def solve_arithmetic(question: str) -> ArithmeticResult:
    """Solve a simple, unambiguous arithmetic question locally.

    Handles ``5 + 5 = ?``, ``12 - 4``, ``3 × 6``, ``10 / 2`` and word forms such as
    ``what is five plus three?``. Returns ``answered=False`` with ``ambiguous=True``
    for anything unclear, rather than guessing.
    """
    text = (question or "").strip()
    if not text:
        return ArithmeticResult(False, ambiguous=True, reason="The question is empty.")

    # Digit form first: it is unambiguous.
    match = _ARITH_RE.search(text)
    if match:
        # A second operator means this is more than one operation (e.g. "2 + 3 * 4").
        # Deliberately unanswered: operator precedence on a challenge prompt is not
        # something to assume. Testing the WHOLE remainder matters -- "* 4" alone does
        # not re-match `_ARITH_RE` because it has no left-hand number, so a naive
        # second search silently answered "2 + 3 * 4" as 5.
        remainder = text[: match.start()] + text[match.end():]
        if _ARITH_RE.search(remainder) or _RESIDUAL_OPERATOR_RE.search(remainder):
            return ArithmeticResult(
                False, ambiguous=True,
                reason="The question contains more than one operation, so no single answer "
                       "is guessed.",
            )
        operation = _OP_ALIASES.get(match.group("op").lower())
        if operation is None:
            return ArithmeticResult(
                False, ambiguous=True, reason=f"Unsupported operator {match.group('op')!r}."
            )
        a, b = float(match.group("a")), float(match.group("b"))
        value = _apply(operation, a, b)
        if value is None:
            return ArithmeticResult(False, ambiguous=True, reason="Division by zero has no answer.")
        return ArithmeticResult(True, value=value, operation=operation, operands=[a, b])

    # Word form: "what is five plus three?"
    word_match = _WORD_ARITH_RE.search(text)
    if word_match:
        a = _word_to_number(word_match.group("a"))
        b = _word_to_number(word_match.group("b"))
        operation = _OP_ALIASES.get(re.sub(r"\s+", " ", word_match.group("op").lower()))
        if a is None or b is None or operation is None:
            return ArithmeticResult(
                False, ambiguous=True, reason="Unrecognised word number or operator."
            )
        value = _apply(operation, float(a), float(b))
        if value is None:
            return ArithmeticResult(False, ambiguous=True, reason="Division by zero has no answer.")
        return ArithmeticResult(
            True, value=value, operation=operation, operands=[float(a), float(b)]
        )

    lowered = text.lower()
    if _NUMBER_RE.search(text) and any(w in lowered for w in _WORD_OPS):
        return ArithmeticResult(
            False, ambiguous=True,
            reason="The numbers and operator could not be paired unambiguously.",
        )

    return ArithmeticResult(
        False, ambiguous=True,
        reason="This is not an ordinary arithmetic question, so no answer is produced.",
    )

# ======================================================================
# Verification-challenge classification
# ======================================================================
ARITHMETIC = "arithmetic"
ORDINARY_FORM = "ordinary_form"
KNOWLEDGE_QUESTION = "knowledge_question"
OTP_CHALLENGE = "otp_challenge"
PAYMENT_APPROVAL = "payment_approval"
SECURITY_CAPTCHA = "security_captcha"
IMAGE_CAPTCHA = "image_captcha"
UNSUPPORTED = "unsupported"

# Which categories the agent may complete on its own. Everything else is human-gated.
AUTOMATICALLY_SOLVABLE = frozenset({ARITHMETIC, ORDINARY_FORM, KNOWLEDGE_QUESTION})

# Security challenges. These exist to distinguish humans from automation, so the only
# legitimate response is to pause and hand the challenge to the operator. There is no
# solving path here and none may be added.
_SECURITY_MARKERS = (
    "i am not a robot", "i'm not a robot", "verify you are human", "prove you are human",
    "select all squares", "select all images", "click each image", "select the images",
    "traffic light", "crosswalk", "bicycle", "fire hydrant", "motorcycle",
    "storefront", "bus in the",
    "recaptcha", "hcaptcha", "turnstile", "arkose", "funcaptcha",
    "geetest", "press & hold", "press and hold", "slide to verify", "drag the puzzle",
)

_IMAGE_CAPTCHA_MARKERS = (
    "select all squares with", "click on the image", "choose the image",
    "select the picture", "which picture shows", "click all that",
)

_OTP_MARKERS = (
    "one-time code", "one time code", "verification code", "otp", "2fa", "two-factor",
    "authenticator app", "sms code", "enter the code we sent", "code we just sent",
    "6-digit code", "4-digit code",
)

_PAYMENT_MARKERS = (
    "pay now", "confirm payment", "card number", "cvv", "credit card", "debit card",
    "billing address", "checkout", "purchase", "buy now", "payment method",
    "connect wallet", "approve spending", "sign transaction", "confirm transaction",
    "signature request", "gas fee to", "unlimited allowance",
)

_KNOWLEDGE_MARKERS = (
    "what does", "which of the following", "true or false", "answer the question",
    "what is the capital", "quiz", "trivia",
)


def classify_verification_challenge(prompt_text: str) -> Dict[str, Any]:
    """Classify a verification prompt so the right handling is chosen.

    This is the distinction the old code lacked: it treated every verification prompt
    alike, so it either tried to solve a security CAPTCHA or parked a trivial question
    the agent could have answered itself.

    Security CAPTCHAs are NEVER solved here. They are reported as requiring the operator,
    and no answer value is produced for them.
    """
    text = (prompt_text or "").strip()
    if not text:
        return {
            "category": UNSUPPORTED,
            "automatic": False,
            "requires_human": True,
            "reason": "The verification prompt is empty, so it cannot be classified.",
        }
    lowered = text.lower()

    # Order matters: a security marker must win over an incidental number in the text.
    if any(m in lowered for m in _SECURITY_MARKERS):
        return {
            "category": SECURITY_CAPTCHA,
            "automatic": False,
            "requires_human": True,
            "reason": (
                "This is a security challenge intended to distinguish humans from automation. "
                "The agent will not attempt to solve or bypass it; the operator must complete "
                "it directly in the supported interface."
            ),
        }
    if any(m in lowered for m in _IMAGE_CAPTCHA_MARKERS):
        return {
            "category": IMAGE_CAPTCHA,
            "automatic": False,
            "requires_human": True,
            "reason": "This is an image-selection challenge and requires a human.",
        }
    if any(m in lowered for m in _PAYMENT_MARKERS):
        return {
            "category": PAYMENT_APPROVAL,
            "automatic": False,
            "requires_human": True,
            "reason": (
                "This is a payment or wallet-approval request. It requires explicit operator "
                "authorization and is never completed automatically."
            ),
        }
    if any(m in lowered for m in _OTP_MARKERS):
        return {
            "category": OTP_CHALLENGE,
            "automatic": False,
            "requires_human": True,
            "reason": (
                "This is an authentication code challenge. Only the operator holds the code, "
                "and the agent must never ask for or handle it."
            ),
        }

    # Ordinary arithmetic is solvable locally and deterministically.
    arithmetic = solve_arithmetic(text)
    if arithmetic.answered:
        return {
            "category": ARITHMETIC,
            "automatic": True,
            "requires_human": False,
            "answer": arithmetic.display,
            "operation": arithmetic.operation,
            "reason": "Ordinary arithmetic computed locally and deterministically.",
        }

    if any(m in lowered for m in _KNOWLEDGE_MARKERS):
        return {
            "category": KNOWLEDGE_QUESTION,
            "automatic": False,
            "requires_human": False,
            "reason": (
                "A non-security knowledge question. It may be answered from the campaign's own "
                "instructions, but no answer is invented here."
            ),
        }

    return {
        "category": UNSUPPORTED,
        "automatic": False,
        "requires_human": True,
        "reason": (
            "This verification type is not recognised and is not attempted. The operator is "
            "asked to complete it."
        ),
    }

