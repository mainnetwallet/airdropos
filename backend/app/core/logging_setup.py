"""Shared logging configuration for every AirdropOS process.

The API (``backend.app.main``) and the background worker
(``backend.app.scheduler.worker``) used to configure logging separately and
identically, and both let the ASGI access log run. Two consequences, neither of
them a defect:

* every registered tool logged one line at INFO, in two processes, on every start;
* the dashboard polls a handful of endpoints every few seconds, so the terminal
  filled with one line per request and warnings scrolled past unread.

This module is the one place that decides what is printed, and it is deliberately
narrow about what it silences:

* ``httpx``/``httpcore``/``websockets`` move to WARNING. Those loggers only ever
  emit per-request lines, never warnings or errors.
* the per-request access log is off unless ``settings.ACCESS_LOG`` is true. It is
  a request line, not a warning or an error, so nothing is lost.
* nothing here raises any logger's level above WARNING for the application's own
  loggers. Transaction, authentication, emergency-stop and audit events all log at
  INFO or above and keep working exactly as before.

Security-relevant history is not in this path at all. ``security.auth.write_audit``
writes every login, revocation, emergency-stop change and similar to the
``audit_logs`` table, readable through ``GET /api/v1/audit``. Quietening stdout
therefore hides nothing an auditor relies on; it only stops the dashboard's polling
from burying the lines that matter.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.app.core.config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# Loggers that only ever emit per-request chatter. They are raised to WARNING, never
# above it, so a genuine warning from any of them still reaches the terminal.
_REQUEST_ONLY_LOGGERS = ("httpx", "httpcore", "websockets")

# The ASGI server's access log is a request line by definition; the matching error
# logger is NOT listed here and must keep reporting startup failures.
_ACCESS_LOGGERS = ("uvicorn.access", "hypercorn.access")

_configured = False


def configure_logging(force: bool = False) -> None:
    """Install the AirdropOS log format and quieten request-only loggers.

    Safe to call more than once: the root handler is installed only the first time,
    so an importer that configures logging early cannot double every later line.
    """
    global _configured
    if _configured and not force:
        return
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    for name in _REQUEST_ONLY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if not access_log_enabled():
        for name in _ACCESS_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
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