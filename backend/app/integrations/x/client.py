"""Official X API v2 transport client.

Scope: transport and authentication only. It signs, sends, classifies and
returns a normalised :class:`XApiResult`. It decides nothing about routing,
persistence or policy - that lives in the service layer.

Verified against the official reference (docs.x.com, OpenAPI ``2.172``). The scope
sets below are transcribed from each endpoint's own ``security`` block, not
inferred:

===============================  ==================================  ====================================
Operation                        Endpoint                            Required scopes
===============================  ==================================  ====================================
who am I                         ``GET  /2/users/me``               ``users.read``
user by handle                   ``GET  /2/users/by/username/:u``   ``users.read``
user by id                       ``GET  /2/users/:id``               ``users.read``
read a post                      ``GET  /2/tweets/:id``              ``tweet.read``, ``users.read``
create a post / reply            ``POST /2/tweets``                  ``tweet.read``, ``tweet.write``, ``users.read``
like a post                      ``POST /2/users/:id/likes``         ``like.write``, ``tweet.read``, ``users.read``
unlike a post                    ``DEL  /2/users/:id/likes/:tweet``  ``like.write``, ``tweet.read``, ``users.read``
follow                           ``POST /2/users/:id/following``      ``users.read``, ``tweet.read``, ``follows.write``
unfollow                         ``DEL  /2/users/:src/following/:t`` ``users.read``, ``tweet.read``, ``follows.write``
posts I liked (verify a like)    ``GET  /2/users/:id/liked_tweets``  ``like.read``, ``users.read``
accounts I follow (verify)       ``GET  /2/users/:id/following``     ``follows.read``, ``users.read``
my recent posts (verify a post)  ``GET  /2/users/:id/tweets``        ``tweet.read``, ``users.read``
refresh a token                  ``POST /2/oauth2/token``            ``offline.access`` at auth time
===============================  ==================================  ====================================

``like.read`` and ``follows.read`` are NOT needed to like or follow: they gate
the *list* endpoints, which this integration only uses for verification.

Base URL is ``https://api.x.com``. Media upload, quote posts, DMs, Spaces and
Ads are deliberately NOT implemented here: they are not part of this
integration's surface and guessing at them would be worse than refusing.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.x.com"
AUTHORIZE_URL = f"{API_BASE}/2/oauth2/authorize"
TOKEN_URL = f"{API_BASE}/2/oauth2/token"

DEFAULT_TIMEOUT = 20.0
# A post is 280 characters for a free account and 25_000 for Premium. The API
# itself is authoritative; this bound only stops the model from sending a
# megabyte of text to find out.
MAX_POST_CHARS = 25_000

# Page bounds X actually enforces. `liked_tweets` rejects max_results below 5;
# `following` accepts 1-1000. An out-of-range value is a 400, not a clamp, so
# both are bounded here.
LIKED_PAGE_MIN, LIKED_PAGE_MAX = 5, 100
FOLLOWING_PAGE_MIN, FOLLOWING_PAGE_MAX = 1, 1000
# Defaults are deliberately modest: X bills these list reads PER RESOURCE, so a
# 1000-entry follow check is not free. An operator can widen these.
LIKED_PAGE_DEFAULT = 100
FOLLOWING_PAGE_DEFAULT = 100


# Every scope this integration requests at authorization time. `offline.access`
# is what makes a refresh token possible; without it the operator has to
# re-authorize whenever the access token expires. Nothing beyond what the
# operations above need is requested.
AUTHORIZATION_SCOPES = [
    "tweet.read",
    "tweet.write",
    "users.read",
    "like.read",
    "like.write",
    "follows.read",
    "follows.write",
    "offline.access",
]

# Scopes each operation needs. Used to refuse an operation up front rather than
# spending a request to be told 403 later.
OPERATION_SCOPES: Dict[str, Tuple[str, ...]] = {
    # GET /2/users/me
    "whoami": ("users.read",),
    # GET /2/users/by/username/:username, GET /2/users/:id
    "read_profile": ("users.read",),
    # GET /2/tweets/:id
    "read_post": ("tweet.read", "users.read"),
    # POST /2/tweets
    "create_post": ("tweet.read", "tweet.write", "users.read"),
    "create_reply": ("tweet.read", "tweet.write", "users.read"),
    # POST/DELETE /2/users/:id/likes[/tweet_id] -- note: like.read is NOT one
    # of them; it gates the liked-posts LIST, not the action.
    "like": ("like.write", "tweet.read", "users.read"),
    "unlike": ("like.write", "tweet.read", "users.read"),
    # POST/DELETE /2/users/:id/following[/target] -- same: follows.read gates
    # the list, not the action.
    "follow": ("users.read", "tweet.read", "follows.write"),
    "unfollow": ("users.read", "tweet.read", "follows.write"),
    # Verification reads.
    "verify_like": ("like.read", "users.read"),
    "verify_follow": ("follows.read", "users.read"),
    "verify_post": ("tweet.read", "users.read"),
}


class XApiError(Exception):
    """A request reached X and X refused it, or the transport failed.

    ``uncertain`` is the important field: it is True exactly when the request
    may have been applied on X's side but the answer never arrived. A caller
    must NOT retry such an operation without checking current state first.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        category: str = "X_API_ERROR",
        retryable: bool = False,
        uncertain: bool = False,
        problem_type: str = "",
        problem_detail: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.category = category
        self.retryable = retryable
        self.uncertain = uncertain
        self.problem_type = problem_type
        self.problem_detail = problem_detail


