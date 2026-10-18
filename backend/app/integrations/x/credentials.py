"""Validation, normalization and bulk import for X API configurations.

Two rules shape everything here:

1. A malformed credential is REJECTED, never silently accepted. A configuration
   that stores an unusable client secret looks working until the moment the
   agent tries to post for the operator.
2. Secrets are held only in memory long enough to encrypt them. Nothing in this
   module returns, logs or echoes a secret, and every error message names the
   field rather than its value.

Import schema (documented, and the only one accepted)
-----------------------------------------------------
JSON, either a bare list or ``{"configurations": [...]}``; CSV with a header row.
Both use the same field names:

===============  ===========================================================
Field            Meaning
===============  ===========================================================
``name``         REQUIRED. Unique display name, 1-120 chars.
``client_id``    REQUIRED. The app's consumer key.
``client_secret``  REQUIRED unless ``access_token`` is given. App secret.
``access_token``  User access token. Optional on import: an operator may
                  prefer to run OAuth from the UI and paste nothing.
``refresh_token``  Refresh token, required to renew without re-consent.
``description``  Optional free text, <= 400 chars.
``enabled``      Optional truthy (``true``/``1``/``yes``/``on``). Default true.
``default``      Optional truthy; at most one row per import may set it.
``username``     Optional ``@handle`` of the authorized account. Verified, not
                 trusted, by ``Test Connection``.
===============  ===========================================================

Aliases are accepted for ergonomics and normalised on the way in
(``api_key`` -> ``client_id``, ``api_secret`` -> ``client_secret``,
``token`` -> ``access_token``, ``label`` -> ``name``).
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.app.integrations.x.crypto import looks_like_secret

MAX_NAME = 120
MAX_DESCRIPTION = 400
MAX_CLIENT_ID = 255
MAX_SECRET = 512
MAX_TOKEN = 2048
MAX_IMPORT_ROWS = 100

TRUTHY = {"1", "true", "yes", "on", "y", "t"}
FALSEY = {"0", "false", "no", "off", "n", "f"}

CANONICAL_FIELDS = (
    "name",
    "description",
    "client_id",
    "client_secret",
    "access_token",
    "refresh_token",
    "username",
    "enabled",
    "default",
)

FIELD_ALIASES = {
    "label": "name",
    "configuration_name": "name",
    "config_name": "name",
    "api_key": "client_id",
    "consumer_key": "client_id",
    "app_key": "client_id",
    "api_secret": "client_secret",
    "consumer_secret": "client_secret",
    "app_secret": "client_secret",
    "token": "access_token",
    "user_access_token": "access_token",
    "access_token_encrypted": "access_token",
    "user_refresh_token": "refresh_token",
    "x_username": "username",
    "handle": "username",
    "active": "enabled",
    "is_enabled": "enabled",
    "preferred": "default",
    "is_default": "default",
}

# A username on X: letters, digits, underscore, up to 15 characters.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# A snowflake id.
ID_RE = re.compile(r"^[0-9]{1,19}$")


class CredentialError(ValueError):
    """One field of one configuration is unusable. Carries the field, not the value."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class ImportError_(ValueError):
    """The uploaded document itself is unusable."""


def normalize_username(value: Any) -> str:
    handle = str(value or "").strip().lstrip("@").strip()
    if not handle:
        return ""
    if not USERNAME_RE.match(handle):
        raise CredentialError("username", "must be 1-15 characters of A-Z, 0-9 or underscore")
    return handle


def validate_id(value: Any, field: str = "id") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise CredentialError(field, "is required")
    if not ID_RE.match(raw):
        raise CredentialError(field, "must be a numeric X id")
    return raw


