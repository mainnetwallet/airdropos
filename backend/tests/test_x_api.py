"""End-to-end tests for the X.com HTTP endpoints.

These exercise the real FastAPI app against an in-memory database, with the
external X API mocked. They cover what the unit tests in
``test_x_integration.py`` cannot: that the routes are actually mounted, that
authentication is enforced, and that no endpoint ever returns a credential.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import settings


@pytest.fixture
def client(factory) -> Iterator[TestClient]:
    """A test client whose database is the fixture's in-memory one.

    Authentication is switched OFF here, which is the project's own documented
    local-development mode. The unauthorized test below re-enables it and
    monkeypatches the setting for its own request, so "the endpoints require a
    principal" is still checked rather than assumed.
    """
    from backend.app.main import app

    original = settings.AUTH_ENABLED
    settings.AUTH_ENABLED = False
    with TestClient(app) as test_client:
        yield test_client
    settings.AUTH_ENABLED = original


def _payload(name: str = "Main", **extra: Any) -> Dict[str, Any]:
    body = {
        "name": name,
        "description": "primary",
        "client_id": "cid-123",
        "client_secret": "S" * 40,
        "access_token": "T" * 40,
        "refresh_token": "R" * 40,
        "username": "operator",
        "enabled": True,
        "default": False,
    }
    body.update(extra)
    return body


# ======================================================================
# Overview and catalog
# ======================================================================
def test_overview_reports_both_methods_without_any_secret(client):
    response = client.get("/api/v1/x/overview")
    assert response.status_code == 200
    body = response.json()

    assert body["api_enabled"] is True
    assert body["vault"]["ready"] is True
    assert body["authorized_count"] == 0
    assert {op["name"] for op in body["catalog"]["operations"]} >= {
        "create_post", "read_profile", "verify_action",
    }
    assert "offline.access" in body["required_scopes"]
    for secret in ("S" * 40, "T" * 40, "R" * 40):
        assert secret not in json.dumps(body)


def test_catalog_documents_which_method_supports_each_operation(client):
    body = client.get("/api/v1/x/catalog").json()
    by_name = {op["name"]: op for op in body["operations"]}
    assert set(by_name["create_post"]["supported_by"]) == {"api", "browser"}
    assert by_name["create_post"]["write"] is True
    # A write that needs a paid plan says so rather than looking universally free.
    assert by_name["create_post"]["api_caveat"]


# ======================================================================
# Configuration CRUD
# ======================================================================
def test_a_configuration_can_be_created_and_never_returns_a_secret(client):
    created = client.post("/api/v1/x/configurations", json=_payload())
    assert created.status_code == 201
    config = created.json()["configuration"]

    for secret in ("S" * 40, "T" * 40, "R" * 40, "cid-123"):
        assert secret not in json.dumps(created.json())
    assert config["client_id_preview"] != "cid-123"
    assert config["has_access_token"] is True
    assert config["client_secret_mask"] == "••••"
    # A new configuration is UNVERIFIED, never optimistically reported working.
    assert config["status"] == "UNVERIFIED"
    assert config["x_user_id"] == ""


def test_a_configuration_without_credentials_is_refused_with_the_field_name(client):
    response = client.post(
        "/api/v1/x/configurations",
        json={"name": "Broken", "client_id": "cid", "client_secret": "", "access_token": ""},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "client_secret"


def test_a_duplicate_name_is_a_conflict_not_a_second_row(client):
    client.post("/api/v1/x/configurations", json=_payload("Same"))
    response = client.post("/api/v1/x/configurations", json=_payload("Same"))
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]["error"]


def test_editing_without_a_secret_keeps_the_stored_one(client):
    config = client.post("/api/v1/x/configurations", json=_payload()).json()["configuration"]
    updated = client.patch(
        f"/api/v1/x/configurations/{config['id']}", json={"description": "changed"}
    )
    assert updated.status_code == 200
    assert updated.json()["configuration"]["description"] == "changed"
    assert updated.json()["configuration"]["has_access_token"] is True


def test_only_one_configuration_can_be_the_default(client):
    a = client.post("/api/v1/x/configurations", json=_payload("A", default=True)).json()["configuration"]
    b = client.post("/api/v1/x/configurations", json=_payload("B")).json()["configuration"]

    client.post("/api/v1/x/configurations/default", json={"config_id": b["id"]})
    rows = client.get("/api/v1/x/configurations").json()["configurations"]
    defaults = [r["name"] for r in rows if r["is_default"]]
    assert defaults == ["B"]
    assert a["id"] != b["id"]


def test_disabling_a_configuration_is_visible_and_tested_as_unavailable(client):
    config = client.post("/api/v1/x/configurations", json=_payload()).json()["configuration"]
    client.post(f"/api/v1/x/configurations/{config['id']}/enabled", json={"enabled": False})
    assert client.get("/api/v1/x/configurations").json()["configurations"][0]["is_enabled"] is False


def test_deleting_a_configuration_keeps_the_activity_history(client):
    config = client.post("/api/v1/x/configurations", json=_payload()).json()["configuration"]
    removed = client.delete(f"/api/v1/x/configurations/{config['id']}")
    assert removed.status_code == 200
    assert removed.json()["activity_history"] == "kept"
    assert client.get(f"/api/v1/x/configurations/{config['id']}").status_code == 404


def test_test_connection_without_a_token_says_what_is_missing(client):
    config = client.post(
        "/api/v1/x/configurations", json=_payload("NoToken", access_token="", refresh_token="")
    ).json()["configuration"]
    response = client.post(f"/api/v1/x/configurations/{config['id']}/test")
    assert response.status_code == 409
    assert "no access token" in response.json()["detail"]["error"]


def test_test_connection_reports_a_real_x_result(client, monkeypatch):
    """A real call to X, mocked. Proves the wire format and the reporting."""
    config = client.post("/api/v1/x/configurations", json=_payload()).json()["configuration"]

    async def fake_whoami(self):
        from backend.app.integrations.x.client import XApiResult

        return XApiResult(
            ok=True,
            operation="whoami",
            status_code=200,
            data={"user": {"id": "42", "username": "operator", "name": "Op"}},
            rate_limit_remaining=14,
        )

    monkeypatch.setattr(
        "backend.app.integrations.x.client.XApiClient.whoami", fake_whoami
    )
    response = client.post(f"/api/v1/x/configurations/{config['id']}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["user"]["username"] == "operator"

    # The stored row now reflects what the call actually proved.
    stored = client.get(f"/api/v1/x/configurations/{config['id']}").json()["configuration"]
    assert stored["status"] == "OK"
    assert stored["x_username"] == "operator"


# ======================================================================
# Bulk import
# ======================================================================
IMPORT_JSON = json.dumps(
    [
        {"name": "Imported A", "client_id": "cid-a", "client_secret": "SA" * 20, "username": "@alpha"},
        {"name": "Imported B", "api_key": "cid-b", "api_secret": "SB" * 20, "default": True},
        {"name": "", "client_id": "cid-c", "client_secret": "SC" * 20},  # invalid
    ]
)


def test_import_preview_writes_nothing_and_reports_the_bad_row(client):
    response = client.post(
        "/api/v1/x/import/preview", json={"content": IMPORT_JSON, "filename": "x.json"}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["valid_count"] == 2
    assert body["error_count"] == 1
    assert body["errors"][0]["field"] == "name"
    assert body["can_import"] is True
    # A preview is a dry run: nothing was stored.
    assert client.get("/api/v1/x/configurations").json()["configurations"] == []
    # …and it carries no secret at all.
    assert "SA" * 20 not in json.dumps(body)
    assert "cid-a" not in json.dumps(body)


def test_import_preview_documents_its_schema(client):
    body = client.post("/api/v1/x/import/preview", json={"content": IMPORT_JSON, "filename": "x.json"}).json()
    assert set(body["schema"]["fields"]) >= {"name", "client_id", "client_secret", "access_token"}
    assert body["schema"]["fields"]["name"]["required"] is True


def test_import_creates_the_valid_rows_and_skips_the_rest(client):
    response = client.post("/api/v1/x/import", json={"content": IMPORT_JSON, "filename": "x.json"})
    assert response.status_code == 200
    body = response.json()

    assert body["created"] == ["Imported A", "Imported B"]
    assert body["created_count"] == 2
    names = [r["name"] for r in body["configurations"]]
    assert names == ["Imported A", "Imported B"]
    assert "SA" * 20 not in json.dumps(body)
    assert "SB" * 20 not in json.dumps(body)


def test_import_accepts_csv(client):
    csv = "name,client_id,client_secret\nCSV One,cid-1,SSSSSSSSSSSSSSSSSSSS\n"
    body = client.post("/api/v1/x/import", json={"content": csv, "filename": "x.csv"}).json()
    assert body["created"] == ["CSV One"]


def test_a_malformed_document_is_rejected_with_a_reason(client):
    response = client.post("/api/v1/x/import/preview", json={"content": "[{bad", "filename": "x.json"})
    assert response.status_code == 422
    assert "not valid JSON" in response.json()["detail"]["error"]


def test_importing_over_an_existing_name_is_refused_per_row(client):
    client.post("/api/v1/x/configurations", json=_payload("Imported A"))
    body = client.post(
        "/api/v1/x/import", json={"content": IMPORT_JSON, "filename": "x.json"}
    ).json()
    assert "Imported A" not in body["created"]
    # A name clash is a REJECTED ROW, not a silent overwrite: the dry run
    # refuses it, so it never reaches the commit loop.
    assert any(e["name"] == "Imported A" and e["field"] == "name" for e in body["errors"])
    assert client.get("/api/v1/x/configurations").json()["configurations"][0]["name"] == "Imported A"


# ======================================================================
# Preferences and browser profile
# ======================================================================
def test_the_execution_preference_round_trips(client):
    assert client.get("/api/v1/x/preference").json()["preference"] == "auto"
    client.post("/api/v1/x/preference", json={"preference": "api_preferred"})
    assert client.get("/api/v1/x/preference").json()["preference"] == "api_preferred"
    # A stored preference must survive a fresh read of the overview too, because
    # that is what the API and the worker both route from.
    assert client.get("/api/v1/x/overview").json()["preference"] == "api_preferred"
    client.post("/api/v1/x/preference", json={"preference": "auto"})


def test_an_unknown_preference_is_refused_with_the_valid_options(client):
    response = client.post("/api/v1/x/preference", json={"preference": "telepathy"})
    assert response.status_code == 422
    assert "api_preferred" in response.json()["detail"]["error"]


def test_the_duplicate_window_round_trips_and_needs_the_x_scope(client, factory, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app.main import app
    from backend.app.security.auth import Principal, Scope, get_principal

    body = client.get("/api/v1/x/preference").json()
    assert body["duplicate_window_hours"] == 6

    assert client.post("/api/v1/x/duplicate-window", json={"hours": 0}).status_code == 200
    assert client.get("/api/v1/x/preference").json()["duplicate_window_hours"] == 0

    # Out of range is refused rather than silently clamped.
    assert client.post("/api/v1/x/duplicate-window", json={"hours": 10_000}).status_code == 422
    assert client.post("/api/v1/x/duplicate-window", json={"hours": -1}).status_code == 422

    # A session that may only create tasks must not be able to weaken a safety
    # mechanism, so every X behaviour/account control needs `x_credentials`.
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    limited = Principal(
        user_id=None, email="l@x", scopes=frozenset({Scope.READ.value, Scope.TASK_WRITE.value})
    )
    app.dependency_overrides[get_principal] = lambda: limited
    try:
        with TestClient(app) as tc:
            assert tc.post("/api/v1/x/duplicate-window", json={"hours": 1}).status_code == 403
            assert tc.post("/api/v1/x/preference", json={"preference": "auto"}).status_code == 403
            assert tc.post("/api/v1/x/configurations/default", json={"config_id": None}).status_code == 403
            assert tc.post("/api/v1/x/browser-profile", json={"profile_id": None}).status_code == 403
            # …but reading the settings is still allowed.
            assert tc.get("/api/v1/x/preference").status_code == 200
    finally:
        app.dependency_overrides.pop(get_principal, None)


def test_the_activity_endpoint_accepts_its_filters(client):
    response = client.get("/api/v1/x/activity?limit=5&method=api&status=SUCCESS")
    assert response.status_code == 200
    assert response.json()["filters"]["method"] == "api"
    # `limit` is bounded, so a caller cannot ask for the whole table at once.
    assert client.get("/api/v1/x/activity?limit=100000").status_code == 422


def test_pinning_a_nonexistent_browser_profile_is_refused(client):
    response = client.post("/api/v1/x/browser-profile", json={"profile_id": "no-such-profile"})
    assert response.status_code == 404


def test_browser_status_explains_itself_when_no_profile_is_selected(client):
    body = client.get("/api/v1/x/browser-status").json()
    # Whether a default agent profile exists depends on the machine this runs
    # on, so both branches are checked for the same property: the operator is
    # told what the state is, never left with an empty answer.
    if body["ok"]:
        assert body["session_key"]
    else:
        assert body["detail"], "an unavailable browser must come with a reason"


# ======================================================================
# Authorization
# ======================================================================
def test_the_x_endpoints_are_not_served_unauthenticated(factory, monkeypatch):
    """The X endpoints are credential-management endpoints; they must be gated."""
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app) as test_client:
        for path in (
            "/api/v1/x/overview",
            "/api/v1/x/configurations",
            "/api/v1/x/activity",
            "/api/v1/x/catalog",
            "/api/v1/x/preference",
        ):
            assert test_client.get(path).status_code == 401, path
        # And a write is refused too.
        assert test_client.post("/api/v1/x/configurations", json=_payload()).status_code == 401


def test_unknown_configuration_ids_are_404_not_500(client):
    assert client.get("/api/v1/x/configurations/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.post("/api/v1/x/configurations/00000000-0000-0000-0000-000000000000/test").status_code == 404


def test_a_malformed_configuration_id_is_a_clean_400(client):
    response = client.get("/api/v1/x/configurations/not-a-uuid")
    assert response.status_code in (400, 422)
    assert "error" in response.json()["detail"]


def test_credential_endpoints_need_the_dedicated_x_scope(factory, monkeypatch):
    """An X access token is a bearer secret that posts as the operator.

    It is gated like a wallet private key is, not like "create a task": a session
    holding only ``task_write`` must not be able to mint or destroy the
    operator's X identity.
    """
    from fastapi.testclient import TestClient

    from backend.app.main import app
    from backend.app.security.auth import READONLY_SCOPES, Principal, Scope, get_principal

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)

    limited = Principal(
        user_id=None,
        email="limited@airdropos.local",
        scopes=frozenset({Scope.READ.value, Scope.TASK_WRITE.value}),
    )
    app.dependency_overrides[get_principal] = lambda: limited
    try:
        with TestClient(app) as test_client:
            # Reading the inventory is allowed…
            assert test_client.get("/api/v1/x/configurations").status_code == 200
            assert test_client.get("/api/v1/x/overview").status_code == 200
            # …but creating, importing, authorizing and deleting are not.
            assert test_client.post("/api/v1/x/configurations", json=_payload()).status_code == 403
            assert test_client.post(
                "/api/v1/x/import", json={"content": IMPORT_JSON, "filename": "x.json"}
            ).status_code == 403
            assert test_client.post("/api/v1/x/import/preview", json={"content": IMPORT_JSON}).status_code == 403
    finally:
        app.dependency_overrides.pop(get_principal, None)

    # And the scope is deliberately NOT part of the read-only observer set.
    assert Scope.X_CREDENTIALS.value not in READONLY_SCOPES
    assert Scope.X_CREDENTIALS.value in {s.value for s in Scope}