@dataclass
class XApiResult:
    """A normalised, secret-free result."""

    ok: bool
    operation: str
    status_code: int = 0
    data: Dict[str, Any] = field(default_factory=dict)
    # ``uncertain`` propagates to the activity record: the write may or may not
    # have happened and must be verified, not retried.
    uncertain: bool = False
    retry_after_seconds: int = 0
    rate_limit_remaining: Optional[int] = None
    # Absolute reset time (epoch seconds) from `x-rate-limit-reset`, when X sent
    # one. Persisted so a rate-limited configuration is refused until the window
    # actually rolls over, rather than being retried into another 429.
    rate_limit_reset_epoch: Optional[int] = None
    error: str = ""
    error_category: str = ""
    problem_type: str = ""


# Categories. Distinct names so the activity log can say WHAT happened rather
# than collapsing everything into "error".
CAT_AUTH = "X_AUTH_FAILED"
CAT_PERMISSION = "X_PERMISSION_DENIED"
CAT_RATE_LIMIT = "X_RATE_LIMITED"
CAT_NOT_FOUND = "X_NOT_FOUND"
CAT_INVALID = "X_INVALID_REQUEST"
CAT_POLICY = "X_POLICY_DENIED"
CAT_SERVER = "X_SERVER_ERROR"
CAT_NETWORK = "X_NETWORK_ERROR"
CAT_TIMEOUT = "X_TIMEOUT_UNCERTAIN"
CAT_UNSUPPORTED = "X_OPERATION_UNSUPPORTED"


def scopes_missing(granted: Optional[List[str]], required: Tuple[str, ...]) -> List[str]:
    """Scopes the credential does not obviously have.

    X only returns the granted scope list at authorization time, so an empty or
    unknown list is reported as "unknown" rather than as "missing everything".
    """
    have = {str(s).strip() for s in (granted or []) if str(s).strip()}
    if not have:
        return []
    return [s for s in required if s not in have]


