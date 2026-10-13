"""Keyboard controls and clipboard for the dashboard's two log panels.

Keys (case-insensitive), shown in each panel's bottom border:

* ``a`` copy all of RECENT ACTIVITY      * ``x`` clear RECENT ACTIVITY
* ``w`` copy all of WARNINGS & ERRORS    * ``z`` clear WARNINGS & ERRORS

Everything here is best effort and never raises: a terminal without a usable
keyboard or clipboard still gets a working dashboard.
"""

from __future__ import annotations

import base64
import os
import queue
import shutil
import subprocess
import sys
import threading
from typing import Callable, Iterable, Optional

from backend.app.terminal.state import EventLine

COPY_ACTIVITY = "a"
COPY_ALERTS = "w"
CLEAR_ACTIVITY = "x"
CLEAR_ALERTS = "z"
KEYS = (COPY_ACTIVITY, COPY_ALERTS, CLEAR_ACTIVITY, CLEAR_ALERTS)


# ----------------------------------------------------------------------
# Text for the clipboard
# ----------------------------------------------------------------------
def format_lines(events: Iterable[EventLine]) -> str:
    """Oldest first, one record per line: ``HH:MM:SS LEVEL source | message``."""
    out = []
    for event in events:
        origin = event.proc or event.source
        out.append(f"{event.time or '--:--:--'} {event.level:<8} {origin} | {event.message}")
    return "\n".join(out)


# ----------------------------------------------------------------------
# Clipboard
# ----------------------------------------------------------------------
def _copy_windows(text: str) -> bool:
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):  # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        threading.Event().wait(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def _copy_command(text: str) -> bool:
    candidates = (
        ["pbcopy"],
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    )
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                continue
    return False


def _copy_osc52(text: str) -> bool:
    """Ask the terminal itself to set the clipboard (works over SSH, too)."""
    try:
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        sys.stdout.write(f"\x1b]52;c;{payload}\x07")
        sys.stdout.flush()
        return True
    except Exception:
        return False


def copy_to_clipboard(text: str) -> bool:
    try:
        if os.name == "nt":
            return _copy_windows(text) or _copy_osc52(text)
        return _copy_command(text) or _copy_osc52(text)
    except Exception:
        return False


# ----------------------------------------------------------------------
# Keyboard
# ----------------------------------------------------------------------
class KeyReader:
    """Reads single key presses on a background thread into a queue.

    Windows reads the console directly (``msvcrt``), which works even though the
    dashboard's stdin is the launcher's shutdown pipe. Elsewhere it reads
    ``/dev/tty``; if there is no controlling terminal the reader simply stays
    idle and the dashboard runs without keys.
    """

    def __init__(self) -> None:
        self.keys: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._restore: Optional[Callable[[], None]] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dashboard-keys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._restore:
            try:
                self._restore()
            except Exception:
                pass

    def poll(self) -> Optional[str]:
        try:
            return self.keys.get_nowait()
        except queue.Empty:
            return None

    def _run(self) -> None:
        try:
            if os.name == "nt":
                self._run_windows()
            else:
                self._run_posix()
        except Exception:
            pass

    def _run_windows(self) -> None:
        import msvcrt

        while not self._stop.is_set():
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):  # arrow / function key: skip its second code
                    msvcrt.getwch()
                    continue
                self.keys.put(char.lower())
            else:
                self._stop.wait(0.03)

    def _run_posix(self) -> None:
        import select
        import termios
        import tty

        tty_file = open("/dev/tty", "rb", buffering=0)
        fd = tty_file.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)  # keep ISIG so Ctrl+C still works

        def restore() -> None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            tty_file.close()

        self._restore = restore
        while not self._stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                data = os.read(fd, 1)
                if data:
                    self.keys.put(data.decode("utf-8", errors="ignore").lower())
