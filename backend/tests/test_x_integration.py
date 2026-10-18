"""Tests for the X.com dual (browser + official API) integration.

Everything external is mocked. No test here contacts api.x.com, and no test here
uses a real credential: live X API behaviour has NOT been verified by this suite,
and a passing run must not be read as evidence that it has.

What IS covered:

* the API client's request shape, response parsing and error classification
* authentication and permission failures, and what the agent is told about them
* credential encryption, AAD binding, and secret redaction everywhere
* multiple configurations, selection, and bulk-import validation
* routing under auto / api_preferred / browser_preferred
* timeouts and uncertain write outcomes
* duplicate-write prevention
* task attribution and persistence of the activity history
"""

from __future__ import annotations

import json
import secrets as secrets_module
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest

from backend.app.integrations.x import browser_adapter, routing, service
from backend.app.core.config import settings
from backend.app.integrations.x.client import (
    AUTHORIZATION_SCOPES,
    XApiClient,
    XApiResult,
    build_authorize_url,
    classify,
    generate_pkce,
)
from backend.app.integrations.x import credentials as creds
from backend.app.integrations.x import crypto, store
from backend.app.models import XActivityRecord, XApiConfiguration
from backend.app.models.x_integration import (
    CONFIG_OK,
    CONFIG_UNVERIFIED,
)


# ======================================================================
# Fakes
# ======================================================================
class FakeResponse:
    """Minimal stand-in for httpx.Response, enough for the client's parsing."""

    def __init__(self, status: int, body: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self) -> Any:
        return self._body

    @property
    def text(self) -> str:  # noqa: D401 - mirrors httpx
        return json.dumps(self._body)


def fake_transport(monkeypatch, handler) -> List[Dict[str, Any]]:
    """Replace the client's httpx call and record every request it makes."""
    seen: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, headers=None, json=None, params=None):
            seen.append({"method": method, "url": url, "headers": headers or {}, "json": json, "params": params})
            return handler(method, url, json=json, params=params)

        async def post(self, url, headers=None, data=None, json=None):
            seen.append({"method": "POST", "url": url, "headers": headers or {}, "data": data})
            return handler("POST", url, json=json, data=data)

    monkeypatch.setattr("backend.app.integrations.x.client.httpx.AsyncClient", FakeClient)
    return seen


def error_body(status: int, detail: str, problem: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "errors": [
            {
                "message": detail,
                "detail": detail,
                "title": problem or "Error",
                "type": problem or "https://api.x.com/2/problems/invalid-request",
                "status": status,
            }
        ]
    }
    return body


def a_client(**kwargs) -> XApiClient:
    return XApiClient(access_token="user-token", client_id="cid", client_secret="csec", **kwargs)


# ======================================================================
# API client: request shape and response parsing
# ======================================================================
async def test_whoami_returns_only_whitelisted_user_fields(monkeypatch):
    seen = fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(
            200,
            {
                "data": {
                    "id": "2244994945",
                    "username": "XDevelopers",
                    "name": "X Developers",
                    "created_at": "2013-12-14T04:35:55.000Z",
                    "some_new_field_x_added": "must not leak",
                }
            },
        ),
    )
    result = await a_client().whoami()

    assert result.ok
    assert result.data["user"]["username"] == "XDevelopers"
    assert "some_new_field_x_added" not in result.data["user"]
    assert seen[0]["method"] == "GET"
    assert seen[0]["url"].endswith("/2/users/me")
    assert seen[0]["headers"]["Authorization"] == "Bearer user-token"


async def test_create_post_sends_the_documented_body(monkeypatch):
    seen = fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(201, {"data": {"id": "1889", "text": "hello"}}),
    )
    result = await a_client().create_post("hello")

    assert result.ok
    assert result.data["post_id"] == "1889"
    assert seen[0]["method"] == "POST"
    assert seen[0]["url"].endswith("/2/tweets")
    assert seen[0]["json"] == {"text": "hello"}


async def test_create_reply_nests_the_reply_object(monkeypatch):
    seen = fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(201, {"data": {"id": "2", "text": "re"}}),
    )
    await a_client().create_post("re", reply_to="1")

    assert seen[0]["json"] == {"text": "re", "reply": {"in_reply_to_tweet_id": "1"}}


async def test_like_and_follow_use_the_acting_users_id(monkeypatch):
    seen = fake_transport(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": {"liked": True}}))
    client = a_client()
    await client.like_post("42", "999")
    await client.follow_user("42", "77")

    assert seen[0]["url"].endswith("/2/users/42/likes")
    assert seen[0]["json"] == {"tweet_id": "999"}
    assert seen[1]["url"].endswith("/2/users/42/following")
    assert seen[1]["json"] == {"target_user_id": "77"}


# ======================================================================
# API client: authentication, permission and rate-limit failures
# ======================================================================
@pytest.mark.parametrize(
    "status,expected_category",
    [
        (401, "X_AUTH_FAILED"),
        (403, "X_PERMISSION_DENIED"),
        (429, "X_RATE_LIMITED"),
        (404, "X_NOT_FOUND"),
        (400, "X_INVALID_REQUEST"),
        (503, "X_SERVER_ERROR"),
    ],
)
def test_status_codes_map_to_distinct_categories(status, expected_category):
    category, _retryable = classify(status)
    assert category == expected_category


async def test_401_is_reported_as_reauthorization_not_a_network_error(monkeypatch):
    fake_transport(monkeypatch, lambda *a, **k: FakeResponse(401, error_body(401, "invalid token")))
    result = await a_client().create_post("hi")

    assert result.ok is False
    assert result.error_category == "X_AUTH_FAILED"
    # X answered, so the write definitively did NOT apply. Reporting it as
    # uncertain would make the caller verify something that never happened.
    assert result.uncertain is False
    assert "authorized again" in result.error


async def test_403_does_not_report_the_operation_as_done(monkeypatch):
    fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(403, error_body(403, "you are not allowed", "not-authorized")),
    )
    result = await a_client().follow_user("42", "77")

    assert result.ok is False
    assert result.error_category == "X_PERMISSION_DENIED"
    assert result.uncertain is False
    assert "app, the account, or this API plan" in result.error


async def test_429_reports_a_wait_and_never_the_token(monkeypatch):
    fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(
            429, error_body(429, "rate limited"), {"retry-after": "42", "x-rate-limit-remaining": "0"}
        ),
    )
    result = await a_client().create_post("hi")

    assert result.error_category == "X_RATE_LIMITED"
    assert result.retry_after_seconds == 42
    assert result.rate_limit_remaining == 0
    assert "user-token" not in result.error


async def test_no_token_never_produces_a_request(monkeypatch):
    seen = fake_transport(monkeypatch, lambda *a, **k: FakeResponse(200, {}))
    result = await XApiClient(access_token="").whoami()

    assert result.ok is False
    assert result.error_category == "X_AUTH_FAILED"
    assert seen == []


# ======================================================================
# Timeouts and uncertain writes
# ======================================================================
async def test_write_timeout_is_uncertain_not_a_clean_failure(monkeypatch):
    async def boom(*a, **k):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(
        "backend.app.integrations.x.client.httpx.AsyncClient",
        type("C", (), {"__init__": lambda s, *a, **k: None}),
    )

    class TimingOutClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **k):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("backend.app.integrations.x.client.httpx.AsyncClient", TimingOutClient)
    result = await a_client().create_post("maybe it posted")

    assert result.ok is False
    assert result.uncertain is True
    assert result.error_category == "X_TIMEOUT_UNCERTAIN"
    assert "UNKNOWN" in result.error
    assert "before retrying" in result.error


