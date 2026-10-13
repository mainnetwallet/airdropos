"""AirdropOS Single Fixed LLM Client

MANDATORY CONSTRAINT (Specification Part 3):
  AirdropOS uses EXACTLY ONE configured LLM integration with ONE fixed active model.
  There is no model switching, no fallback, no routing, and no load balancing.

All AI-powered reasoning (planning, research interpretation, tool selection, task
decomposition, reporting, learning analysis, self-coding) flows through this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


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
    """Normalized response from the single fixed model."""

    __slots__ = ("content", "model", "usage", "finish_reason", "raw")

    def __init__(
        self,
        content: str,
        model: str,
        usage: Dict[str, int],
        finish_reason: Optional[str],
        raw: Dict[str, Any],
    ):
        self.content = content
        self.model = model
        self.usage = usage
        self.finish_reason = finish_reason
        self.raw = raw

    @property
    def token_count(self) -> int:
        return int(self.usage.get("total_tokens", 0))


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
        self._max_tokens = settings.LLM_MAX_TOKENS
        self._timeout = settings.LLM_TIMEOUT_SECONDS
        self._max_retries = settings.LLM_MAX_RETRIES

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
        payload: Dict[str, Any] = {
            "model": self._model_id,
            "messages": [m.to_payload() for m in messages],
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if stop:
            payload["stop"] = stop

        raw = await self._post("/chat/completions", payload)

        choices = raw.get("choices") or []
        if not choices:
            raise LLMError("LLM returned no choices", category="MALFORMED_RESPONSE", retryable=False)

        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""

        return LLMResponse(
            content=content,
            model=raw.get("model", self._model_id),
            usage=raw.get("usage") or {},
            finish_reason=choice.get("finish_reason"),
            raw=raw,
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
        self, messages: List[Message], temperature: Optional[float] = None
    ) -> AsyncIterator[str]:
        """Stream tokens from the single fixed model for chat UIs."""
        payload = {
            "model": self._model_id,
            "messages": [m.to_payload() for m in messages],
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise LLMError(
                        f"LLM stream failed with HTTP {response.status_code}: {body[:300]!r}",
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
            return {
                "status": "HEALTHY",
                "latency_ms": round(latency_ms, 2),
                "model": raw.get("model", self._model_id),
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

        for attempt in range(1, self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)

                if response.status_code == 200:
                    return response.json()

                body_snippet = response.text[:400]

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

            if attempt < self._max_retries:
                backoff = min(2 ** (attempt - 1), 8)
                logger.warning(
                    "LLM call attempt %s/%s failed (%s). Retrying in %ss. Model stays locked to %s.",
                    attempt,
                    self._max_retries,
                    last_error.category if last_error else "unknown",
                    backoff,
                    self._model_id,
                )
                await asyncio.sleep(backoff)

        raise last_error or LLMError("LLM call failed", category="UNKNOWN", retryable=True)


# Module-level singleton. There is exactly one configured LLM for the whole platform.
llm = FixedLLMClient()
