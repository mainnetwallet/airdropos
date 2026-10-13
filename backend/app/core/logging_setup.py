"""Shared logging configuration for every AirdropOS process.

This module is the one place that decides what is printed, and it is deliberately
narrow about what it silences:

* ``httpx``/``httpcore``/``websockets`` move to WARNING. Those loggers only ever
  emit per-request lines, never warnings or errors.
* the per-request access log is off unless ``settings.ACCESS_LOG`` is true. It is
  a request line, not a warning or an error, so nothing is lost.
* nothing here raises any logger's level above WARNING for the application's own
  loggers. Transaction, authentication, emergency-stop and audit events all log at
  INFO or above and keep working exactly as before.

Where the records go:

``logs/airdropos.log``
    Rotating human-readable diagnostics. Everything at INFO and above, always,
    whatever the terminal is doing.

``logs/airdropos-events.jsonl``
    Structured one-line-per-record stream consumed by the terminal dashboard.
    Only DEBUG is dropped from *this* sink; DEBUG stays in the diagnostic log.

the terminal
    Present only when ``run.py`` has handed the screen to the dashboard
    (``AIRDROPOS_QUIET=1``). Without it the console handler prints exactly as
    before, so a piped run, CI, or a crash before the dashboard starts still
    shows the log.

Security-relevant history is not in this path at all. ``security.auth.write_audit``
writes every login, revocation, emergency-stop change and similar to the
``audit_logs`` table, readable through ``GET /api/v1/audit``. Neither sink hides
anything an auditor relies on; the dashboard only decides where records are
*displayed*, and every message is redacted before it is written.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from backend.app.core.config import settings
from backend.app.terminal import events

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# Loggers that only ever emit per-request chatter. They are raised to WARNING, never
# above it, so a genuine warning from any of them still reaches the terminal.
_REQUEST_ONLY_LOGGERS = ("httpx", "httpcore", "websockets")

# The ASGI server's access log is a request line by definition; the matching error
# logger is NOT listed here and must keep reporting startup failures.
_ACCESS_LOGGERS = ("uvicorn.access", "hypercorn.access")

_configured = False


def _quiet_console() -> bool:
    """True when the terminal dashboard owns the screen and must stay unbroken."""
    return os.environ.get("AIRDROPOS_QUIET", "").strip().lower() in ("1", "true", "yes", "on")


def configure_logging(force: bool = False) -> None:
    """Install the AirdropOS log sinks and quieten request-only loggers.

    Safe to call more than once: handlers are installed only the first time,
    so an importer that configures logging early cannot double every later line.
    """
    global _configured
    if _configured and not force:
        return
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT)
        root.setLevel(level)
    else:
        root.setLevel(level)

    for name in _REQUEST_ONLY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if not access_log_enabled():
        for name in _ACCESS_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    # Rotating diagnostics and the structured event stream, in every process,
    # console or not. Idempotent, so a re-import cannot duplicate every line.
    events.install_handlers(root)

    if _quiet_console():
        # The dashboard renders the activity feed; a console handler here would
        # punch holes through its live region.
        for handler in list(root.handlers):
            if type(handler).__name__ == "StreamHandler" and not isinstance(
                handler, logging.FileHandler
            ):
                root.removeHandler(handler)

    _configured = True


def access_log_enabled() -> bool:
    """Whether one line per HTTP request should be printed.

    Off by default. The dashboard's own polling is the majority of the traffic and
    it is not interesting; set ``ACCESS_LOG=true`` in ``.env`` (or pass
    ``python run.py --access-log``) to turn it back on.
    """
    return bool(getattr(settings, "ACCESS_LOG", False))


def uvicorn_args(extra: Optional[list] = None) -> list:
    """uvicorn CLI flags implied by the logging settings.

    ``--no-access-log`` stops the access-log middleware being installed at all.
    ``configure_logging`` already lowers the ``uvicorn.access`` logger, but uvicorn
    re-applies its own logging config in some launch paths (reload, embedding), so
    the flag is passed explicitly rather than relying on import order. Warnings and
    errors come from a different logger (``uvicorn.error``) and are untouched either
    way.
    """
    args = list(extra or [])
    if not access_log_enabled():
        args.append("--no-access-log")
    return args