async def test_read_timeout_is_not_uncertain(monkeypatch):
    class TimingOutClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **k):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("backend.app.integrations.x.client.httpx.AsyncClient", TimingOutClient)
    result = await a_client().read_post("12345")

    assert result.ok is False
    assert result.uncertain is False


# ======================================================================
# Verification reads
# ======================================================================
async def test_liked_posts_reports_a_definitive_answer(monkeypatch):
    fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(200, {"data": [{"id": "1"}, {"id": "999"}]}),
    )
    result = await a_client().liked_posts("42", "999")
    assert result.ok and result.data["liked"] is True

    fake_transport(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": [{"id": "1"}]}))
    result = await a_client().liked_posts("42", "999")
    assert result.ok and result.data["liked"] is False


async def test_is_following_checks_the_real_list(monkeypatch):
    fake_transport(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": [{"id": "77"}]}))
    result = await a_client().is_following("42", "77")
    assert result.ok and result.data["following"] is True


# ======================================================================
# OAuth helpers
# ======================================================================
def test_authorize_url_requests_pkce_and_explicit_scopes():
    pkce = generate_pkce()
    url = build_authorize_url(
        client_id="cid", redirect_uri="https://app/cb", code_challenge=pkce["code_challenge"], state="st"
    )
    assert url.startswith("https://api.x.com/2/oauth2/authorize?")
    assert "code_challenge_method=S256" in url
    assert "code_challenge=" in url
    assert "state=st" in url
    for scope in AUTHORIZATION_SCOPES:
        assert scope in url
    assert "offline.access" in url


def test_pkce_verifier_is_random_and_long_enough():
    a, b = generate_pkce(), generate_pkce()
    assert a["code_verifier"] != b["code_verifier"]
    assert 43 <= len(a["code_verifier"]) <= 128


# ======================================================================
# Credential encryption
# ======================================================================
def test_sealed_credentials_are_not_readable_and_round_trip(monkeypatch):
    monkeypatch.setattr(crypto, "_master_key", lambda: b"\x01" * 32)
    vault = crypto.CredentialVault("row-1")
    envelope = vault.seal({"client_secret": "super-secret", "access_token": "tok"})

    assert "super-secret" not in envelope
    assert "tok" not in envelope
    assert envelope.startswith("xcv1.")
    assert vault.open(envelope) == {"client_secret": "super-secret", "access_token": "tok"}


def test_a_bundle_copied_from_another_row_does_not_open(monkeypatch):
    monkeypatch.setattr(crypto, "_master_key", lambda: b"\x01" * 32)
    envelope = crypto.CredentialVault("row-1").seal({"access_token": "tok"})

    with pytest.raises(crypto.DecryptionFailed) as exc:
        crypto.CredentialVault("row-2").open(envelope)
    assert "does not belong" in str(exc.value)


def test_a_bundle_sealed_with_another_key_does_not_open(monkeypatch):
    monkeypatch.setattr(crypto, "_master_key", lambda: b"\x01" * 32)
    envelope = crypto.CredentialVault("row-1").seal({"access_token": "tok"})

    monkeypatch.setattr(crypto, "_master_key", lambda: b"\x02" * 32)
    with pytest.raises(crypto.DecryptionFailed):
        crypto.CredentialVault("row-1").open(envelope)


def test_tampered_ciphertext_is_rejected(monkeypatch):
    monkeypatch.setattr(crypto, "_master_key", lambda: b"\x01" * 32)
    vault = crypto.CredentialVault("row-1")
    envelope = vault.seal({"access_token": "tok"})
    head, nonce, blob, fingerprint = envelope.split(".")
    flipped = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")

    with pytest.raises(crypto.DecryptionFailed):
        vault.open(f"{head}.{nonce}.{flipped}.{fingerprint}")


def test_empty_seal_stores_nothing():
    assert crypto.CredentialVault("row-1").seal({}) == ""


def test_vault_refuses_the_public_development_key_in_production(monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SECRET_KEY", crypto._DEV_SECRET_KEY)
    monkeypatch.setattr(settings, "WALLET_VAULT_MASTER_KEY", "")

    assert crypto.vault_ready() is False
    status = crypto.vault_status()
    assert status["ready"] is False and "WALLET_VAULT_MASTER_KEY" in status["detail"]


# ======================================================================
# Redaction
# ======================================================================
def test_redaction_hides_every_secret_shaped_key():
    payload = {
        "client_secret": "s3cr3t-value",
        "access_token": "aaaa-value",
        "refresh_token": "rrrr-value",
        "Authorization": "Bearer bearer-value",
        "nested": {"api_key": "kkk-value", "safe": "visible"},
        "list": [{"password": "ppp-value"}],
    }
    clean = crypto.redact(payload)
    blob = json.dumps(clean)
    for secret in ("s3cr3t-value", "aaaa-value", "rrrr-value", "bearer-value", "kkk-value", "ppp-value"):
        assert secret not in blob
    assert clean["nested"]["safe"] == "visible"


def test_mask_never_reveals_the_value():
    assert "abcdefgh" not in crypto.mask("abcdefgh")
    assert crypto.mask("") == ""
    # Length only: no prefix and no suffix of the value itself.
    masked = crypto.mask("abcdefgh")
    assert "ab" not in masked and "gh" not in masked
    assert "8 chars" in masked


def test_assert_no_secrets_refuses_an_unmasked_secret():
    crypto.assert_no_secrets({"post_id": "1", "url": "https://x.com"})
    with pytest.raises(ValueError):
        crypto.assert_no_secrets({"access_token": "leaked"})


# ======================================================================
# Credential validation and bulk import
# ======================================================================
def test_a_row_without_a_client_id_is_rejected():
    with pytest.raises(creds.CredentialError) as exc:
        creds.normalize_row({"name": "Main"})
    assert exc.value.field == "client_id"


def test_a_row_with_no_secret_at_all_is_rejected():
    with pytest.raises(creds.CredentialError) as exc:
        creds.normalize_row({"name": "Main", "client_id": "cid"})
    assert exc.value.field == "client_secret"


def test_an_access_token_alone_is_accepted_with_a_warning():
    clean, warnings = creds.normalize_row({"name": "Main", "client_id": "cid", "access_token": "t" * 40})
    assert clean["access_token"]
    assert any("cannot be refreshed" in w for w in warnings)


def test_field_aliases_are_normalised():
    clean, _ = creds.normalize_row(
        {"label": "Main", "api_key": "cid", "api_secret": "s" * 30, "handle": "@someone"}
    )
    assert clean["name"] == "Main" and clean["client_id"] == "cid"
    assert clean["username"] == "someone"


def test_a_swapped_key_and_secret_is_flagged():
    _, warnings = creds.normalize_row(
        {"name": "Main", "client_id": "A" * 45, "client_secret": "short-one", "access_token": ""}
    )
    assert any("swapped" in w for w in warnings)


def test_malformed_username_is_rejected():
    with pytest.raises(creds.CredentialError) as exc:
        creds.normalize_row({"name": "M", "client_id": "c", "client_secret": "s", "username": "not a handle!"})
    assert exc.value.field == "username"


def test_json_and_csv_import_produce_the_same_rows():
    rows_json = creds.parse_document('[{"name":"A","client_id":"c","client_secret":"s"}]', filename="x.json")
    rows_csv = creds.parse_document("name,client_id,client_secret\nA,c,s\n", filename="x.csv")
    assert len(rows_json) == len(rows_csv) == 1
    assert rows_json[0]["name"] == "A" and rows_csv[0]["name"] == "A"


def test_malformed_json_is_rejected_with_a_position():
    with pytest.raises(creds.ImportError_) as exc:
        creds.parse_document('[{"name": }]', filename="x.json")
    assert "line" in str(exc.value)


def test_an_empty_document_is_rejected():
    with pytest.raises(creds.ImportError_):
        creds.parse_document("   ", filename="x.json")


def test_preview_reports_duplicates_and_invalid_rows_without_saving():
    rows = [
        {"name": "A", "client_id": "c", "client_secret": "s"},
        {"name": "A", "client_id": "c", "client_secret": "s"},          # duplicate in file
        {"name": "Taken", "client_id": "c", "client_secret": "s"},      # duplicate in db
        {"name": "", "client_id": "c", "client_secret": "s"},           # invalid
    ]
    preview = creds.preview_import(rows, existing_names=["Taken"])
    assert preview.valid_count == 1
    assert "A" in preview.duplicates_in_file
    assert "Taken" in preview.duplicates_in_database
    assert any(e["field"] == "name" for e in preview.errors)


def test_preview_never_exposes_a_secret_in_its_response():
    preview = creds.preview_import([{"name": "A", "client_id": "cid", "client_secret": "TOPSECRET"}])
    blob = json.dumps(preview.as_dict())
    assert "TOPSECRET" not in blob
    assert "cid" not in blob  # only the masked hint travels
    # …but the commit path can still get it, from memory.
    assert preview.secrets_for(preview.rows[0]["row"])["client_secret"] == "TOPSECRET"
    assert preview.client_id_for(preview.rows[0]["row"]) == "cid"


def test_only_one_default_may_be_imported_at_once():
    rows = [
        {"name": "A", "client_id": "c", "client_secret": "s", "default": True},
        {"name": "B", "client_id": "c", "client_secret": "s", "default": True},
    ]
    preview = creds.preview_import(rows)
    assert any(e["field"] == "default" for e in preview.errors)
    assert preview.valid_count == 1


# ======================================================================
# Routing
# ======================================================================
def test_auto_sends_a_write_to_the_api_when_it_is_authorized():
    decision = routing.choose_method(
        "like", api_available=True, browser_available=True
    )
    assert decision.method == routing.API
    assert decision.ok


def test_auto_falls_back_to_the_browser_when_no_api_account_exists():
    decision = routing.choose_method(
        "like", api_available=False, api_unavailable_reason="no authorized account",
        browser_available=True,
    )
    assert decision.method == routing.BROWSER
    assert decision.fallback is True
    assert "no authorized account" in decision.reason


def test_browser_preferred_sends_a_read_to_the_browser():
    decision = routing.choose_method(
        "read_profile", preference=routing.MODE_BROWSER_PREFERRED, api_available=True, browser_available=True
    )
    assert decision.method == routing.BROWSER


def test_api_preferred_still_reports_when_the_api_cannot_do_it():
    decision = routing.choose_method(
        "like", preference=routing.MODE_API_PREFERRED, api_available=False,
        api_unavailable_reason="rate limited", browser_available=False,
        browser_unavailable_reason="no browser profile",
    )
    assert decision.ok is False
    assert "rate limited" in decision.blocked_reason
    assert "no browser profile" in decision.blocked_reason


def test_a_task_phrased_as_website_work_goes_to_the_browser_even_in_api_preferred_mode():
    decision = routing.choose_method(
        "read_profile",
        preference=routing.MODE_API_PREFERRED,
        task_text="Open X.com and inspect this profile page",
        api_available=True,
        browser_available=True,
    )
    assert decision.method == routing.BROWSER


def test_a_task_phrased_as_an_api_call_goes_to_the_api_even_in_browser_preferred_mode():
    decision = routing.choose_method(
        "like",
        preference=routing.MODE_BROWSER_PREFERRED,
        task_text="like this post via the API",
        api_available=True,
        browser_available=True,
    )
    assert decision.method == routing.API


def test_an_unknown_operation_is_refused_by_name():
    decision = routing.choose_method("dm_someone", api_available=True, browser_available=True)
    assert decision.ok is False
    assert "read_profile" in decision.blocked_reason


def test_every_operation_declares_its_api_scope():
    for name, op in routing.OPERATIONS.items():
        if op.api:
            assert op.api_scope, f"{name} claims API support with no scope declared"


# ======================================================================
# Fingerprints / duplicate prevention
# ======================================================================
def test_identical_posts_produce_the_same_fingerprint():
    a = service.operation_key("create_post", "api:Main", service.post_target("hello world"))
    b = service.operation_key("create_post", "api:Main", service.post_target("hello world"))
    assert a == b


def test_the_fingerprint_does_not_leak_the_post_text():
    key = service.operation_key("create_post", "api:Main", service.post_target("my secret announcement"))
    assert "my secret announcement" not in key
    assert "my" not in key


def test_different_accounts_get_different_fingerprints():
    target = service.post_target("hello")
    assert service.operation_key("create_post", "api:A", target) != service.operation_key(
        "create_post", "api:B", target
    )


def test_different_text_produces_different_fingerprints():
    assert service.operation_key("create_post", "api:A", service.post_target("one")) != (
        service.operation_key("create_post", "api:A", service.post_target("two"))
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1234567890123456789", "1234567890123456789"),
        ("https://x.com/someone/status/1234567890123456789", "1234567890123456789"),
        ("https://twitter.com/someone/status/1234567890123456789?s=20", "1234567890123456789"),
    ],
)
def test_post_ids_are_accepted_as_ids_or_urls(raw, expected):
    assert service.parse_post_id(raw) == expected


def test_a_non_post_url_is_refused_with_the_value():
    with pytest.raises(service.XError) as exc:
        service.parse_post_id("https://x.com/home")
    assert "not a post id" in str(exc.value)


def test_handles_are_normalised_and_validated():
    assert service.parse_handle("@someone") == "someone"
    with pytest.raises(service.XError):
        service.parse_handle("way too long to be a handle")


# ======================================================================
# Database-backed behaviour
# ======================================================================
async def _make_config(factory, **kwargs) -> XApiConfiguration:
    session = factory()
    try:
        payload = {
            "name": kwargs.pop("name", "Main"),
            "client_id": "cid",
            "client_secret": "s" * 40,
            "access_token": "t" * 40,
            "refresh_token": "r" * 40,
        }
        payload.update(kwargs)
        row = await store.create_configuration(session, payload, actor="tester")
    finally:
        await session.close()
    return row


async def test_multiple_configurations_coexist_and_only_one_is_default(factory):
    a = await _make_config(factory, name="A")
    b = await _make_config(factory, name="B")
    session = factory()
    try:
        await store.set_default_configuration(session, str(b.id))
        rows = await store.list_configurations(session)
        defaults = [r.name for r in rows if r.is_default]
        assert defaults == ["B"]
        assert len(rows) == 2
    finally:
        await session.close()
    assert a.name == "A"


async def test_a_duplicate_name_is_refused(factory):
    await _make_config(factory, name="Same")
    session = factory()
    try:
        with pytest.raises(store.StoreError):
            await store.create_configuration(
                session, {"name": "Same", "client_id": "c", "client_secret": "s"}
            )
    finally:
        await session.close()


async def test_the_public_projection_never_contains_a_secret(factory):
    row = await _make_config(factory)
    session = factory()
    try:
        public = store.public_configuration(row)
    finally:
        await session.close()
    blob = json.dumps(public)
    assert "s" * 40 not in blob
    assert "t" * 40 not in blob
    assert "r" * 40 not in blob
    assert public["has_access_token"] is True
    assert public["client_secret_mask"] == "••••"


async def test_editing_without_a_secret_keeps_the_stored_one(factory):
    row = await _make_config(factory)
    session = factory()
    try:
        before = store.decrypt_credentials(row)
        updated = await store.update_configuration(session, str(row.id), {"description": "renamed"})
        after = store.decrypt_credentials(updated)
        assert after["access_token"] == before["access_token"]
        assert after["client_secret"] == before["client_secret"]
    finally:
        await session.close()


async def test_a_new_secret_replaces_only_that_secret(factory):
    row = await _make_config(factory)
    session = factory()
    try:
        updated = await store.update_configuration(session, str(row.id), {"access_token": "new" * 40})
        values = store.decrypt_credentials(updated)
        assert values["access_token"] == "new" * 40
        assert values["client_secret"] == "s" * 40
        # Replacing credentials invalidates what we believed about the account.
        assert updated.status == CONFIG_UNVERIFIED
    finally:
        await session.close()


async def test_selection_prefers_the_default_configuration(factory):
    await _make_config(factory, name="A")
    b = await _make_config(factory, name="B")
    session = factory()
    try:
        await store.set_default_configuration(session, str(b.id))
        chosen, reason = await store.select_configuration(session)
        assert chosen is not None and chosen.name == "B"
        assert "default" in reason
    finally:
        await session.close()


async def test_selection_skips_a_configuration_with_no_authorized_account(factory):
    await _make_config(factory, name="NoToken", access_token="")
    await _make_config(factory, name="WithToken")
    session = factory()
    try:
        chosen, reason = await store.select_configuration(session)
        assert chosen is not None and chosen.name == "WithToken"
        assert "first authorized configuration" in reason
    finally:
        await session.close()


async def test_selection_explains_itself_when_nothing_is_usable(factory):
    session = factory()
    try:
        chosen, reason = await store.select_configuration(session)
        assert chosen is None
        assert "No X API configuration" in reason
    finally:
        await session.close()


async def test_a_disabled_configuration_cannot_be_selected_explicitly(factory):
    row = await _make_config(factory, enabled=False)
    session = factory()
    try:
        chosen, reason = await store.select_configuration(session, str(row.id))
        assert chosen is None and "disabled" in reason
    finally:
        await session.close()


# ======================================================================
# Activity history and duplicate guard
# ======================================================================
async def test_activity_records_persist_and_never_store_a_secret(factory):
    session = factory()
    try:
        row = await store.record_activity(
            session,
            method="api",
            operation="create_post",
            status="SUCCESS",
            target="text:abc",
            operation_key="create_post:api:Main:abc",
            result_summary={"post_id": "1889", "access_token": "leaked"},
        )
        blob = json.dumps(store.public_activity(row))
        assert "leaked" not in blob
        assert "1889" in blob
        assert store.public_activity(row)["outcome_uncertain"] is False
    finally:
        await session.close()


async def test_the_duplicate_guard_stops_a_repeated_write(factory):
    session = factory()
    try:
        await store.record_activity(
            session, method="api", operation="create_post", status="SUCCESS",
            target="text:abc", operation_key="k1", result_summary={"post_id": "1889"},
        )
        found = await store.find_recent(session, "k1")
        assert found is not None and found.status == "SUCCESS"
    finally:
        await session.close()


async def test_an_uncertain_write_is_found_separately_from_a_success(factory):
    session = factory()
    try:
        await store.record_activity(
            session, method="api", operation="create_post", status="UNCERTAIN",
            target="text:abc", operation_key="k2", outcome_uncertain=True,
        )
        assert await store.find_recent(session, "k2") is None
        pending = await store.find_uncertain(session, "k2")
        assert pending is not None and pending.outcome_uncertain is True
    finally:
        await session.close()


async def test_resolving_uncertainty_closes_the_record(factory):
    session = factory()
    try:
        row = await store.record_activity(
            session, method="api", operation="create_post", status="UNCERTAIN",
            operation_key="k3", outcome_uncertain=True,
        )
        closed = await store.resolve_uncertain(session, str(row.id), status="SUCCESS", summary={"post_id": "1889"})
        assert closed is not None
        assert closed.outcome_uncertain is False
        assert await store.find_uncertain(session, "k3") is None
    finally:
        await session.close()


async def test_activity_history_survives_the_configuration_it_came_from(factory):
    row = await _make_config(factory)
    session = factory()
    try:
        await store.record_activity(
            session, method="api", operation="like", status="SUCCESS",
            config=row, operation_key="k4", target="post:1",
        )
        await store.delete_configuration(session, str(row.id))
        remaining = await store.list_activity(session)
        assert len(remaining) == 1
        assert remaining[0].config_name == "Main"
    finally:
        await session.close()


async def test_activity_is_filterable_by_method_and_status(factory):
    session = factory()
    try:
        await store.record_activity(session, method="api", operation="like", status="SUCCESS", operation_key="a")
        await store.record_activity(session, method="browser", operation="read_profile", status="FAILED", operation_key="b")
        assert len(await store.list_activity(session, method="api")) == 1
        assert len(await store.list_activity(session, status="FAILED")) == 1
        assert len(await store.list_activity(session)) == 2
    finally:
        await session.close()


# ======================================================================
# Service-level duplicate guard
# ======================================================================
async def test_guard_refuses_to_resend_a_completed_write(factory):
    session = factory()
    try:
        guard = await service._guard_write(
            session, key="k", allow_duplicate=False, operation="create_post"
        )
        assert guard is None  # nothing to block yet
        await store.record_activity(
            session, method="api", operation="create_post", status="SUCCESS",
            operation_key="k", result_summary={"post_id": "1889"},
        )
        guard = await service._guard_write(
            session, key="k", allow_duplicate=False, operation="create_post"
        )
        assert guard is not None
        assert guard.data["already_completed"] is True
        assert guard.data["original_post_id"] == "1889"
    finally:
        await session.close()


async def test_guard_refuses_to_resend_an_uncertain_write(factory):
    session = factory()
    try:
        await store.record_activity(
            session, method="api", operation="create_post", status="UNCERTAIN",
            operation_key="k", outcome_uncertain=True,
        )
        guard = await service._guard_write(
            session, key="k", allow_duplicate=False, operation="create_post"
        )
        assert guard is not None
        assert guard.uncertain is True
        assert guard.ok is False
        assert "verify" in guard.blocked_reason.lower()
    finally:
        await session.close()


async def test_an_explicit_override_allows_a_deliberate_repeat(factory):
    session = factory()
    try:
        await store.record_activity(
            session, method="api", operation="create_post", status="SUCCESS", operation_key="k"
        )
        guard = await service._guard_write(
            session, key="k", allow_duplicate=True, operation="create_post"
        )
        assert guard is None
    finally:
        await session.close()


# ======================================================================
# Outcome -> ToolResult mapping
# ======================================================================
def test_an_uncertain_write_is_not_retryable():
    from backend.app.tools.x_tools import _to_tool_result

    outcome = service.XOutcome(
        ok=False, operation="create_post", method="api", status=service.UNCERTAIN,
        error="no answer", error_category="X_TIMEOUT_UNCERTAIN", uncertain=True,
    )
    result = _to_tool_result("x_post", outcome)
    assert result.retryable is False
    assert result.ok is False
    assert "UNKNOWN" in (result.error or "").upper()


def test_a_rate_limited_write_stays_retryable():
    from backend.app.tools.x_tools import _to_tool_result

    outcome = service.XOutcome(
        ok=False, operation="create_post", method="api", status=service.FAILED,
        error="rate limited", error_category="X_RATE_LIMITED",
    )
    assert _to_tool_result("x_post", outcome).retryable is True


def test_a_needs_user_outcome_parks_the_task():
    from backend.app.tools.x_tools import _to_tool_result
    from backend.app.tools.base import ToolResultStatus

    outcome = service.XOutcome(
        ok=False, operation="create_post", method="browser", status=service.BLOCKED,
        error="sign in first", error_category="HUMAN_INTERVENTION_REQUIRED", needs_user=True,
    )
    assert _to_tool_result("x_post", outcome).status is ToolResultStatus.NEEDS_USER_ACTION


def test_a_success_carries_its_routing_and_evidence():
    from backend.app.tools.x_tools import _to_tool_result

    outcome = service.XOutcome(
        ok=True, operation="create_post", method="api", status=service.OK,
        data={"post_id": "1889"}, route={"method": "api", "reason": "API is preferred."},
    )
    result = _to_tool_result("x_post", outcome)
    assert result.ok
    assert result.data["method"] == "api"
    assert result.evidence["method"] == "api"


# ======================================================================
# Tool schema and permissions
# ======================================================================
def test_every_x_tool_declares_permissions_for_both_methods():
    from backend.app.tools.x_tools import X_TOOLS
    from backend.app.tools.base import SideEffect

    for tool in X_TOOLS:
        perms = set(tool.required_permissions)
        assert "x.read" in perms or "x.write" in perms, tool.name
        # A tool may reach a browser at runtime, so browser access must be part
        # of what the operator grants before the tool runs at all.
        assert perms & {"browser.read", "browser.interact"}, tool.name
        if tool.side_effect in (SideEffect.WEB_INTERACTION, SideEffect.STATE_MUTATION):
            assert "x.write" in perms, tool.name


def test_write_tools_are_not_claimed_idempotent():
    from backend.app.tools.x_tools import XPostTool, XReplyTool

    assert XPostTool.is_idempotent is False
    assert XReplyTool.is_idempotent is False


def test_the_method_argument_is_constrained_to_the_three_choices():
    from backend.app.tools.x_tools import XPostTool

    assert XPostTool.input_schema["properties"]["method"]["enum"] == ["auto", "api", "browser"]


def test_unknown_parameters_are_rejected_by_the_schema():
    from backend.app.tools.x_tools import XPostTool
    from backend.app.tools.base import ToolValidationError

    with pytest.raises(ToolValidationError) as exc:
        XPostTool().validate_params({"text": "hi", "shell_command": "rm -rf /"})
    assert "unexpected parameter" in str(exc.value)


def test_post_text_is_bounded_by_the_schema():
    from backend.app.tools.x_tools import XPostTool
    from backend.app.tools.base import ToolValidationError

    with pytest.raises(ToolValidationError):
        XPostTool().validate_params({"text": "x" * 30_000})


# ======================================================================
# Browser adapter: login state and non-bypass behaviour
# ======================================================================
class FakeLocator:
    def __init__(self, count: int = 0, visible: bool = True) -> None:
        self._count = count
        self._visible = visible

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def wait_for(self, state="visible", timeout=1000):
        return None

    async def click(self, timeout=1000):
        return None

    async def inner_text(self, timeout=1000):
        return ""


class FakePage:
    def __init__(self, url: str = "https://x.com/home", selectors: Dict[str, int] | None = None) -> None:
        self.url = url
        self.selectors = selectors or {}

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.selectors.get(selector, 0))

    async def keyboard(self):  # pragma: no cover - not exercised in these tests
        raise AssertionError("keyboard should not be used by a read-only test")

    async def wait_for_timeout(self, ms):  # pragma: no cover
        return None


@pytest.mark.asyncio
async def test_login_state_reads_the_pages_own_controls(monkeypatch):
    async def fake_page(session_key, **kwargs):
        return FakePage(selectors={browser_adapter.SEL_ACCOUNT_SWITCHER: 1})

    monkeypatch.setattr(browser_adapter, "_page", fake_page)
    assert (await browser_adapter.login_state("p"))["state"] == "LOGGED_IN"

    async def logged_out(session_key, **kwargs):
        return FakePage(selectors={browser_adapter.SEL_LOGIN_BUTTON: 1})

    monkeypatch.setattr(browser_adapter, "_page", logged_out)
    assert (await browser_adapter.login_state("p"))["state"] == "LOGGED_OUT"


@pytest.mark.asyncio
async def test_an_unreadable_page_reports_unknown_not_logged_out(monkeypatch):
    """Signing the operator out of their own account is the worse error."""

    async def fake_page(session_key, **kwargs):
        return FakePage(selectors={})

    monkeypatch.setattr(browser_adapter, "_page", fake_page)
    state = await browser_adapter.login_state("p")
    assert state["state"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_the_adapter_refuses_a_non_x_destination(monkeypatch):
    with pytest.raises(browser_adapter.BrowserXError) as exc:
        await browser_adapter._open("p", "https://evil.example.com/")
    assert "non-X URL" in str(exc.value)


@pytest.mark.asyncio
async def test_a_human_gate_is_raised_not_bypassed(monkeypatch):
    class Observation:
        requires_human = True
        human_reason = "CAPTCHA"

    async def fake_page(session_key, **kwargs):
        return FakePage()

    async def fake_inspect(page):
        return Observation()

    monkeypatch.setattr(browser_adapter, "_page", fake_page)
    monkeypatch.setattr(browser_adapter.controller, "inspect", fake_inspect)
    monkeypatch.setattr(browser_adapter.controller, "navigate", lambda *a, **k: None)

    with pytest.raises(browser_adapter.BrowserGate) as exc:
        await browser_adapter._open("p", "https://x.com/home")
    assert "CAPTCHA" in str(exc.value)


@pytest.mark.asyncio
async def test_a_closed_session_gate_stops_the_adapter(monkeypatch):
    monkeypatch.setattr(browser_adapter, "_gate_reason", lambda key: "Paused by the operator")
    with pytest.raises(browser_adapter.BrowserGate) as exc:
        await browser_adapter._page("p")
    assert "Paused" in str(exc.value)


# ======================================================================
# Regressions found in security review
# ======================================================================
def test_browser_adapter_operations_hold_the_session_interaction_lock():
    """Two X tasks sharing a profile must not interleave on one page.

    ``create_post`` types into the composer and then clicks Post. Without the
    controller's per-session INTERACTION lock (which every browser tool in this
    project takes via ``browser_tools._serialized``), task B could type into task
    A's open composer and A would then publish B's text publicly.
    """
    from backend.app.browser.controller import controller as real_controller

    locked: List[str] = []

    class FakeLock:
        def __init__(self, key):
            self.key = key

        async def __aenter__(self):
            locked.append(self.key)
            return self

        async def __aexit__(self, *exc):
            return False

    monkey = pytest.MonkeyPatch()
    monkey.setattr(real_controller, "interaction_lock", lambda key: FakeLock(key))
    try:
        async def fake_open(session_key, url, *, gate=True):
            raise browser_adapter.BrowserXError("stop here, we only need the lock")

        monkey.setattr(browser_adapter, "_open", fake_open)
        import asyncio

        for call in (
            browser_adapter.read_profile("p1", "someone"),
            browser_adapter.read_post("p1", "12345"),
            browser_adapter.create_post("p1", "text"),
            browser_adapter.like_post("p1", "12345"),
            browser_adapter.unlike_post("p1", "12345"),
            browser_adapter.follow_user("p1", "someone"),
            browser_adapter.unfollow_user("p1", "someone"),
            browser_adapter.like_state_for("p1", "12345"),
            browser_adapter.follow_state_for("p1", "someone"),
        ):
            try:
                asyncio.get_event_loop_policy().new_event_loop().run_until_complete(call)
            except browser_adapter.BrowserXError:
                pass
            except RuntimeError:  # pragma: no cover - loop already closed
                asyncio.run(call)
    finally:
        monkey.undo()

    assert locked, "no operation took the interaction lock"
    assert set(locked) == {"p1"}


def test_every_x_tool_requires_browser_navigate():
    """Revoking ``browser.navigate`` must actually stop X work navigating.

    Every X operation has to change the URL to run in the browser (X has no
    in-page API), and ``bootstrap.py`` deliberately separates ``browser.navigate``
    from ``browser.read`` so an operator can hand out one without the other.
    """
    from backend.app.tools.x_tools import X_TOOLS

    for tool in X_TOOLS:
        assert "browser.navigate" in set(tool.required_permissions), tool.name


async def test_a_rate_limited_configuration_is_refused_until_the_window_rolls_over(factory):
    """The brake has to actually engage.

    It used to require ``rate_limit_remaining is not None`` (X omits that header
    on some 429s) and to have ``rate_limit_reset_at`` populated (nothing ever
    wrote it), so the agent kept calling api.x.com into another 429.
    """
    from datetime import timedelta

    row = await _make_config(factory)
    session = factory()
    try:
        await store.record_check(
            session, row, status="RATE_LIMITED",
            detail="Rate limited by X.",
            rate_limit_remaining=0,
            rate_limit_reset_epoch=int(
                (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
            ),
        )
        readiness = await service._api_readiness(session, None)
        assert readiness["available"] is False
        reason = str(readiness["reason"]).lower()
        assert "rate limited" in reason
        assert "not calling again" in reason
    finally:
        await session.close()


async def test_an_expired_rate_limit_window_lets_the_call_through(factory):
    from datetime import timedelta

    row = await _make_config(factory)
    session = factory()
    try:
        await store.record_check(
            session, row, status="RATE_LIMITED", detail="Rate limited by X.",
            rate_limit_reset_epoch=int(
                (datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp()
            ),
        )
        readiness = await service._api_readiness(session, None)
        assert readiness["available"] is True
        assert readiness["config"] is not None and readiness["config"].name == "Main"
    finally:
        await session.close()


def test_the_duplicate_fingerprint_does_not_depend_on_the_method():
    """Switching from the API to the browser must not defeat the guard."""
    api_config = XApiConfiguration(id=1, name="Main", x_user_id="42", x_username="operator")
    targets = {"config": api_config, "browser_profile_id": "p1"}

    target = service.post_target("the same announcement")
    key = service.operation_key("create_post", service._account_label(targets), target)
    # Same account, same text, whichever method would run it.
    assert service._account_label({"config": api_config, "browser_profile_id": "p1"}) == \
        service._account_label({"config": api_config, "browser_profile_id": "p1"})
    assert key.startswith("create_post:x:42")


def test_renaming_a_configuration_does_not_reset_the_duplicate_window():
    """The fingerprint used the configuration NAME, so a rename cleared it."""
    config = XApiConfiguration(id=1, name="Old Name", x_user_id="42")
    targets = {"config": config, "browser_profile_id": ""}
    before = service._account_label(targets)
    config.name = "New Name"
    assert before == service._account_label(targets)


def test_the_duplicate_override_is_not_reachable_from_any_tool_schema():
    """`allow_duplicate` skips BOTH guard branches, so it must not be model-settable."""
    from backend.app.tools.base import ToolValidationError
    from backend.app.tools.x_tools import X_TOOLS

    for tool in X_TOOLS:
        assert "allow_duplicate" not in (tool.input_schema.get("properties") or {}), tool.name
        with pytest.raises(ToolValidationError):
            tool.validate_params({"allow_duplicate": True})


def test_activity_error_and_target_are_scrubbed_of_token_shaped_text():
    """`error` and `target` are strings, so key-based redaction cannot reach them."""
    token = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    scrubbed = crypto.scrub_text(f"rejected token {token} by X")
    assert token not in scrubbed
    assert crypto.REDACTED in scrubbed


def test_the_vault_refuses_a_placeholder_master_key_in_production(monkeypatch):
    """`core.config` flags placeholder keys; the vault now mirrors that check."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SECRET_KEY", "a" * 64)
    monkeypatch.setattr(settings, "WALLET_VAULT_MASTER_KEY", "replace-with-a-real-key")

    assert crypto.vault_ready() is False


def test_a_deeply_nested_import_document_is_rejected_not_crashed(monkeypatch):
    """Deep nesting inside the 2 MB cap used to escape as an unhandled error.

    Simulated rather than constructed: a literal document deep enough to blow the
    recursion limit takes the interpreter with it, which is not something a test
    should be doing.
    """

    def blow_up(raw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(creds.json, "loads", blow_up)
    with pytest.raises(creds.ImportError_) as exc:
        creds.parse_document('[{"name":"A"}]', filename="x.json")
    assert "nested too deeply" in str(exc.value)


def test_a_csv_error_is_reported_as_a_bad_document(monkeypatch):
    import csv as csv_module

    def boom(*args, **kwargs):
        raise csv_module.Error("field larger than field limit")

    monkeypatch.setattr(creds.csv, "DictReader", boom)
    with pytest.raises(creds.ImportError_) as exc:
        creds.parse_document("name,client_id\nx,y\n", filename="x.csv")
    assert "could not be read" in str(exc.value)


# ======================================================================
# Regressions from the independent source audit
# ======================================================================
def test_declared_scopes_match_the_official_openapi_security_blocks():
    """Transcribed from each endpoint's own `security` block (OpenAPI 2.172).

    The previous table declared `like.read`/`follows.read` as the scopes for the
    LIKE and FOLLOW actions. Those gate the list endpoints, not the actions, and
    the two genuinely required scopes (`users.read`, `tweet.read`) were missing -
    so the operator was shown a wrong scope list on the X page.
    """
    from backend.app.integrations.x.client import OPERATION_SCOPES

    assert set(OPERATION_SCOPES["like"]) == {"like.write", "tweet.read", "users.read"}
    assert set(OPERATION_SCOPES["unlike"]) == {"like.write", "tweet.read", "users.read"}
    assert set(OPERATION_SCOPES["follow"]) == {"users.read", "tweet.read", "follows.write"}
    assert set(OPERATION_SCOPES["unfollow"]) == {"users.read", "tweet.read", "follows.write"}
    assert set(OPERATION_SCOPES["create_post"]) == {"tweet.read", "tweet.write", "users.read"}
    # The list-gating scopes belong to verification only.
    assert set(OPERATION_SCOPES["verify_like"]) == {"like.read", "users.read"}
    assert set(OPERATION_SCOPES["verify_follow"]) == {"follows.read", "users.read"}


def test_no_operation_text_claims_a_paid_tier_gates_writes():
    """X API v2 is pay-per-usage credits; there is no free/paid write tier.

    The previous wording told operators writes "need a paid API tier", which is
    factually wrong and would send them looking for a plan that does not exist.
    """
    catalog = routing.describe_catalog()["operations"]
    blob = " ".join(f"{o['api_caveat']} {o['api_scope']}" for o in catalog).lower()
    for phrase in ("paid tier", "paid api tier", "paid plan", "self-serve plan", "basic plan"):
        assert phrase not in blob, f"stale plan claim in the catalog: {phrase}"
    # The real caveat is cost + credit balance, and it should be present.
    assert any("credit" in str(o["api_caveat"]).lower() for o in catalog)


async def test_a_follow_that_is_only_pending_is_not_reported_as_success(monkeypatch):
    """`POST /2/users/:id/following` returns `pending_follow` for protected accounts."""
    fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(200, {"data": {"following": False, "pending_follow": True}}),
    )
    result = await a_client().follow_user("42", "77")

    assert result.ok is False
    assert result.error_category == "X_POLICY_DENIED"
    assert "pending" in result.error.lower()
    assert result.data["pending_follow"] is True


async def test_a_follow_that_reports_not_following_is_not_reported_as_success(monkeypatch):
    fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(200, {"data": {"following": False, "pending_follow": False}}),
    )
    result = await a_client().follow_user("42", "77")
    assert result.ok is False
    assert "not in the requested state" in result.error


async def test_a_like_that_reports_not_liked_is_not_reported_as_success(monkeypatch):
    fake_transport(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": {"liked": False}}))
    result = await a_client().like_post("42", "999")
    assert result.ok is False
    assert result.error_category == "X_INVALID_REQUEST"


async def test_a_write_with_no_confirming_flag_is_uncertain_not_success(monkeypatch):
    """A 2xx whose body cannot confirm the action is an UNKNOWN outcome."""
    fake_transport(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": {}}))
    result = await a_client().like_post("42", "999")
    assert result.ok is False
    assert result.uncertain is True
    assert "UNKNOWN" in result.error


async def test_create_post_keeps_the_id_when_x_reports_partial_success(monkeypatch):
    """X can return `data` AND `errors`. The post exists; failing it would duplicate it."""
    fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(
            201,
            {
                "data": {"id": "1889", "text": "hello @ghost"},
                "errors": [{"title": "DuplicateRule", "detail": "mention could not be resolved"}],
            },
        ),
    )
    result = await a_client().create_post("hello @ghost")

    assert result.ok is True
    assert result.data["post_id"] == "1889"
    assert result.data["partial"] is True
    assert "mention could not be resolved" in result.data["warnings"][0]


async def test_create_post_without_an_id_is_a_failure(monkeypatch):
    fake_transport(monkeypatch, lambda *a, **k: FakeResponse(201, {"errors": [{"detail": "nope"}]}))
    result = await a_client().create_post("hello")
    assert result.ok is False
    assert "nothing was created" in result.error


async def test_an_incomplete_liked_posts_check_is_not_a_false_negative(monkeypatch):
    """100 most-recent likes is not proof an older like is absent."""
    fake_transport(
        monkeypatch,
        lambda *a, **k: FakeResponse(
            200, {"data": [{"id": "1"}], "meta": {"next_token": "abc"}}
        ),
    )
    result = await a_client().liked_posts("42", "999")
    assert result.ok is True
    assert result.data["liked"] is False
    assert result.data["confidence"] == "PARTIAL"
    assert result.data["list_exhausted"] is False
    assert "NOT proof" in result.data["note"]


async def test_an_exhausted_liked_posts_check_is_a_definitive_negative(monkeypatch):
    fake_transport(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": [{"id": "1"}]}))
    result = await a_client().liked_posts("42", "999")
    assert result.data["confidence"] == "DEFINITIVE"
    assert result.data["list_exhausted"] is True


async def test_verification_pages_stay_within_what_x_accepts(monkeypatch):
    """`liked_tweets` rejects max_results < 5; an out-of-range value is a 400."""
    seen = fake_transport(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": []}))
    await a_client().liked_posts("42", "999", limit=1)
    assert int(seen[0]["params"]["max_results"]) >= 5
    await a_client().liked_posts("42", "999", limit=5000)
    assert int(seen[1]["params"]["max_results"]) <= 100

    await a_client().is_following("42", "77", limit=5000)
    assert int(seen[2]["params"]["max_results"]) <= 1000


def test_a_placeholder_vault_key_is_refused_even_when_it_is_well_formed(monkeypatch):
    """The unsafe-key check used to run only on the SECRET_KEY fall-through.

    `_master_key()` returned a configured vault key without ever consulting
    `_unsafe_key()`, so a placeholder vault key in production was accepted. The
    previous test passed for the wrong reason - it tripped the 64-hex length
    check, not the placeholder check.
    """
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SECRET_KEY", "b" * 64)
    # 64 valid hex characters AND a placeholder prefix: only the placeholder
    # check can catch this.
    monkeypatch.setattr(settings, "WALLET_VAULT_MASTER_KEY", "replace-with-" + "c" * 51)

    assert crypto.vault_ready() is False
    assert crypto._unsafe_key() is True


def test_the_shipped_development_vault_key_is_refused_in_production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SECRET_KEY", "b" * 64)
    monkeypatch.setattr(settings, "WALLET_VAULT_MASTER_KEY", crypto._DEV_VAULT_KEY)
    assert crypto.vault_ready() is False


def test_a_real_vault_key_is_accepted_in_production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SECRET_KEY", "b" * 64)
    monkeypatch.setattr(settings, "WALLET_VAULT_MASTER_KEY", "d" * 64)
    assert crypto.vault_ready() is True


async def _make_config_with_handle(factory, handle: str):
    """A configuration created on THIS session, so it stays persistent."""
    session = factory()
    try:
        row = await store.create_configuration(
            session,
            {
                "name": "Bound",
                "client_id": "cid",
                "client_secret": "s" * 40,
                "access_token": "old" * 20,
                "username": handle,
            },
            actor="tester",
        )
        await session.refresh(row)
        return row, session
    except Exception:
        await session.close()
        raise


async def test_a_token_for_the_wrong_account_is_refused_and_not_stored(factory, monkeypatch):
    """Account binding: an authorization code can be for ANY account.

    Without this check, a configuration set up for `@expected` would silently
    start acting as whatever account X authorized, and the next post would go out
    from the wrong identity.
    """
    row, session = await _make_config_with_handle(factory, "expected")
    try:
        service._remember_pending(str(row.id), "verifier", "st")

        async def fake_whoami(self):
            from backend.app.integrations.x.client import XApiResult

            return XApiResult(
                ok=True, operation="whoami",
                data={"user": {"id": "1", "username": "someoneelse", "name": ""}},
            )

        monkeypatch.setattr("backend.app.integrations.x.client.XApiClient.whoami", fake_whoami)

        async def fake_post(self, url, headers=None, data=None, json=None):
            return FakeResponse(200, {"access_token": "NEWTOKEN", "refresh_token": "rt", "expires_in": 7200})

        monkeypatch.setattr(
            "backend.app.integrations.x.client.httpx.AsyncClient.post", fake_post, raising=False
        )

        with pytest.raises(service.StoreError) as exc:
            await service.complete_authorization(session, str(row.id), "the-code", "st")
        assert "someoneelse" in str(exc.value)
        assert "expected" in str(exc.value)

        # The refused token must NOT have replaced what was stored.
        assert store.decrypt_credentials(row).get("access_token") == "old" * 20
        assert row.status == "AUTH_REQUIRED"
    finally:
        await session.close()


async def test_a_token_for_the_expected_account_is_accepted(factory, monkeypatch):
    row, session = await _make_config_with_handle(factory, "expected")
    try:
        service._remember_pending(str(row.id), "verifier", "st")

        async def fake_whoami(self):
            from backend.app.integrations.x.client import XApiResult

            return XApiResult(
                ok=True, operation="whoami",
                data={"user": {"id": "1", "username": "Expected", "name": ""}},
            )

        monkeypatch.setattr("backend.app.integrations.x.client.XApiClient.whoami", fake_whoami)

        async def fake_post(self, url, headers=None, data=None, json=None):
            return FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 7200})

        monkeypatch.setattr(
            "backend.app.integrations.x.client.httpx.AsyncClient.post", fake_post, raising=False
        )

        result = await service.complete_authorization(session, str(row.id), "the-code", "st")
        assert result["ok"] is True
        assert store.decrypt_credentials(row).get("access_token") == "at"
    finally:
        await session.close()


async def test_an_unverifiable_binding_is_refused_rather_than_stored(factory, monkeypatch):
    """If the identity cannot be confirmed, do not store the token blind."""
    row, session = await _make_config_with_handle(factory, "expected")
    try:
        service._remember_pending(str(row.id), "verifier", "st")

        async def failing_whoami(self):
            from backend.app.integrations.x.client import XApiResult

            return XApiResult(ok=False, operation="whoami", error="rate limited",
                              error_category="X_RATE_LIMITED")

        monkeypatch.setattr("backend.app.integrations.x.client.XApiClient.whoami", failing_whoami)

        async def fake_post(self, url, headers=None, data=None, json=None):
            return FakeResponse(200, {"access_token": "NEWTOKEN", "expires_in": 7200})

        monkeypatch.setattr(
            "backend.app.integrations.x.client.httpx.AsyncClient.post", fake_post, raising=False
        )

        with pytest.raises(service.StoreError) as exc:
            await service.complete_authorization(session, str(row.id), "the-code", "st")
        assert "NOT" in str(exc.value) and "stored" in str(exc.value)
        assert store.decrypt_credentials(row).get("access_token") == "old" * 20
    finally:
        await session.close()


async def test_the_duplicate_window_is_operator_configurable(factory):
    """Without a knob, a legitimate repeat publish had no recourse at all."""
    from backend.app.services.preferences import X_DUPLICATE_WINDOW_HOURS, set_preference

    session = factory()
    try:
        assert await service.duplicate_window_seconds(session) == 6 * 3600
        await set_preference(session, X_DUPLICATE_WINDOW_HOURS, "0")
        assert await service.duplicate_window_seconds(session) == 0
        await set_preference(session, X_DUPLICATE_WINDOW_HOURS, "not-a-number")
        assert await service.duplicate_window_seconds(session) == 6 * 3600
        await set_preference(session, X_DUPLICATE_WINDOW_HOURS, "-5")
        assert await service.duplicate_window_seconds(session) == 0
    finally:
        await session.close()


async def test_a_zero_window_still_refuses_to_repeat_an_uncertain_write(factory):
    """The window relaxes deliberate repeats; it must never relax ambiguity."""
    session = factory()
    try:
        await set_duplicate_window(session, 0)
        await store.record_activity(
            session, method="api", operation="create_post", status="UNCERTAIN",
            operation_key="k", outcome_uncertain=True,
        )
        guard = await service._guard_write(
            session, key="k", allow_duplicate=False, operation="create_post"
        )
        assert guard is not None and guard.uncertain is True
    finally:
        await session.close()


async def test_a_zero_window_allows_a_deliberate_known_duplicate(factory):
    session = factory()
    try:
        await set_duplicate_window(session, 0)
        await store.record_activity(
            session, method="api", operation="create_post", status="SUCCESS",
            operation_key="k", result_summary={"post_id": "1889"},
        )
        guard = await service._guard_write(
            session, key="k", allow_duplicate=False, operation="create_post"
        )
        assert guard is None
    finally:
        await session.close()


async def set_duplicate_window(session, hours) -> None:
    from backend.app.services.preferences import X_DUPLICATE_WINDOW_HOURS, set_preference

    await set_preference(session, X_DUPLICATE_WINDOW_HOURS, str(hours))


def test_the_duplicate_key_no_longer_leaks_a_configuration_name():
    assert "Main" not in service.operation_key("create_post", "x:42", "abc")


def test_the_oauth_state_is_unguessable_and_compared_in_constant_time():
    """It used to be sha256(row_id | wall_clock) - both observable by the caller."""
    import hmac as _hmac
    import inspect as _inspect

    seen = set()
    for _ in range(5):
        service._PENDING.clear()
        seen.add(_next_state())
    assert len(seen) == 5, "state must not repeat"

    src = _inspect.getsource(service._take_pending)
    assert "compare_digest" in src


def _next_state() -> str:
    from backend.app.integrations.x.client import generate_pkce

    service._remember_pending("cfg", generate_pkce()["code_verifier"], secrets_module.token_urlsafe(32))
    return str(list(service._PENDING.values())[0]["state"])


# ======================================================================
# Unverified-by-design checks
# ======================================================================
def test_the_client_only_talks_to_the_official_x_host():
    from backend.app.integrations.x.client import API_BASE

    assert API_BASE == "https://api.x.com"


def test_quote_posts_are_not_silently_supported():
    """Quote-posting needs an Enterprise plan; it must not appear as supported."""
    assert "quote" not in routing.describe_catalog()["operations"][0]["name"]
    assert all(
        "quote" not in op["label"].lower() for op in routing.describe_catalog()["operations"]
    )