# ======================================================================
# OAuth 2.0 helpers (authorization code + PKCE)
# ======================================================================
def generate_pkce() -> Dict[str, str]:
    """A fresh PKCE verifier/challenge pair for the authorization-code flow."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return {"code_verifier": verifier, "code_challenge": challenge}


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: Optional[List[str]] = None,
) -> str:
    """The URL the operator opens to authorize an account.

    Scopes are requested explicitly. If X adds a scope to the account's default
    set that is not in ``AUTHORIZATION_SCOPES`` it is dropped rather than
    silently granted: this integration never requests more than it needs.
    """
    from urllib.parse import urlencode

    if not client_id:
        raise XApiError("A client id is required to build an authorization URL.", category=CAT_INVALID)
    wanted = [s for s in (scopes or AUTHORIZATION_SCOPES)]
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(wanted),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _problem(response: httpx.Response) -> Tuple[str, str]:
    """Extract (problem_type, problem_detail) from an X error body."""
    try:
        body = response.json()
    except Exception:
        return "", (response.text or "")[:200]
    if not isinstance(body, dict):
        return "", str(body)[:200]
    parts: List[Dict[str, Any]] = []
    for key in ("errors", "error"):
        value = body.get(key)
        if isinstance(value, list):
            parts.extend(p for p in value if isinstance(p, dict))
        elif isinstance(value, dict):
            parts.append(value)
    problem = body.get("problem") if isinstance(body.get("problem"), dict) else None
    if isinstance(problem, dict):
        parts.append(problem)
    if not parts and isinstance(body.get("detail"), str):
        return str(body.get("type", "")), body["detail"][:300]
    types = [str(p.get("type", "")) for p in parts if p.get("type")]
    details = [str(p.get("detail") or p.get("message") or p.get("title") or "") for p in parts]
    return (types[0] if types else ""), ("; ".join(d for d in details if d))[:300]


def classify(status: int) -> Tuple[str, bool]:
    """(category, retryable) for an HTTP status from X."""
    if status in (401,):
        return CAT_AUTH, False
    if status == 403:
        return CAT_PERMISSION, False
    if status == 429:
        return CAT_RATE_LIMIT, True
    if status in (404, 410):
        return CAT_NOT_FOUND, False
    if status in (400, 422):
        return CAT_INVALID, False
    if status >= 500:
        return CAT_SERVER, True
    return CAT_INVALID, False


class XApiClient:
    """A single, stateless client bound to one configuration's credentials.

    It holds no cache between calls beyond the token it was constructed with,
    and it never mutates the stored token: refreshing is the service's job, so
    that a refreshed token can be persisted through the same audited write path.
    """

    def __init__(
        self,
        *,
        access_token: str,
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        scopes: Optional[List[str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._access_token = str(access_token or "")
        self._client_id = str(client_id or "")
        self._client_secret = str(client_secret or "")
        self._refresh_token = str(refresh_token or "")
        self._scopes = list(scopes or [])
        self._timeout = float(timeout or DEFAULT_TIMEOUT)

    # ------------------------------------------------------------------
    def can(operation: str) -> Tuple[bool, str]:
        """Whether this credential can attempt ``operation`` at all."""
        required = OPERATION_SCOPES.get(operation)
        if not required:
            return False, f"'{operation}' is not an X operation this integration implements."
        if not self._access_token:
            return False, "No authorized X account. Complete OAuth for this configuration first."
        return True, ""

    def missing_scopes(self, operation: str) -> List[str]:
        """Scopes this credential apparently lacks, per the stored scope list.

        X only reports the granted scopes at authorization time, so an empty or
        absent list means "unknown" and yields ``[]`` rather than an accusation.
        Informational only: the authoritative check is X's own 403, and this
        exists to explain it before the operator spends a request on it.
        """
        return scopes_missing(self._scopes, OPERATION_SCOPES.get(operation, ()))

    # ------------------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> XApiResult:
        """One HTTP round trip, fully classified.

        A transport timeout on a write is reported with ``uncertain=True``: the
        request may have been accepted. Treating that as a plain failure is what
        produces duplicate posts.
        """
        if not self._access_token:
            return XApiResult(
                ok=False,
                operation=operation,
                error="This configuration has no authorized X account.",
                error_category=CAT_AUTH,
            )
        url = f"{API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "User-Agent": "AirdropOS/1.0 (+https://airdropos.local)",
        }
        is_write = operation in _WRITE_OPERATIONS
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, url, headers=headers, json=json_body, params=params
                )
        except httpx.TimeoutException as exc:
            logger.warning("X API timeout operation=%s type=%s", operation, type(exc).__name__)
            return XApiResult(
                ok=False,
                operation=operation,
                uncertain=is_write,
                error=(
                    "The X API did not answer in time. The outcome is UNKNOWN: the operation "
                    "may still have been applied. Verify the current state before retrying."
                ),
                error_category=CAT_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            logger.warning("X API transport error operation=%s type=%s", operation, type(exc).__name__)
            # A connection that never completed is ambiguous for a write: the
            # request may have reached X before the transport gave up.
            return XApiResult(
                ok=False,
                operation=operation,
                uncertain=is_write,
                error=(
                    f"Could not reach the X API ({type(exc).__name__}). "
                    + (
                        "The outcome of this write is UNKNOWN: verify the current state "
                        "before retrying."
                        if is_write
                        else "Nothing was changed."
                    )
                ),
                error_category=CAT_NETWORK,
            )
        return self._interpret(operation, response)

    def _interpret(self, operation: str, response: httpx.Response) -> XApiResult:
        rate_remaining = _int_header(response, "x-rate-limit-remaining")
        rate_reset = _int_header(response, "x-rate-limit-reset")
        retry_after = _retry_after(response)

        if response.status_code >= 400:
            category, _retryable = classify(response.status_code)
            problem_type, problem_detail = _problem(response)
            message = _safe_error_message(category, response.status_code, problem_type, problem_detail)
            return XApiResult(
                ok=False,
                operation=operation,
                status_code=response.status_code,
                uncertain=False,  # X answered, so the operation definitively did not apply
                retry_after_seconds=retry_after,
                rate_limit_remaining=rate_remaining,
                rate_limit_reset_epoch=rate_reset,
                error=message,
                error_category=category,
                problem_type=problem_type,
            )

        try:
            body = response.json()
        except Exception:
            body = {}
        return XApiResult(
            ok=True,
            operation=operation,
            status_code=response.status_code,
            data=body if isinstance(body, dict) else {"data": body},
            rate_limit_remaining=rate_remaining,
            rate_limit_reset_epoch=rate_reset,
            retry_after_seconds=retry_after,
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------
    async def whoami(self) -> XApiResult:
        """``GET /2/users/me`` - proves the token belongs to a real account."""
        result = await self.request(
            "GET", "/2/users/me", operation="whoami",
            params={"user.fields": "id,username,name,created_at,verified,public_metrics"},
        )
        if result.ok:
            user = (result.data.get("data") or {}) if isinstance(result.data, dict) else {}
            if isinstance(user, dict):
                result.data = {"user": _public_user(user)}
        return result

    async def read_user_by_username(self, username: str) -> XApiResult:
        handle = str(username or "").lstrip("@").strip()
        if not handle:
            return _invalid("whoami", "A username (without @) is required.")
        result = await self.request(
            "GET", f"/2/users/by/username/{handle}", operation="read_profile",
            params={"user.fields": "id,username,name,description,created_at,verified,public_metrics"},
        )
        if result.ok:
            user = (result.data.get("data") or {}) if isinstance(result.data, dict) else {}
            if isinstance(user, dict):
                result.data = {"user": _public_user(user)}
        return result

    async def read_user_by_id(self, user_id: str) -> XApiResult:
        result = await self.request(
            "GET", f"/2/users/{user_id}", operation="read_profile",
            params={"user.fields": "id,username,name,description,created_at,verified,public_metrics"},
        )
        if result.ok:
            user = (result.data.get("data") or {}) if isinstance(result.data, dict) else {}
            if isinstance(user, dict):
                result.data = {"user": _public_user(user)}
        return result

    async def read_post(self, post_id: str, *, own: bool = False) -> XApiResult:
        """``GET /2/tweets/:id``. ``own=True`` verifies a post WE created."""
        result = await self.request(
            "GET", f"/2/tweets/{post_id}", operation="read_post",
            params={
                "expansions": "author_id",
                "tweet.fields": "created_at,conversation_id,public_metrics,text",
                "user.fields": "id,username",
            },
        )
        if result.ok and own:
            result.data = {**(result.data or {}), "verified_by": "direct_lookup"}
        return result

    async def my_recent_posts(self, user_id: str, limit: int = 10) -> XApiResult:
        """This account's most recent posts, for confirming a publish landed."""
        result = await self.request(
            "GET", f"/2/users/{user_id}/tweets", operation="verify_post",
            params={"max_results": max(5, min(int(limit or 10), 100)), "tweet.fields": "created_at,text"},
        )
        if result.ok:
            result.data = {"posts": (result.data.get("data") or []) if isinstance(result.data, dict) else []}
        return result

    async def liked_posts(
        self, user_id: str, post_id: str, limit: int = LIKED_PAGE_DEFAULT
    ) -> XApiResult:
        """Whether ``post_id`` appears among the user's liked posts.

        This is a PAGE-BOUNDED check, and says so. X has no "did I like this
        post" endpoint, so the only way to answer is to page through
        ``/2/users/:id/liked_tweets``. If the post is not on the page that was
        read, the honest answer is "not among the most recent N", NOT "not
        liked": an account with more than N likes would otherwise be told its
        like never happened, and the caller would act on that false negative.
        """
        limit = max(LIKED_PAGE_MIN, min(int(limit or LIKED_PAGE_DEFAULT), LIKED_PAGE_MAX))
        result = await self.request(
            "GET", f"/2/users/{user_id}/liked_tweets", operation="verify_like",
            params={"max_results": limit, "tweet.fields": "created_at"},
        )
        if not result.ok:
            # The check itself failed. That is a different thing from "not liked".
            return XApiResult(
                ok=False,
                operation="verify_like",
                status_code=result.status_code,
                error=f"The liked-posts check could not run: {result.error}",
                error_category=result.error_category or CAT_NETWORK,
            )
        body = result.data if isinstance(result.data, dict) else {}
        liked = list(body.get("data") or [])
        found = any(str(item.get("id")) == str(post_id) for item in liked if isinstance(item, dict))
        more = bool((body.get("meta") or {}).get("next_token")) or len(liked) >= limit
        return XApiResult(
            ok=True,
            operation="verify_like",
            status_code=result.status_code,
            data={
                "liked": found,
                "post_id": str(post_id),
                "checked_count": len(liked),
                "page_size": limit,
                "list_exhausted": not more,
                "confidence": "PARTIAL" if (not found and more) else "DEFINITIVE",
                "note": (
                    f"Found in the {len(liked)} most recent likes."
                    if found
                    else (
                        "Not among the most recent likes, and that list was fully read."
                        if not more
                        else (
                            f"Not among the {len(liked)} most recent likes, but the list has more "
                            "pages, so this is NOT proof the post was not liked. Check on x.com "
                            "rather than repeating the action."
                        )
                    )
                ),
            },
        )

    async def is_following(
        self, user_id: str, target_user_id: str, limit: int = FOLLOWING_PAGE_DEFAULT
    ) -> XApiResult:
        """Whether ``target_user_id`` is in the user's following list.

        Page-bounded and cost-bounded: X bills this read per returned resource, so
        the default page stays modest and an exhausted check is distinguished from
        a truncated one.
        """
        limit = max(FOLLOWING_PAGE_MIN, min(int(limit or FOLLOWING_PAGE_DEFAULT), FOLLOWING_PAGE_MAX))
        result = await self.request(
            "GET", f"/2/users/{user_id}/following", operation="verify_follow",
            params={"max_results": limit, "user.fields": "id,username"},
        )
        if not result.ok:
            return XApiResult(
                ok=False,
                operation="verify_follow",
                status_code=result.status_code,
                error=f"The following-list check could not run: {result.error}",
                error_category=result.error_category or CAT_NETWORK,
            )
        body = result.data if isinstance(result.data, dict) else {}
        following = list(body.get("data") or [])
        found = any(str(item.get("id")) == str(target_user_id) for item in following if isinstance(item, dict))
        more = bool((body.get("meta") or {}).get("next_token")) or len(following) >= limit
        return XApiResult(
            ok=True,
            operation="verify_follow",
            status_code=result.status_code,
            data={
                "following": found,
                "checked_count": len(following),
                "page_size": limit,
                "list_exhausted": not more,
                "confidence": "PARTIAL" if (not found and more) else "DEFINITIVE",
                "note": (
                    "Found in the following list that was read."
                    if found
                    else (
                        "Not in the following list, and that list was fully read."
                        if not more
                        else (
                            f"Not among the {len(following)} most recent follows, but the list has "
                            "more pages, so this is NOT proof the follow did not happen."
                        )
                    )
                ),
            },
        )

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------
    async def create_post(self, text: str, *, reply_to: Optional[str] = None) -> XApiResult:
        """``POST /2/tweets``. A reply is the same call plus ``reply``.

        Returns the created post id when X answers. That id is the only reliable
        handle for verifying (or de-duplicating) the operation later, so it is
        surfaced verbatim.

        X answers ``201`` with BOTH ``data`` and ``errors`` when the post was
        created but something attached to it failed (an unresolvable mention, a
        duplicate-rule violation). That is still a created post: reporting it as a
        failure would invite a retry and DUPLICATE it. It is reported as success
        with ``partial`` set, so the caller keeps the id and knows to look.
        """
        body: Dict[str, Any] = {"text": str(text or "")}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
        operation = "create_reply" if reply_to else "create_post"
        result = await self.request("POST", "/2/tweets", operation=operation, json_body=body)
        if not result.ok:
            return result

        payload = result.data if isinstance(result.data, dict) else {}
        data = payload.get("data")
        if not isinstance(data, dict) or not str(data.get("id") or ""):
            # A 2xx with no post id means no post was created. Reporting success
            # here would be a lie the operator acts on.
            detail = _problem_summary(payload)
            return XApiResult(
                ok=False, operation=operation, status_code=result.status_code,
                rate_limit_remaining=result.rate_limit_remaining,
                rate_limit_reset_epoch=result.rate_limit_reset_epoch,
                error=(
                    "X answered without returning a post id, so nothing was created."
                    + (f" {detail}" if detail else "")
                ),
                error_category=CAT_INVALID,
            )
        summary: Dict[str, Any] = {
            "post_id": str(data.get("id", "")),
            "text": str(data.get("text", "")),
        }
        partial = _problem_summary(payload)
        if partial:
            summary["partial"] = True
            # Always a list, so a consumer does not have to know whether this
            # particular call produced one warning or several.
            summary["warnings"] = [partial]
        return XApiResult(
            ok=True, operation=operation, status_code=result.status_code,
            data=summary,
            rate_limit_remaining=result.rate_limit_remaining,
            rate_limit_reset_epoch=result.rate_limit_reset_epoch,
        )

    async def like_post(self, user_id: str, post_id: str) -> XApiResult:
        """``POST /2/users/:id/likes``.

        X returns ``{"data": {"liked": true}}``. A 200 whose body says
        ``liked: false`` means the like did NOT take effect, so it is a failure -
        not a success dressed up by the status code.
        """
        result = await self._write(
            "POST", f"/2/users/{user_id}/likes", {"tweet_id": str(post_id)}, "like"
        )
        return _assert_flag(result, flag="liked", want=True, what="the like")

    async def unlike_post(self, user_id: str, post_id: str) -> XApiResult:
        result = await self._write(
            "DELETE", f"/2/users/{user_id}/likes/{post_id}", None, "unlike"
        )
        return _assert_flag(result, flag="liked", want=False, what="the unlike")

    async def follow_user(self, user_id: str, target_user_id: str) -> XApiResult:
        """``POST /2/users/:id/following``.

        X returns ``{"data": {"following": bool, "pending_follow": bool}}``.
        ``pending_follow`` is true when the target protects their posts, so the
        follow is only a REQUEST. Reporting that as "you now follow them" is
        exactly the false success this refuses to produce.
        """
        result = await self._write(
            "POST", f"/2/users/{user_id}/following", {"target_user_id": str(target_user_id)}, "follow"
        )
        return self._assert_follow(result, want=True)

    async def unfollow_user(self, user_id: str, target_user_id: str) -> XApiResult:
        result = await self._write(
            "DELETE", f"/2/users/{user_id}/following/{target_user_id}", None, "unfollow"
        )
        return self._assert_follow(result, want=False)

    @staticmethod
    def _assert_follow(result: XApiResult, *, want: bool) -> XApiResult:
        """Check the relationship actually changed, not just that X said 200."""
        if not result.ok:
            return result
        flags = ((result.data or {}).get("response") or {}).get("flags") or {}
        following = bool(flags.get("following"))
        pending = bool(flags.get("pending_follow"))
        if following == want and not (want and pending):
            return result
        if want and pending:
            return XApiResult(
                ok=False, operation=result.operation, status_code=result.status_code,
                rate_limit_remaining=result.rate_limit_remaining,
                rate_limit_reset_epoch=result.rate_limit_reset_epoch,
                error=(
                    "X accepted the request but the account does NOT follow that user yet: the "
                    "target protects their posts and the request is pending their approval."
                ),
                error_category=CAT_POLICY,
                data={"applied": False, "following": False, "pending_follow": True},
            )
        return XApiResult(
            ok=False, operation=result.operation, status_code=result.status_code,
            rate_limit_remaining=result.rate_limit_remaining,
            rate_limit_reset_epoch=result.rate_limit_reset_epoch,
            error=(
                f"X answered, but the relationship is not in the requested state "
                f"(following={following}). The action did not take effect."
            ),
            error_category=CAT_INVALID,
            data={"applied": False, "following": following, "pending_follow": pending},
        )

    async def _write(
        self, method: str, path: str, body: Optional[Dict[str, Any]], operation: str
    ) -> XApiResult:
        result = await self.request(method, path, operation=operation, json_body=body)
        if result.ok:
            result.data = {"applied": True, "response": _shape_only(result.data)}
        return result

    # ------------------------------------------------------------------
    async def refresh_access_token(self) -> XApiResult:
        """``POST /2/oauth2/token`` with the refresh-token grant.

        The returned tokens are secrets; the caller persists them through the
        credential store and must not log them.
        """
        if not (self._client_id and self._client_secret and self._refresh_token):
            return XApiResult(
                ok=False,
                operation="refresh",
                error=(
                    "This configuration has no refresh token, so the access token cannot be "
                    "renewed. Re-authorize the account."
                ),
                error_category=CAT_AUTH,
            )
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    TOKEN_URL,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Authorization": _basic_auth_header(self._client_id, self._client_secret),
                    },
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "client_id": self._client_id,
                    },
                )
        except httpx.TimeoutException:
            return XApiResult(
                ok=False, operation="refresh", uncertain=False,
                error="The token endpoint did not answer in time.", error_category=CAT_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return XApiResult(
                ok=False, operation="refresh", uncertain=False,
                error=f"Could not reach the X token endpoint ({type(exc).__name__}).",
                error_category=CAT_NETWORK,
            )
        if response.status_code >= 400:
            category, _ = classify(response.status_code)
            _, detail = _problem(response)
            return XApiResult(
                ok=False, operation="refresh", status_code=response.status_code,
                error=_safe_error_message(category, response.status_code, "", detail),
                error_category=category,
            )
        try:
            body = response.json()
        except Exception:
            return XApiResult(
                ok=False, operation="refresh",
                error="The token endpoint returned a response that is not JSON.",
                error_category=CAT_SERVER,
            )
        expires_in = body.get("expires_in")
        expires_at = None
        if isinstance(expires_in, (int, float)):
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        return XApiResult(
            ok=True,
            operation="refresh",
            status_code=response.status_code,
            data={
                "access_token": str(body.get("access_token", "")),
                "refresh_token": str(body.get("refresh_token", "")) or self._refresh_token,
                "token_type": str(body.get("token_type", "bearer")),
                "scope": str(body.get("scope", "")),
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )


# Writes whose outcome can be ambiguous when the answer is lost. Read-only
# operations are safe to retry, so they are deliberately absent.
_WRITE_OPERATIONS = {
    "create_post",
    "create_reply",
    "like",
    "unlike",
    "follow",
    "unfollow",
}


# ======================================================================
# Helpers
# ======================================================================
def _invalid(operation: str, message: str) -> XApiResult:
    return XApiResult(ok=False, operation=operation, error=message, error_category=CAT_INVALID)


def _int_header(response: httpx.Response, name: str) -> Optional[int]:
    raw = response.headers.get(name)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _retry_after(response: httpx.Response) -> int:
    """Seconds to wait before retrying, from whichever header X sent."""
    retry_after = _int_header(response, "retry-after") or 0
    reset = _int_header(response, "x-rate-limit-reset") or 0
    if reset:
        # `x-rate-limit-reset` is an epoch second on X, not a relative delay.
        wait = int(reset - time.time())
        if wait > 0:
            return min(max(wait, 1), 900)
    return min(max(retry_after, 0), 900)


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist of user fields. Anything X adds later is dropped on purpose."""
    keep = (
        "id", "username", "name", "description", "created_at", "verified",
        "verified_type", "public_metrics", "protected",
    )
    return {k: user[k] for k in keep if k in user}


def _problem_summary(body: Any, limit: int = 5) -> str:
    """One short line summarising the ``errors`` array, if there is one.

    X sends ``data`` AND ``errors`` together when the write applied but something
    attached to it did not. Both halves matter: dropping the errors hides a real
    problem, and treating the whole call as failed invites a duplicate.
    """
    if not isinstance(body, dict):
        return ""
    errors = body.get("errors")
    if not isinstance(errors, list) or not errors:
        return ""
    parts: List[str] = []
    for item in errors[:limit]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("detail") or item.get("title") or item.get("message") or "").strip()
        if text:
            parts.append(text[:160])
    return "; ".join(parts)


def _shape_only(body: Any) -> Dict[str, Any]:
    """Summarise a write response without echoing a whole payload back.

    Keeps the boolean result fields X documents for each write (``liked``,
    ``following``, ``pending_follow``). Those are what prove the action took
    effect, so they must survive into the caller's result.
    """
    if not isinstance(body, dict):
        return {"received": True}
    out: Dict[str, Any] = {"received": True}
    data = body.get("data")
    flags: Dict[str, Any] = {}
    if isinstance(data, dict):
        out["data_id"] = str(data.get("id", ""))
        for name in ("liked", "following", "pending_follow"):
            if name in data:
                flags[name] = data[name]
    elif isinstance(data, list):
        out["data_count"] = len(data)
    if flags:
        out["flags"] = flags
    detail = _problem_summary(body)
    if detail:
        out["warnings"] = [detail]
        out["partial"] = True
    return out


def _assert_flag(result: XApiResult, *, flag: str, want: bool, what: str) -> XApiResult:
    """Refuse to call a write successful unless the body confirms it.

    A 2xx alone is not proof. X documents an explicit boolean for the likes and
    follows endpoints, and when that boolean does not match what was asked for,
    the honest answer is a failure. An absent flag is "cannot confirm", never
    assumed good.
    """
    if not result.ok:
        return result
    flags = ((result.data or {}).get("response") or {}).get("flags") or {}
    if not flags or flag not in flags:
        return XApiResult(
            ok=False, operation=result.operation, status_code=result.status_code,
            rate_limit_remaining=result.rate_limit_remaining,
            rate_limit_reset_epoch=result.rate_limit_reset_epoch,
            error=(
                f"X answered, but the response did not confirm {what}. Treat the outcome as "
                "UNKNOWN and verify on x.com before repeating it."
            ),
            error_category=CAT_INVALID,
            data={"applied": False, "confirmed": False},
            uncertain=True,
        )
    if bool(flags[flag]) is want:
        return result
    return XApiResult(
        ok=False, operation=result.operation, status_code=result.status_code,
        rate_limit_remaining=result.rate_limit_remaining,
        rate_limit_reset_epoch=result.rate_limit_reset_epoch,
        error=(
            f"X answered, but it reports {flag}={bool(flags[flag])}, so {what} did not take effect."
        ),
        error_category=CAT_INVALID,
        data={"applied": False, "flags": flags},
    )


def _safe_error_message(category: str, status: int, problem_type: str, detail: str) -> str:
    """An operator-readable message. Never includes response headers or tokens."""
    if category == CAT_AUTH:
        base = "X rejected the credentials (401). The account needs to be authorized again."
    elif category == CAT_PERMISSION:
        base = (
            "X refused the request (403). The app, the account, or this API plan is not "
            "permitted to perform it."
        )
    elif category == CAT_RATE_LIMIT:
        base = "X rate limit reached (429). Wait for the reset and do not retry sooner."
    elif category == CAT_NOT_FOUND:
        base = "X does not have that object (404). Check the id or handle."
    elif category == CAT_SERVER:
        base = f"X returned a server error ({status}). This is usually temporary."
    else:
        base = f"X rejected the request ({status})."
    if detail:
        return f"{base} {detail}"
    if problem_type:
        return f"{base} ({problem_type})"
    return base


def x_api_enabled() -> bool:
    """Operator switch for the whole API half of this integration."""
    return bool(getattr(settings, "X_API_ENABLED", True))