def _truthy(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSEY or not text:
        return False
    raise CredentialError(field, "must be true or false")


def normalize_row(row: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Canonicalise one import row.

    Returns ``(clean, warnings)``. Raises :class:`CredentialError` on the first
    field that cannot be accepted, because a half-valid credential is worse than
    a rejected one.
    """
    if not isinstance(row, dict):
        raise CredentialError("row", "must be an object")

    clean: Dict[str, Any] = {}
    warnings: List[str] = []

    for raw_key, value in row.items():
        key = str(raw_key or "").strip().lower().replace(" ", "_")
        key = FIELD_ALIASES.get(key, key)
        if key not in CANONICAL_FIELDS:
            warnings.append(f"ignored unknown field '{str(raw_key)[:40]}'")
            continue
        if value is None:
            continue
        if key in ("enabled", "default"):
            clean[key] = _truthy(value, key)
            continue
        text = str(value).strip()
        if text:
            clean[key] = text

    name = str(clean.get("name") or "").strip()
    if not name:
        raise CredentialError("name", "is required")
    if len(name) > MAX_NAME:
        raise CredentialError("name", f"must be at most {MAX_NAME} characters")

    client_id = str(clean.get("client_id") or "").strip()
    if not client_id:
        raise CredentialError("client_id", "is required (the app's consumer key)")
    if len(client_id) > MAX_CLIENT_ID:
        raise CredentialError("client_id", f"must be at most {MAX_CLIENT_ID} characters")

    client_secret = str(clean.get("client_secret") or "").strip()
    access_token = str(clean.get("access_token") or "").strip()
    refresh_token = str(clean.get("refresh_token") or "").strip()

    if not client_secret and not access_token:
        raise CredentialError(
            "client_secret",
            "is required unless an access_token is supplied (with a token only, the app "
            "cannot refresh it later)",
        )
    for field, value, limit in (
        ("client_secret", client_secret, MAX_SECRET),
        ("access_token", access_token, MAX_TOKEN),
        ("refresh_token", refresh_token, MAX_TOKEN),
    ):
        if value and len(value) > limit:
            raise CredentialError(field, f"must be at most {limit} characters")
        if value and re.search(r"\s", value):
            raise CredentialError(field, "must not contain whitespace")

    if refresh_token and not access_token:
        warnings.append("a refresh_token without an access_token is stored but cannot be used yet")
    if access_token and not client_secret:
        # Usable now, but there is no way to renew it: when the access token
        # expires the account has to be authorized again by hand.
        warnings.append(
            "no client_secret, so this access token cannot be refreshed; the account will need "
            "re-authorizing when the token expires"
        )

    if looks_like_secret(client_id) and not looks_like_secret(client_secret):
        # A very common paste error: the secret ends up in the client_id column.
        # Left alone this produces a configuration that can never authenticate and
        # an opaque 401 later, so it is called out at import time.
        warnings.append(
            "client_id looks like a token rather than a consumer key; check that the app "
            "key and app secret were not swapped"
        )

    username = str(clean.get("username") or "").strip()
    if username:
        clean["username"] = normalize_username(username)

    description = str(clean.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION:
        raise CredentialError("description", f"must be at most {MAX_DESCRIPTION} characters")

    clean.setdefault("enabled", True)
    clean.setdefault("default", False)
    clean["name"] = name
    clean["client_id"] = client_id
    clean["description"] = description
    return clean, warnings


def validate_configuration(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Public entry point used by the single-configuration API and by import."""
    return normalize_row(payload)


# ----------------------------------------------------------------------
# Document parsing
# ----------------------------------------------------------------------
def parse_document(text: str, *, content_type: str = "", filename: str = "") -> List[Dict[str, Any]]:
    """Parse a JSON or CSV bulk-import document into raw rows.

    The format is chosen by the file extension first and by the content second,
    because browsers send a generic content type for pasted text.
    """
    raw = (text or "").strip()
    if not raw:
        raise ImportError_("The import document is empty.")
    if len(raw) > 2_000_000:
        raise ImportError_("The import document is larger than 2 MB.")

    name = (filename or "").lower()
    looks_json = name.endswith(".json") or raw[0] in "[{"
    if looks_json:
        return _parse_json(raw)
    if name.endswith(".csv") or "," in raw.splitlines()[0]:
        return _parse_csv(raw)
    raise ImportError_(
        "Could not tell whether this is JSON or CSV. Use a .json or .csv file, or paste a "
        "JSON array."
    )


def _parse_json(raw: str) -> List[Dict[str, Any]]:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImportError_(f"The document is not valid JSON (line {exc.lineno}, column {exc.colno}).") from exc
    except RecursionError as exc:
        # A document nested far deeper than any real credential file, inside the
        # size cap. Rejected as a bad document rather than crashing the request.
        raise ImportError_("The document is nested too deeply to read.") from exc
    if isinstance(body, dict):
        body = body.get("configurations", body.get("configs"))
        if body is None:
            raise ImportError_(
                'A JSON object is only accepted as {"configurations": [...]}.'
            )
    if not isinstance(body, list):
        raise ImportError_("The document must be a list of configurations, or an object with a "
                           "'configurations' list.")
    if len(body) > MAX_IMPORT_ROWS:
        raise ImportError_(f"At most {MAX_IMPORT_ROWS} configurations can be imported at once.")
    return [item for item in body if isinstance(item, dict)]


def _parse_csv(raw: str) -> List[Dict[str, Any]]:
    try:
        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            raise ImportError_("The CSV has no header row.")
        if len(reader.fieldnames) > 40:
            raise ImportError_("The CSV has an implausible number of columns.")
        rows: List[Dict[str, Any]] = []
        for line_no, row in enumerate(reader, start=2):
            if line_no > MAX_IMPORT_ROWS + 1:
                break
            if all((value or "").strip() == "" for value in row.values()):
                continue
            rows.append({k: v for k, v in row.items() if k is not None})
    except csv.Error as exc:
        # A field larger than the csv module's limit, or a quoting error. The
        # document is bad, not the server: report it like any other bad import.
        raise ImportError_(f"The CSV could not be read: {type(exc).__name__}.") from exc
    if not rows:
        raise ImportError_("The CSV contains a header but no data rows.")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ImportError_(f"At most {MAX_IMPORT_ROWS} configurations can be imported at once.")
    return rows


# ----------------------------------------------------------------------
# Dry run
# ----------------------------------------------------------------------
class ImportPreview:
    """The result of validating a bulk import WITHOUT writing anything."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.duplicates_in_file: List[str] = []
        self.duplicates_in_database: List[str] = []
        self.default_conflicts: List[str] = []

    @property
    def valid_count(self) -> int:
        return len(self.rows)

    def as_dict(self) -> Dict[str, Any]:
        """The preview as it goes over the wire.

        ``_secrets`` is stripped HERE, not left to the caller: a preview is
        returned to a browser, and the one field that must never reach one is the
        one that is easiest to forget.
        """
        return {
            "valid_count": self.valid_count,
            "error_count": len(self.errors),
            "rows": [
                {k: v for k, v in row.items() if not k.startswith("_")} for row in self.rows
            ],
            "errors": self.errors,
            "warnings": self.warnings,
            "duplicates_in_file": self.duplicates_in_file,
            "duplicates_in_database": self.duplicates_in_database,
            "default_conflicts": self.default_conflicts,
            "can_import": self.valid_count > 0,
            "schema": IMPORT_SCHEMA_DOC,
        }

    def secrets_for(self, row_index: int) -> Dict[str, str]:
        """The in-memory secrets for one accepted row (1-based row number)."""
        for row in self.rows:
            if row["row"] == row_index:
                return dict(row.get("_secrets") or {})
        raise KeyError(f"No accepted row {row_index} in this preview.")

    def client_id_for(self, row_index: int) -> str:
        """The real client id for one accepted row.

        Not a secret (the preview already shows a masked form of it), but it is
        still withheld from the preview response so the commit reads exactly what
        the operator submitted rather than a reconstruction of it.
        """
        for row in self.rows:
            if row["row"] == row_index:
                return str(row.get("_client_id") or "")
        raise KeyError(f"No accepted row {row_index} in this preview.")


IMPORT_SCHEMA_DOC: Dict[str, Any] = {
    "formats": ["json", "csv"],
    "json_shapes": [
        '[{"name": "...", "client_id": "...", "client_secret": "..."}]',
        '{"configurations": [{"name": "...", ...}]}',
    ],
    "fields": {
        "name": {"required": True, "type": "string", "notes": "unique, 1-120 chars"},
        "client_id": {"required": True, "type": "string", "notes": "the app's consumer key"},
        "client_secret": {
            "required": "unless access_token is given",
            "type": "string",
            "notes": "app secret; required to refresh a token",
        },
        "access_token": {"required": False, "type": "string", "notes": "user access token"},
        "refresh_token": {"required": False, "type": "string", "notes": "needed to renew silently"},
        "username": {"required": False, "type": "string", "notes": "@handle; verified by Test Connection"},
        "description": {"required": False, "type": "string", "notes": "free text"},
        "enabled": {"required": False, "type": "boolean", "default": True},
        "default": {"required": False, "type": "boolean", "default": False},
    },
    "aliases": FIELD_ALIASES,
    "max_rows": MAX_IMPORT_ROWS,
}


def preview_import(
    rows: Iterable[Dict[str, Any]],
    *,
    existing_names: Optional[Iterable[str]] = None,
    current_default: Optional[str] = None,
) -> ImportPreview:
    """Validate rows and report what would happen, without touching the database."""
    preview = ImportPreview()
    known = {str(n).strip().lower() for n in (existing_names or [])}
    seen_in_file: Dict[str, int] = {}
    defaults_requested: List[str] = []

    for index, raw in enumerate(rows, start=1):
        try:
            clean, warnings = normalize_row(raw)
        except CredentialError as exc:
            preview.errors.append(
                {
                    "row": index,
                    "name": _safe_name(raw),
                    "field": exc.field,
                    "message": exc.message,
                }
            )
            continue
        except Exception as exc:  # defensive: never 500 on an operator's file
            preview.errors.append(
                {"row": index, "name": _safe_name(raw), "field": "row", "message": type(exc).__name__}
            )
            continue

        key = clean["name"].lower()
        if key in seen_in_file:
            preview.duplicates_in_file.append(clean["name"])
            preview.errors.append(
                {
                    "row": index,
                    "name": clean["name"],
                    "field": "name",
                    "message": f"already defined earlier in this file (row {seen_in_file[key]})",
                }
            )
            continue
        seen_in_file[key] = index

        if key in known:
            preview.duplicates_in_database.append(clean["name"])
            preview.errors.append(
                {
                    "row": index,
                    "name": clean["name"],
                    "field": "name",
                    "message": "a configuration with this name already exists",
                }
            )
            continue

        if clean.get("default"):
            if current_default:
                preview.default_conflicts.append(current_default)
            defaults_requested.append(clean["name"])

        for message in warnings:
            preview.warnings.append({"row": index, "name": clean["name"], "message": message})

        # The preview deliberately reports WHICH fields are present and never
        # their values. `has_*` is the only thing an operator needs to sanity check.
        preview.rows.append(
            {
                "row": index,
                "name": clean["name"],
                "description": clean.get("description", ""),
                "client_id_preview": _client_id_preview(clean["client_id"]),
                "has_client_secret": bool(clean.get("client_secret")),
                "has_access_token": bool(clean.get("access_token")),
                "has_refresh_token": bool(clean.get("refresh_token")),
                "username": clean.get("username", ""),
                "enabled": bool(clean.get("enabled", True)),
                "default": bool(clean.get("default", False)),
                # Carried in memory for the commit call, never serialised into a
                # response body. `as_dict` strips every underscore-prefixed key,
                # so neither the secrets nor the unmasked client id can reach the
                # browser through a preview.
                "_client_id": clean["client_id"],
                "_secrets": {
                    "client_secret": clean.get("client_secret", ""),
                    "access_token": clean.get("access_token", ""),
                    "refresh_token": clean.get("refresh_token", ""),
                },
            }
        )

    if len(defaults_requested) > 1:
        preview.errors.append(
            {
                "row": 0,
                "name": ", ".join(defaults_requested),
                "field": "default",
                "message": "at most one configuration in an import may be the default",
            }
        )
        preview.rows = [r for r in preview.rows if r["name"] not in defaults_requested[1:]]

    return preview


def _safe_name(row: Any) -> str:
    """The row's name, or an empty string. Never its secrets."""
    if isinstance(row, dict):
        for key in ("name", "label", "configuration_name", "config_name"):
            value = str(row.get(key) or "").strip()
            if value:
                return value[:MAX_NAME]
    return ""


def _client_id_preview(client_id: str) -> str:
    """X consumer keys are ~25 chars; show enough to recognise, not enough to use."""
    text = str(client_id or "")
    if len(text) <= 6:
        return f"{text[:2]}…"
    return f"{text[:5]}…{text[-2:]}"