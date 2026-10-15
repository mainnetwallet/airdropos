"""AirdropOS Single Fixed LLM Client

MANDATORY CONSTRAINT (Specification Part 3):
  AirdropOS uses EXACTLY ONE configured LLM integration with ONE fixed active model.
  There is no model switching, no fallback, no routing, and no load balancing.

All AI-powered reasoning (planning, research interpretation, tool selection, task
decomposition, reporting, learning analysis, self-coding) flows through this module.

INPUT CONTEXT BUDGET
--------------------
`complete` and `stream` run a token preflight before submitting. A request that
fits the configured budget is sent byte-for-byte as built, with no rewriting of
any kind. A request that cannot fit is reduced by `llm.compaction`, which now asks
the SAME fixed model for a bounded, structured summary - it never touches a system
message or the active objective, and never discards content silently. See
`llm/compaction.py` for the workflow and its termination policy.

Compaction calls the model through `_compaction_summarize`, a private path that
skips the budget preflight entirely. Without that, a compaction request would need
compacting itself, which is exactly the recursion it must not have.

This is strictly an INPUT concern. It has nothing to do with `LLM_MAX_TOKENS`,
which is an OUTPUT limit and remains unset by default, leaving `max_tokens` off
the request entirely.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from backend.app.core.config import settings
from backend.app.llm.compaction import (
    ContextCompactionError,
    classify_provider_error,
    detect_billing_refusal_in_content,
)
from backend.app.llm.token_budget import TokenBudget, count_request_tokens, record_usage

logger = logging.getLogger(__name__)

# Set while the budget preflight (and therefore compaction) is running, so a nested
# normal request can never re-enter the guarded path and recurse.
_in_compaction: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "airdropos_llm_in_compaction", default=False
)

# Set while the compaction summarisation call is in flight. That path deliberately
# bypasses the preflight, so this flag is what stops it from ever being re-entered:
# a compaction request can never trigger a second compaction.
_in_compaction_call: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "airdropos_llm_compaction_call", default=False
)


class LLMError(Exception):
    """Raised when the single configured LLM endpoint fails.

    The system must report this failure, never silently switch models.
    """

    def __init__(self, message: str, category: str = "PROVIDER_ERROR", retryable: bool = True):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class LLMPolicyError(Exception):
    """Raised when a caller attempts forbidden model-switching behaviour."""


class Message:
    """A single chat message. Deliberately simple to keep provider payloads explicit."""

    __slots__ = ("role", "content", "name", "tool_call_id")

    def __init__(
        self,
        role: str,
        content: str,
        name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ):
        if role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"Invalid role: {role}")
        self.role = role
        self.content = content
        self.name = name
        self.tool_call_id = tool_call_id

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Message(role={self.role!r}, len={len(self.content)})"


class LLMResponse:
    """Normalized response from the single fixed model.

    `model` and `requested_model` are deliberately separate. `requested_model`
    is what AirdropOS asked for, and it is fixed. `model` is what the provider
    said it served, and is None when the provider said nothing at all. Collapsing
    the two would make a routing endpoint indistinguishable from a compliant one.
    """

    __slots__ = ("content", "model", "requested_model", "usage", "finish_reason", "raw")

    def __init__(
        self,
        content: str,
        model: Optional[str],
        usage: Dict[str, int],
        finish_reason: Optional[str],
        raw: Dict[str, Any],
        requested_model: Optional[str] = None,
    ):
        self.content = content
        self.model = model
        self.requested_model = requested_model
        self.usage = usage
        self.finish_reason = finish_reason
        self.raw = raw

    @property
    def token_count(self) -> int:
        return int(self.usage.get("total_tokens", 0))

    @property
    def model_matches_request(self) -> Optional[bool]:
        """True/False when the provider named a model, None when it did not."""
        if self.model is None or not self.requested_model:
            return None
        return self.model == self.requested_model or self.model.startswith(
            f"{self.requested_model}-"
        )


class FixedLLMClient:
    """The ONE and ONLY LLM integration in AirdropOS.

    OpenAI-compatible so any compatible endpoint can be configured without
    rewriting any agent architecture.
    """

    def __init__(self) -> None:
        self.base_url = settings.LLM_BASE_URL.rstrip("/")
        self.api_key = settings.LLM_API_KEY
        # Locked at construction. Any attempt to alter it raises LLMPolicyError.
        self._model_id: str = settings.LLM_MODEL_ID
        self._temperature = settings.LLM_TEMPERATURE
        # None means "no application-imposed output limit": the parameter is then omitted
        # from every request and the provider's own limit applies.
        self._max_tokens: Optional[int] = settings.LLM_MAX_TOKENS
        self._timeout = settings.LLM_TIMEOUT_SECONDS
        # Retries AFTER the first attempt, hard-capped at one: a failed call (timeout, connection,
        # 429, 5xx) is tried once more, and if that also fails the error is raised at once. An
        # older .env with LLM_MAX_RETRIES=3 therefore cannot bring back long retry chains.
        self._max_retries = max(0, min(int(settings.LLM_MAX_RETRIES), 1))
        self._max_attempts = self._max_retries + 1
        # Every distinct model the provider has said it served. Exposed so an
        # operator can SEE routing instead of inferring it.
        self._served_models: set = set()

    # ------------------------------------------------------------------
    # Fixed-model integrity
    # ------------------------------------------------------------------
    @property
    def model_id(self) -> str:
        return self._model_id

    def assert_model_locked(self, requested_model: str) -> None:
        """Guard against accidental model routing or fallback.

        The agent may only ever address the single configured model.
        """
        if requested_model != self._model_id:
            raise LLMPolicyError(
                "Model switching is forbidden. AirdropOS uses exactly one fixed model. "
                f"Requested '{requested_model}' but the locked model is '{self._model_id}'."
            )

    def _output_limit(self, override: Optional[int] = None) -> Optional[int]:
        """The output-token limit for one request, or None when none is configured."""
        return self._max_tokens if override is None else override

    def _build_payload(
        self,
        messages: List[Message],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
        stop: Optional[List[str]] = None,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """The one place a chat-completion request body is assembled.

        Normal, JSON-mode and streaming requests all go through here so they cannot
        drift apart. ``max_tokens`` is included only when a limit was explicitly
        configured (or passed by the caller); it is never sent as null.
        """
        payload: Dict[str, Any] = {
            "model": self._model_id,
            "messages": [m.to_payload() for m in messages],
            "temperature": self._temperature if temperature is None else temperature,
        }
        limit = self._output_limit(max_tokens)
        if limit is not None:
            payload["max_tokens"] = limit
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if stop:
            payload["stop"] = stop
        if stream:
            payload["stream"] = True
        return payload

    def describe(self) -> Dict[str, Any]:
        """Safe descriptor for the dashboard. Never exposes the API key."""
        return {
            "base_url": self.base_url,
            "model_id": self._model_id,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "timeout_seconds": self._timeout,
            "max_retries": self._max_retries,
            "api_key_configured": bool(self.api_key) and "placeholder" not in self.api_key,
            "api_key_masked": self._mask_key(),
            "policy": "single_fixed_model",
            "context_budget": self.budget.describe(),
            # What the provider has actually said it served. Empty means no call
            # has completed yet. A non-empty list containing anything other than
            # the configured model is provider-side routing, not a configuration
            # problem, and AirdropOS cannot prevent it.
            "served_models_observed": sorted(
                m for m in self._served_models if m is not None
            ),
            "provider_omitted_model": None in self._served_models,
        }

    @staticmethod
    def _mask_key() -> str:
        key = settings.LLM_API_KEY or ""
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:3]}{'*' * (len(key) - 7)}{key[-4:]}"

    # ------------------------------------------------------------------
    # Input context budget
    # ------------------------------------------------------------------
    @property
    def budget(self) -> TokenBudget:
        return TokenBudget.from_settings()

    @property
    def in_compaction(self) -> bool:
        return _in_compaction.get()

    async def _preflight(
        self, messages: List[Message], *, aggressive: bool = False
    ) -> List[Message]:
        """Size-check and, only if necessary, reduce a request.

        A request inside the budget returns the identical list of identical
        message objects, so the overwhelming majority of calls pay a token count
        and nothing else.

        `aggressive` exists for the one case the estimate cannot see: the provider
        itself refused a request that was under the trigger. It must therefore NOT
        short-circuit on `estimated <= trigger`, or the recovery retry would resend the
        very payload that was just refused.
        """
        from backend.app.llm.compaction import build_compactor

        budget = self.budget
        estimated = count_request_tokens(messages)
        if estimated <= budget.trigger and not aggressive:
            return messages

        logger.info(
            "llm.context preflight: estimated=%s trigger=%s usable=%s window=%s aggressive=%s",
            estimated,
            budget.trigger,
            budget.usable_input,
            budget.context_window,
            aggressive,
        )

        token = _in_compaction.set(True)
        try:
            compactor = build_compactor(budget, summarizer=self._compaction_summarize)
            result = await compactor.compact_request(messages, aggressive=aggressive)
        finally:
            _in_compaction.reset(token)

        report = result.report
        logger.info(
            "llm.context compacted: estimated=%s -> counted=%s strategies=%s reduced=%s "
            "llm_rounds=%s removed_chars=%s references=%s",
            report.original_tokens,
            report.final_tokens,
            ",".join(report.strategies) or "none",
            report.messages_reduced,
            report.llm_rounds,
            report.characters_removed,
            report.references_emitted,
        )
        return result.messages

    async def _compaction_summarize(self, messages: List[Message]) -> Optional[str]:
        """Ask the fixed model to summarise context for compaction - no recursion.

        This is a DEDICATED internal path: it builds the payload and posts it directly,
        bypassing the budget preflight. Going through `complete()` instead would make
        compaction depend on compaction, which for an oversized request is an infinite
        regress. `_in_compaction_call` guards the path against ever being re-entered.

        The model is the same locked `LLM_MODEL_ID` as every other call. No routing, no
        fallback and no model switching is introduced here, and the output-token limit is
        left exactly as configured (unset by default).
        """
        if _in_compaction_call.get():
            logger.warning("Refusing a nested compaction summarisation call")
            return None

        token = _in_compaction_call.set(True)
        try:
            payload = self._build_payload(messages, temperature=0.0, json_mode=True)
            try:
                raw = await self._post("/chat/completions", payload)
            except LLMError as exc:
                logger.warning(
                    "LLM compaction round could not complete (%s); falling back.", exc.category
                )
                return None
            record_usage(count_request_tokens(messages), raw.get("usage"))
            try:
                response = self._normalize(raw)
            except LLMError as exc:
                logger.warning(
                    "LLM compaction round produced an unusable reply (%s); falling back.",
                    exc.category,
                )
                return None
            self._record_served_model(response.model)
            return response.content
        finally:
            _in_compaction_call.reset(token)

    async def _try_preflight(
        self, messages: List[Message], *, aggressive: bool = False
    ) -> List[Message]:
        """Preflight, surfacing "cannot fit" as the error type callers already handle.

        `ContextCompactionError` deliberately does not inherit from `LLMError`,
        because an unreducible request is not a provider fault. It is translated
        here so the existing `except LLMError` handlers in the planner, the
        discovery extractor and the chat loop report it correctly instead of it
        escaping as an unhandled exception. The message is preserved verbatim, so
        the operator still gets the actionable explanation.
        """
        try:
            return await self._preflight(messages, aggressive=aggressive)
        except ContextCompactionError as exc:
            raise LLMError(str(exc), category="CONTEXT_TOO_LARGE", retryable=False) from exc

    # ------------------------------------------------------------------
    # Core completion
    # ------------------------------------------------------------------
    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
        stop: Optional[List[str]] = None,
    ) -> LLMResponse:
        """Execute a chat completion against the single fixed model."""
        if self.in_compaction or _in_compaction_call.get():
            # Compacting a request by making a request that needs the same compaction
            # would recurse, so it is refused rather than retried. The dedicated
            # summarisation path (`_compaction_summarize`) uses the transport directly
            # and never arrives here.
            raise LLMError(
                "A nested LLM request was made while compaction was running.",
                category="COMPACTION_RECURSION",
                retryable=False,
            )

        prepared = await self._try_preflight(messages)

        attempts = 0
        empty_retries = 0
        while True:
            payload = self._build_payload(
                prepared,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
                stop=stop,
            )
            estimated = count_request_tokens(prepared)

            try:
                raw = await self._post("/chat/completions", payload)
            except LLMError as exc:
                if exc.category != "CONTEXT_OVERFLOW":
                    raise
                if attempts >= settings.LLM_CONTEXT_OVERFLOW_MAX_RETRIES:
                    logger.error(
                        "llm.context overflow recovery exhausted after %s retry attempt(s); "
                        "estimated=%s usable=%s",
                        attempts,
                        estimated,
                        self.budget.usable_input,
                    )
                    raise
                attempts += 1
                logger.warning(
                    "llm.context provider reported a context overflow at estimated=%s tokens; "
                    "retrying once with a harder reduction (attempt %s/%s)",
                    estimated,
                    attempts,
                    settings.LLM_CONTEXT_OVERFLOW_MAX_RETRIES,
                )
                # A smaller request is required. Retrying the same payload could
                # only fail the same way, so the retry is built from a harder
                # reduction and recounted before it is sent.
                prepared = await self._try_preflight(messages, aggressive=True)
                if count_request_tokens(prepared) >= estimated:
                    raise LLMError(
                        "The provider reported a context overflow but the request could not "
                        "be reduced any further, so it was not retried.",
                        category="CONTEXT_OVERFLOW",
                        retryable=False,
                    )
                continue

            # A router picks the model per request, and a reasoning model sometimes spends
            # its whole reply thinking and returns no content. That is transient: the same
            # request often succeeds on the next call, so retry a bounded number of times
            # instead of failing the operator's message on the first empty reply.
            try:
                response = self._normalize(raw)
            except LLMError as exc:
                if (
                    exc.category == "EMPTY_CONTENT"
                    and empty_retries < settings.LLM_EMPTY_CONTENT_MAX_RETRIES
                ):
                    empty_retries += 1
                    record_usage(estimated, raw.get("usage"))
                    logger.warning(
                        "llm.empty_content the model returned reasoning but no answer; "
                        "retrying the same request (%s/%s)",
                        empty_retries,
                        settings.LLM_EMPTY_CONTENT_MAX_RETRIES,
                    )
                    continue
                raise
            break

        record_usage(estimated, raw.get("usage"))
        self._record_served_model(response.model)
        return response

    def _normalize(self, raw: Dict[str, Any]) -> "LLMResponse":
        """Turn a provider body into an `LLMResponse`, or fail loudly."""
        choices = raw.get("choices") or []
        if not choices:
            raise LLMError("LLM returned no choices", category="MALFORMED_RESPONSE", retryable=False)

        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""

        # A reasoning model can spend the whole reply on reasoning and return a
        # null body. That is not an empty answer, and it must not be mistaken
        # for one: it is reported as what it is.
        if not content and message.get("reasoning"):
            raise LLMError(
                "The model used its entire response on reasoning and returned no content.",
                category="EMPTY_CONTENT",
                retryable=False,
            )

        # The configured endpoint answers an unfunded request with a normal
        # completion whose text is a top-up notice. Surfacing that as the
        # model's answer would send the caller hunting for a JSON problem that
        # does not exist.
        if detect_billing_refusal_in_content(content):
            raise LLMError(
                "The LLM endpoint refused the request for lack of credit; it is not a "
                "model answer and not a context problem.",
                category="BILLING_REFUSAL",
                retryable=False,
            )

        return LLMResponse(
            content=content,
            # `model` is what the provider actually reported. It is deliberately
            # NOT defaulted to the configured id: filling in a model the provider
            # never claimed is exactly how a router serving something else stays
            # invisible. `None` means "the provider did not say", which is a
            # different fact from "it said the configured one".
            model=raw.get("model") or None,
            requested_model=self._model_id,
            usage=raw.get("usage") or {},
            finish_reason=choice.get("finish_reason"),
            raw=raw,
        )

    def _record_served_model(self, served_model: Optional[str]) -> None:
        """Note a served-model mismatch without breaking a legitimate provider.

        A provider may legitimately answer with a dated variant of the model it
        was asked for (`gpt-4o` -> `gpt-4o-2024-08-06`), so a mismatch is
        reported rather than treated as a failure. The endpoint may be a router
        that picks the model per request, so a different model is expected there:
        it is recorded in the diagnostics and logged at DEBUG, or at WARNING when
        LLM_WARN_ON_SERVED_MODEL_MISMATCH is on. LLM_REQUIRE_SERVED_MODEL_MATCH
        makes it a hard failure instead.
        """
        self._served_models.add(served_model)
        if served_model is None or served_model == self._model_id:
            return
        if served_model.startswith(self._model_id + "-"):
            return  # a dated variant of the same model; normal and not a fault
        # Routers (API routers, gateways) pick the model per request, so a different served
        # model is expected there. Keep it visible in diagnostics (`served_models_observed`),
        # but only warn when the operator asked for that.
        level = logging.WARNING if settings.LLM_WARN_ON_SERVED_MODEL_MISMATCH else logging.DEBUG
        logger.log(
            level,
            "LLM endpoint served model %r for requested model %r.",
            served_model, self._model_id,
        )
        if settings.LLM_REQUIRE_SERVED_MODEL_MATCH:
            raise LLMError(
                f"The LLM endpoint served {served_model!r} but {self._model_id!r} is "
                "required. Refusing the response under LLM_REQUIRE_SERVED_MODEL_MATCH.",
                category="MODEL_MISMATCH",
                retryable=False,
            )

    async def complete_json(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Complete and parse strict JSON. Raises if the model emits prose.

        The caller supplies messages whose system prompt mandates JSON output.
        """
        response = await self.complete(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        return self.parse_json(response.content)

    @staticmethod
    def parse_json(content: str) -> Dict[str, Any]:
        """Tolerant JSON extraction that still fails loudly on garbage.

        Models routinely wrap the object: a sentence of prose and then a fenced
        ```json block, or the fence alone, or text after it. Some routers also ignore
        ``response_format``. So after trying the whole text, this looks for the object
        inside fences and then anywhere in the text. A JSON *array* or a bare value is
        still rejected: callers need an object.
        """
        text = (content or "").strip()
        if not text:
            raise LLMError("LLM returned empty content", category="MALFORMED_RESPONSE", retryable=False)

        # strict=False allows raw newlines/tabs inside strings, which models emit often.
        decoder = json.JSONDecoder(strict=False)

        def as_object(candidate: str) -> Optional[Dict[str, Any]]:
            try:
                value = decoder.decode(candidate.strip())
            except ValueError:
                return None
            return value if isinstance(value, dict) else None

        found = as_object(text)
        if found is not None:
            return found

        candidates: List[Dict[str, Any]] = []

        # 1. Fenced blocks anywhere in the text (```json ... ``` or ``` ... ```).
        for match in re.finditer(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n?(.*?)```", text, re.DOTALL):
            block = as_object(match.group(1))
            if block is not None:
                candidates.append(block)

        # 2. Any JSON object embedded in prose: try to decode from each opening brace.
        if not candidates:
            index = text.find("{")
            while index != -1:
                try:
                    value, end = decoder.raw_decode(text, index)
                except ValueError:
                    index = text.find("{", index + 1)
                    continue
                if isinstance(value, dict):
                    candidates.append(value)
                    index = text.find("{", end)  # skip past the object just read
                else:
                    index = text.find("{", index + 1)

        if candidates:
            # Prefer the object that looks like the agent contract; else the first.
            for candidate in candidates:
                if "reply" in candidate or "tool_calls" in candidate or "create_task" in candidate:
                    return candidate
            return candidates[0]

        snippet = text[:300].replace("\n", " ")
        raise LLMError(
            f"LLM did not return valid JSON. Content began: {snippet}",
            category="MALFORMED_RESPONSE",
            retryable=False,
        )

    async def stream(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Stream tokens from the single fixed model for chat UIs."""
        if self.in_compaction or _in_compaction_call.get():
            raise LLMError(
                "A nested LLM request was made while compaction was running.",
                category="COMPACTION_RECURSION",
                retryable=False,
            )

        # Preflight runs BEFORE the stream opens, because once the first token
        # has been handed to the caller a retry is no longer transparent.
        prepared = await self._try_preflight(messages)
        payload = self._build_payload(
            prepared, temperature=temperature, max_tokens=max_tokens, stream=True
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    text = body.decode("utf-8", "replace")
                    # The stream never yields partial output before this point,
                    # so a context overflow here is still safely retryable. Once
                    # the first token has been yielded below, no retry is ever
                    # attempted: replaying would duplicate output the caller has
                    # already seen.
                    if classify_provider_error(response.status_code, text) == "CONTEXT_OVERFLOW":
                        raise LLMError(
                            "The model refused the streamed request because it exceeded "
                            "the context window.",
                            category="CONTEXT_OVERFLOW",
                            retryable=False,
                        )
                    raise LLMError(
                        f"LLM stream failed with HTTP {response.status_code}: {text[:300]!r}",
                        category="HTTP_ERROR",
                        retryable=response.status_code >= 500,
                    )
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        token = delta.get("content")
                        if token:
                            yield token

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------
    async def health_check(self) -> Dict[str, Any]:
        """Probe the configured endpoint. Distinguishes config problems from outages."""
        payload = {
            "model": self._model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        started = asyncio.get_event_loop().time()
        try:
            raw = await self._post("/chat/completions", payload)
            latency_ms = (asyncio.get_event_loop().time() - started) * 1000
            served = raw.get("model") or None
            self._record_served_model(served)
            return {
                "status": "HEALTHY",
                "latency_ms": round(latency_ms, 2),
                "requested_model": self._model_id,
                "served_model": served,
                "model_matches_request": (
                    None if served is None
                    else served == self._model_id or served.startswith(f"{self._model_id}-")
                ),
                "detail": "Fixed model endpoint responding",
            }
        except LLMError as exc:
            return {
                "status": "UNAVAILABLE",
                "latency_ms": None,
                "model": self._model_id,
                "detail": str(exc),
                "error_category": exc.category,
                "retryable": exc.retryable,
            }

    # ------------------------------------------------------------------
    # Transport with retry policy
    # ------------------------------------------------------------------
    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}{path}"
        last_error: Optional[LLMError] = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)

                if response.status_code == 200:
                    return response.json()

                body_snippet = response.text[:400]

                # A genuine context-window refusal is the one 4xx that is worth
                # acting on rather than merely reporting, because the caller can
                # genuinely fix it by sending a smaller request. It is detected
                # from the provider's own wording, never guessed from the status
                # code alone: a 400 that is not about size must stay a plain
                # bad request and must not trigger compaction.
                overflow = classify_provider_error(response.status_code, response.text)
                if overflow == "CONTEXT_OVERFLOW":
                    raise LLMError(
                        "The model refused the request because it exceeded the context "
                        f"window: {body_snippet}",
                        category="CONTEXT_OVERFLOW",
                        retryable=False,
                    )
                if overflow == "BILLING_REFUSAL":
                    raise LLMError(
                        "The LLM endpoint refused the request for lack of credit.",
                        category="BILLING_REFUSAL",
                        retryable=False,
                    )

                # 4xx (other than rate limit) is not retryable: retrying cannot help.
                if response.status_code in (401, 403):
                    raise LLMError(
                        f"LLM authentication failed (HTTP {response.status_code}). "
                        "Verify LLM_API_KEY for the configured endpoint.",
                        category="AUTH_FAILURE",
                        retryable=False,
                    )
                if response.status_code == 404:
                    raise LLMError(
                        f"LLM endpoint or model not found (HTTP 404). "
                        f"Verify LLM_BASE_URL and LLM_MODEL_ID ('{self._model_id}').",
                        category="NOT_FOUND",
                        retryable=False,
                    )
                if response.status_code == 429:
                    last_error = LLMError(
                        "LLM rate limit exceeded (HTTP 429).", category="RATE_LIMITING", retryable=True
                    )
                elif response.status_code >= 500:
                    last_error = LLMError(
                        f"LLM provider error (HTTP {response.status_code}): {body_snippet}",
                        category="PROVIDER_ERROR",
                        retryable=True,
                    )
                else:
                    raise LLMError(
                        f"LLM rejected the request (HTTP {response.status_code}): {body_snippet}",
                        category="BAD_REQUEST",
                        retryable=False,
                    )

            except httpx.TimeoutException as exc:
                last_error = LLMError(f"LLM request timed out: {exc}", category="TIMEOUT", retryable=True)
            except httpx.ConnectError as exc:
                last_error = LLMError(
                    f"Cannot reach LLM endpoint {self.base_url}: {exc}",
                    category="CONNECTION_ERROR",
                    retryable=True,
                )
            except httpx.HTTPError as exc:
                last_error = LLMError(f"LLM transport error: {exc}", category="TRANSPORT_ERROR", retryable=True)

            if attempt < self._max_attempts:
                backoff = min(2 ** (attempt - 1), 8)
                logger.warning(
                    "LLM call attempt %s/%s failed (%s). Retrying in %ss. Model stays locked to %s.",
                    attempt,
                    self._max_attempts,
                    last_error.category if last_error else "unknown",
                    backoff,
                    self._model_id,
                )
                await asyncio.sleep(backoff)

        raise last_error or LLMError("LLM call failed", category="UNKNOWN", retryable=True)


# Module-level singleton. There is exactly one configured LLM for the whole platform.
llm = FixedLLMClient()
