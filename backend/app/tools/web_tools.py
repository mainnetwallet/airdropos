"""Web & Research Tools.

All external content returned by these tools is UNTRUSTED DATA. The extraction
helpers deliberately strip nothing that would change the text's meaning but do
mark provenance so downstream consumers can tell official sources from hearsay.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from backend.app.tools.base import BaseTool, SideEffect, ToolResult

logger = logging.getLogger(__name__)

USER_AGENT = "AirdropOS/1.0 (autonomous research agent; +https://airdropos.local)"

MAX_REDIRECTS = 5


class DestinationBlocked(Exception):
    """A URL (or a redirect hop) points at a loopback, link-local, metadata or private address."""


async def guarded_get(url: str, timeout: float) -> httpx.Response:
    """GET ``url`` following redirects manually, vetting every hop.

    The research tools take URLs chosen by an LLM that reads untrusted pages, so an attacker can
    steer them at cloud metadata, loopback or the compose network (postgres, redis). Each hop is
    checked with the browser's resolving destination check, which fails closed. Limitation: httpx
    resolves the name again when it connects, so DNS rebinding is narrowed, not eliminated; an
    egress firewall remains the complete answer.
    """
    from backend.app.browser.controller import destination_block_reason  # lazy: avoid import cycles

    current = url
    last_request: Optional[httpx.Request] = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            reason = await destination_block_reason(current)
            if reason:
                raise DestinationBlocked(reason)
            response = await client.get(current, headers={"User-Agent": USER_AGENT})
            last_request = response.request
            location = response.headers.get("location")
            if response.is_redirect and location:
                current = str(response.url.join(location))
                continue
            return response
    raise httpx.TooManyRedirects(f"More than {MAX_REDIRECTS} redirects", request=last_request)


# Content that commonly indicates a campaign announcement.
ANNOUNCEMENT_PATTERNS = [
    r"\bairdrop\b", r"\bsnapshot\b", r"\bclaim\b", r"\beligible\b",
    r"\brewards?\b", r"\bpoints?\b", r"\btestnet\b", r"\bseason\b",
]

DATE_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4})\b",
    re.IGNORECASE,
)

# Instructions embedded in fetched pages are stripped before reaching the LLM.
INJECTION_MARKERS = [
    "ignore previous instructions", "ignore all previous", "disregard your instructions",
    "you are now", "new instructions:", "system prompt", "reveal your",
    "override your", "disable your safety", "act as", "developer mode",
]


def sanitize_external_text(text: str, max_chars: int = 12000) -> Dict[str, Any]:
    """Neutralize prompt-injection content in externally fetched text.

    We do not attempt to be clever. Suspicious passages are annotated so the
    LLM sees them as hostile data, not as instructions.
    """
    truncated = text[:max_chars]
    findings: List[str] = []
    sanitized_lines = []
    for line in truncated.splitlines():
        lowered = line.lower()
        hit = next((m for m in INJECTION_MARKERS if m in lowered), None)
        if hit:
            findings.append(hit)
            sanitized_lines.append(f"[UNTRUSTED-CONTENT-FLAGGED: {hit}] {line[:200]}")
        else:
            sanitized_lines.append(line)
    return {
        "text": "\n".join(sanitized_lines),
        "injection_markers_found": findings,
        "is_flagged": bool(findings),
        "truncated": len(text) > max_chars,
    }


def extract_dates(text: str) -> List[str]:
    return [m.group(0) for m in DATE_PATTERN.finditer(text)]


class WebFetchTool(BaseTool):
    """Fetch a URL and return its text content with provenance."""

    name = "web_fetch"
    version = "1.0.0"
    description = "Fetch a web page and return its readable text content, detected dates, and injection flags."
    required_permissions = ["web.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL", "maxLength": 2048},
            "max_chars": {"type": "integer", "description": "Truncation limit", "minimum": 500, "maximum": 50000},
        },
        "required": ["url"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        url = params["url"]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult.failure(f"Unsupported URL scheme: {parsed.scheme}", "INVALID_INPUT")

        try:
            response = await guarded_get(url, self.timeout_seconds)
        except DestinationBlocked as exc:
            return ToolResult.failure(f"Destination not allowed: {exc}", category="DESTINATION_BLOCKED", retryable=False)
        except httpx.TooManyRedirects as exc:
            return ToolResult.failure(f"Redirect loop: {exc}", category="REDIRECT_LOOP", retryable=False)
        except httpx.TimeoutException:
            return ToolResult.failure(f"Timeout fetching {url}", category="NETWORK_TIMEOUT", retryable=True)
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"Fetch failed: {exc}", category="NETWORK_ERROR", retryable=True)

        if response.status_code >= 400:
            return ToolResult.failure(
                f"HTTP {response.status_code} fetching {url}", category="HTTP_ERROR",
                retryable=response.status_code >= 500,
            )

        html = response.text
        text = _html_to_text(html)
        sanitized = sanitize_external_text(text, params.get("max_chars", 12000))

        return ToolResult.success(
            data={
                "url": url,
                "final_url": str(response.url),
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "text": sanitized["text"],
                "dates_detected": extract_dates(text),
                "injection_flagged": sanitized["is_flagged"],
                "injection_markers": sanitized["injection_markers_found"],
                "content_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )


class SiteSearchTool(BaseTool):
    """Search a site's own pages for campaign keywords."""

    name = "site_search"
    version = "1.0.0"
    description = "Fetch a site index page and extract lines mentioning campaign keywords such as airdrop, claim, snapshot, or rewards."
    required_permissions = ["web.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Page to scan", "maxLength": 2048},
            "keywords": {
                "type": "array",
                "description": "Keywords to match",
                "items": {"type": "string"},
            },
        },
        "required": ["url"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        url = params["url"]
        keywords = [k.lower() for k in (params.get("keywords") or ANNOUNCEMENT_PATTERNS)]
        keywords = [re.sub(r"\\b|\\s", "", k) if k.startswith("\\") else k for k in keywords]

        try:
            response = await guarded_get(url, self.timeout_seconds)
            response.raise_for_status()
        except DestinationBlocked as exc:
            return ToolResult.failure(f"Destination not allowed: {exc}", category="DESTINATION_BLOCKED", retryable=False)
        except Exception as exc:
            return ToolResult.failure(f"Site scan failed: {exc}", category="NETWORK_ERROR", retryable=True)

        text = _html_to_text(response.text)
        matches = []
        for line in text.splitlines():
            lowered = line.lower()
            hit = next((k for k in keywords if k.strip("\\b") in lowered), None)
            if hit:
                matches.append(line.strip()[:300])
            if len(matches) >= 100:
                break

        return ToolResult.success(
            data={
                "url": url,
                "keywords": keywords,
                "match_count": len(matches),
                "matches": matches,
                "dates_detected": extract_dates(text),
            }
        )


class DocumentationReaderTool(BaseTool):
    """Read official documentation pages for eligibility and claim requirements."""

    name = "read_documentation"
    version = "1.0.0"
    description = "Read an official documentation page and extract eligibility, requirement, and deadline sections."
    required_permissions = ["web.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Documentation URL", "maxLength": 2048},
            "sections": {
                "type": "array",
                "description": "Section keywords to extract",
                "items": {"type": "string"},
            },
        },
        "required": ["url"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        url = params["url"]
        sections = [s.lower() for s in (params.get("sections") or ["eligib", "require", "claim", "deadline", "snapshot"])]

        try:
            response = await guarded_get(url, self.timeout_seconds)
            response.raise_for_status()
        except DestinationBlocked as exc:
            return ToolResult.failure(f"Destination not allowed: {exc}", category="DESTINATION_BLOCKED", retryable=False)
        except Exception as exc:
            return ToolResult.failure(f"Documentation read failed: {exc}", category="NETWORK_ERROR", retryable=True)

        text = _html_to_text(response.text)
        sanitized = sanitize_external_text(text, 20000)

        lines = sanitized["text"].splitlines()
        extracted: Dict[str, List[str]] = {s: [] for s in sections}
        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) > 500:
                continue
            for section in sections:
                if section in stripped.lower() and len(extracted[section]) < 30:
                    extracted[section].append(stripped)

        return ToolResult.success(
            data={
                "url": url,
                "sections": extracted,
                "dates_detected": extract_dates(text),
                "injection_flagged": sanitized["is_flagged"],
                "read_at": datetime.now(timezone.utc).isoformat(),
            }
        )


def _html_to_text(html: str) -> str:
    """Best-effort readable text extraction without a heavy parser dependency."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except Exception:
        # Regex fallback keeps the tool functional if bs4 fails.
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"&amp;?", "&", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def register_web_tools(registry) -> None:
    registry.register_or_replace_many([WebFetchTool(), SiteSearchTool(), DocumentationReaderTool()])
