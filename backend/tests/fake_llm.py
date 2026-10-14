"""A deterministic stand-in for the one fixed LLM, for Agent Inbox reply interpretation.

It records every call, so a test can prove the model was (or was not) consulted and see
exactly what context it was given. It can answer with a fixed object, a sequence, a callable
of the request, or raise an `LLMError`.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Union

from backend.app.llm.client import LLMError

Answer = Union[Dict[str, Any], str, BaseException, Callable[[Dict[str, Any]], Any]]


def decision(
    kind: str,
    *,
    explanation: str = "Interpreted by the test double.",
    extracted: Optional[List[str]] = None,
    missing: Optional[List[str]] = None,
    next_step: str = "",
) -> Dict[str, Any]:
    return {
        "decision": kind,
        "explanation": explanation,
        "extracted_information": list(extracted or []),
        "missing_information": list(missing or []),
        "recommended_next_step": next_step,
    }


class FakeInterpretationLLM:
    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.calls: List[Dict[str, Any]] = []  # the parsed user-context of each call
        self.system_prompts: List[str] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete_json(self, messages, **kwargs) -> Dict[str, Any]:
        self.system_prompts.append(messages[0].content)
        context = json.loads(messages[-1].content)
        self.calls.append(context)
        answer = self.answer
        if callable(answer):
            answer = answer(context)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, str):
            # Models sometimes answer in prose; the real client raises on that.
            raise LLMError(f"LLM did not return valid JSON. Content began: {answer[:80]}",
                           category="MALFORMED_RESPONSE", retryable=False)
        return dict(answer)
