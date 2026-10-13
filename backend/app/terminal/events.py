"""Cross-process event stream and rotating diagnostic logs.

The API, the worker, the frontend and the launcher are four separate processes.
Before this module each of them printed straight to the same terminal, so their
lines interleaved, scrolled away, and left no single place to look afterwards.
Two sinks are installed here instead:

``logs/airdropos.log``
    Human-readable, rotating, one line per record. Full diagnostics: everything
    the application logs at INFO and above, in the AirdropOS log format.

``logs/airdropos-events.jsonl``
    One JSON object per line, appended by every process. This is what the
    terminal dashboard renders. It is *structured*: the dashboard never has to
    scrape a line of terminal text to find out what happened, and the launcher
    truncates it once per run so a dashboard shows exactly one run's history.

Design constraints this module has to respect:

* **Never break the application.** Publishing an event is best effort. A full
  disk, a read-only log directory or a failed write must never surface inside a
  tool call, a task execution or the emergency-stop path.
* **Never leak a secret.** Every message is passed through :func:`redact`
  before it reaches either sink, because these files are on disk and the
  dashboard prints them.
* **Never hide a warning.** Only DEBUG is dropped from the event stream (it is
  still in the diagnostic log at DEBUG). Nothing raises a level.
* **Stay importable anywhere.** This module depends on the standard library
  only, so ``run.py`` can publish its own events without importing FastAPI,
  SQLAlchemy or the tool registry.

Multi-process writes use one ``os.write`` of one complete line to a file opened
``O_APPEND``. That is a single append per record, which both Windows and POSIX
serialise for a line well under a page, so records from different processes
cannot interleave inside one another.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# backend/app/terminal/events.py -> backend/app/terminal -> backend/app -> backend -> <root>
ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = ROOT / "logs"
DIAG_LOG = LOG_DIR / "airdropos.log"
EVENT_LOG = LOG_DIR / "airdropos-events.jsonl"

DIAG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# Bounded on purpose: an event that is longer than this is a traceback or a
# pasted response, not an activity line, and truncating it keeps one record to
# one atomic append.
MAX_MESSAGE_CHARS = 600

# Rotation of the diagnostic log. 8 MiB is far more than one run produces; the
# bound exists so a long-lived launcher cannot fill the disk.
MAX_LOG_BYTES = 8 * 1024 * 1024
LOG_ARCHIVES = 3

# Levels below this never reach the event stream. Everything else does.
EVENT_STREAM_LEVEL = logging.INFO

_ENABLED = os.environ.get("AIRDROPOS_EVENT_STREAM", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# ----------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------
# Logs are shown in the browser and in the terminal, so strip anything that looks
# like a credential. Same rules the worker's own log ring uses; the definitions
# live here so there is one list, not two that drift.
_REDACTIONS = [
    (
        re.compile(
            r"(?i)\b(bearer|token|api[_-]?key|secret|password|authorization)"
            r"(\s*[=:]\s*|\s+)(?:(?:bearer|basic)\s+)?[^\s,;'\"]+"
        ),
        r"\1\2***",
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "***"),
    (re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b"), "***"),
    (re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b"), "***"),  # Telegram bot tokens
]


# Terminal control sequences: OSC (hyperlinks, titles) ending in BEL or ESC \,
# CSI (colours, cursor moves), and any other two-byte ESC sequence.
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-Z\\-_]"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_terminal_codes(text: str) -> str:
    """Remove colour/cursor/hyperlink escape sequences and control characters.

    Frontend (Vite) output carries them. Left in, the dashboard would write them
    straight to the terminal: the cell widths no longer match what Rich counted,
    so borders tear and fragments of old frames stay on screen. Never raises.
    """
    try:
        text = _ANSI.sub("", str(text))
        text = text.replace("\t", " ")
        return _CONTROL.sub("", text)
    except Exception:  # pragma: no cover
        return str(text)


def redact(text: str) -> str:
    """Mask anything that looks like a credential. Never raises."""
    try:
        for pattern, replacement in _REDACTIONS:
            text = pattern.sub(replacement, text)
        return text
    except Exception:  # pragma: no cover - redaction must not break a log call
        return "***"


# ----------------------------------------------------------------------
# Publishing
# ----------------------------------------------------------------------
_write_lock = threading.Lock()
_fd: Optional[int] = None


def log_dir() -> Path:
    """The directory both sinks live in, created on demand."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def _event_fd() -> int:
    global _fd
    if _fd is None:
        _fd = os.open(str(EVENT_LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    return _fd


def process_name(default: str = "process") -> str:
    """Which AirdropOS process this is, as named by the launcher."""
    return (os.environ.get("AIRDROPOS_PROC") or default).strip() or default


def reset_event_stream() -> None:
    """Start a fresh event stream. Called once, by the launcher, before any service."""
    try:
        log_dir()
        EVENT_LOG.write_text("", encoding="utf-8")
    except OSError:
        pass


def publish(
    level: str,
    source: str,
    message: str,
    *,
    proc: Optional[str] = None,
) -> None:
    """Append one structured event. Best effort: never raises, never blocks.

    Used for anything that is not a ``logging`` record - the launcher's own
    progress lines, and the frontend's output, which has no Python logger.
    """
    if not _ENABLED:
        return
    record: Dict[str, Any] = {
        "ts": round(time.time(), 3),
        "t": datetime.now().strftime("%H:%M:%S"),
        "level": str(level or "INFO").upper(),
        "source": str(source or ""),
        "proc": proc or process_name(),
        "pid": os.getpid(),
        "message": redact(strip_terminal_codes(message))[:MAX_MESSAGE_CHARS],
    }
    try:
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8", "replace")
    except Exception:  # pragma: no cover - a non-serialisable value in a log call
        return
    with _write_lock:
        try:
            os.write(_event_fd(), line)
        except Exception:
            # A broken event stream must never take the application down. Drop
            # the handle so the next call retries rather than failing forever.
            global _fd
            if _fd is not None:
                try:
                    os.close(_fd)
                except OSError:
                    pass
                _fd = None


def publish_success(source: str, message: str, *, proc: Optional[str] = None) -> None:
    """Record a lifecycle milestone the caller knows it has actually reached.

    This is deliberately not inferred by the dashboard: only the component that
    performed the work can say it succeeded, so "backend started" is published
    after the backend is serving, not on the way there. It is not a ``logging``
    call, so the diagnostic log keeps exactly the lines it had before.
    """
    publish("SUCCESS", source, message, proc=proc)


# ----------------------------------------------------------------------
# logging handlers
# ----------------------------------------------------------------------
class _BestEffortRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating handler that can never fail an emit.

    Several processes append to the same log. When one of them rotates, Windows
    refuses to rename a file another process still holds open; that used to
    propagate into ``logging`` and print a stack trace over the application it
    was supposed to be describing. Rotation failures now fall back to reopening
    the current file and carry on.
    """

    def doRollover(self) -> None:  # pragma: no cover - filesystem dependent
        try:
            super().doRollover()
            return
        except Exception:
            pass
        try:
            if self.stream is not None:
                self.stream.close()
            self.stream = self._open()
        except Exception:
            pass


class EventStreamHandler(logging.Handler):
    """Mirror every ``logging`` record into the structured event stream.

    The handler is process-local; the file is what is shared.
    """

    def __init__(self, level: int = EVENT_STREAM_LEVEL, proc: Optional[str] = None) -> None:
        super().__init__(level=level)
        self.proc = proc

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                message = f"{message} | {type(exc).__name__}: {exc}"
            publish(record.levelname, record.name, message, proc=self.proc)
        except Exception:
            self.handleError(record)


def diagnostic_handler() -> Optional[logging.Handler]:
    """The rotating text log. Returns None if the directory cannot be opened.

    Rotation is owned by the launcher: it sets ``AIRDROPOS_LOG_OWNER`` for the
    processes it starts, and only those rotate. A process launched by hand gets
    the same file, appended to, but never renames it out from under the others.
    """
    if os.environ.get("AIRDROPOS_LOG_FILE", "").strip().lower() in ("0", "false", "no", "off"):
        return None
    owner = os.environ.get("AIRDROPOS_LOG_OWNER", "").strip().lower() in ("1", "true", "yes", "on")
    try:
        log_dir()
        if owner:
            return _BestEffortRotatingFileHandler(
                str(DIAG_LOG),
                maxBytes=MAX_LOG_BYTES,
                backupCount=LOG_ARCHIVES,
                encoding="utf-8",
                delay=False,
            )
        return logging.FileHandler(str(DIAG_LOG), encoding="utf-8", delay=False)
    except OSError:
        # A read-only or missing log directory must not stop the service; the
        # dashboard reports the missing diagnostics instead.
        return None


def install_handlers(root: Optional[logging.Logger] = None, *, console: bool = True) -> None:
    """Attach the diagnostic log and the event stream to ``root``.

    Idempotent: calling it twice does not duplicate every line.
    """
    target = root or logging.getLogger()
    if not any(getattr(h, "_airdropos_diag", False) for h in target.handlers):
        handler = diagnostic_handler()
        if handler is not None:
            handler.setFormatter(logging.Formatter(DIAG_FORMAT))
            handler._airdropos_diag = True  # type: ignore[attr-defined]
            target.addHandler(handler)
    if _ENABLED and not any(isinstance(h, EventStreamHandler) for h in target.handlers):
        target.addHandler(EventStreamHandler())


def enable_utf8_output() -> None:
    """Let box-drawing characters survive on a Windows console.

    The dashboard is drawn with characters outside cp1252, and a launcher also
    prints whatever a child process emitted. Writing one of those to a stream
    still using the OEM code page raises UnicodeEncodeError *inside* the
    renderer, which is a far worse failure than a substituted glyph.

    The code page is switched first; the streams only move to UTF-8 when that
    succeeded or when they already speak it, so a console that refuses the
    switch degrades to replacing a few characters rather than to mojibake
    everywhere.
    """
    switched = False
    if os.name == "nt":
        try:
            import ctypes

            switched = bool(ctypes.windll.kernel32.SetConsoleOutputCP(65001))
        except Exception:
            switched = False
    for stream in (sys.stdout, sys.stderr):
        try:
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if switched or encoding == "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


__all__ = [
    "DIAG_LOG",
    "EVENT_LOG",
    "EventStreamHandler",
    "LOG_DIR",
    "diagnostic_handler",
    "enable_utf8_output",
    "install_handlers",
    "process_name",
    "publish",
    "publish_success",
    "redact",
    "reset_event_stream",
]