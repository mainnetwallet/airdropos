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

# Scrolling RECENT ACTIVITY. Delivered by KeyReader as these names.
SCROLL_UP, SCROLL_DOWN = "up", "down"
PAGE_UP, PAGE_DOWN = "pgup", "pgdn"
SCROLL_TOP, SCROLL_END = "home", "end"
PAGE_LINES = 10

_WINDOWS_SPECIAL = {"H": SCROLL_UP, "P": SCROLL_DOWN, "I": PAGE_UP, "Q": PAGE_DOWN, "G": SCROLL_TOP, "O": SCROLL_END}
_POSIX_SPECIAL = {"A": SCROLL_UP, "B": SCROLL_DOWN, "H": SCROLL_TOP, "F": SCROLL_END,
                  "5~": PAGE_UP, "6~": PAGE_DOWN, "1~": SCROLL_TOP, "4~": SCROLL_END,
                  "7~": SCROLL_TOP, "8~": SCROLL_END}


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


def _copy_windows_clip(text: str) -> bool:
    """clip.exe, fed UTF-16 with a BOM so non-ASCII text survives."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["clip"], input=("\ufeff" + text).encode("utf-16-le"), check=True,
                       timeout=5, creationflags=flags,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def copy_to_clipboard(text: str) -> str:
    """Put ``text`` on the clipboard. Returns how it was done, or "" if it failed.

    ``"native"`` means the operating system confirmed it. ``"osc52"`` means an
    escape sequence was sent to the terminal, which cannot confirm anything: some
    terminals ignore it, so it is reported differently and never as a sure copy.
    """
    try:
        if os.name == "nt":
            if _copy_windows(text) or _copy_windows_clip(text):
                return "native"
        elif _copy_command(text):
            return "native"
        return "osc52" if _copy_osc52(text) else ""
    except Exception:
        return ""


def save_copy(name: str, text: str) -> str:
    """Write the copied text to ``logs/copy-<name>.txt``. Returns the path or ""."""
    try:
        from backend.app.terminal import events

        events.log_dir()
        path = events.LOG_DIR / f"copy-{name}.txt"
        path.write_text(text + "\n", encoding="utf-8")
        try:
            return str(path.relative_to(events.LOG_DIR.parent))
        except ValueError:
            return str(path)
    except Exception:
        return ""


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
        self.error = ""  # why keys are unavailable, if the reader could not run
        self.received = 0  # key presses seen so far

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
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:60]

    def _raw_console_windows(self) -> None:
        """Stop the console echoing typed characters onto the dashboard.

        Left in line mode, a pressed key is echoed at the cursor (a stray "a"
        appears and is painted over a moment later). Echo and line input are
        switched off on the real console input buffer, keeping Ctrl+C working, and
        switched back on when the dashboard stops.
        """
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        # GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING
        handle = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            return
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.CloseHandle(handle)
            return
        saved = mode.value
        ENABLE_LINE_INPUT, ENABLE_ECHO_INPUT = 0x0002, 0x0004
        kernel32.SetConsoleMode(handle, saved & ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT))

        def restore() -> None:
            kernel32.SetConsoleMode(handle, saved)
            kernel32.CloseHandle(handle)

        self._restore = restore

    def _run_windows(self) -> None:
        import msvcrt

        try:
            self._raw_console_windows()
        except Exception:
            pass  # echo may flicker, but keys still work
        while not self._stop.is_set():
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):  # arrow / function key: a second code follows
                    name = _WINDOWS_SPECIAL.get(msvcrt.getwch())
                    if name:
                        self.keys.put(name)
                    continue
                self.received += 1
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
                if not data:
                    continue
                if data == b"\x1b":
                    self._read_escape(fd, select)
                else:
                    self.keys.put(data.decode("utf-8", errors="ignore").lower())

    def _read_escape(self, fd, select) -> None:
        """Parse ``ESC [ A`` style sequences; a lone ESC is ignored."""
        seq = ""
        while len(seq) < 4:
            ready, _, _ = select.select([fd], [], [], 0.03)
            if not ready:
                return
            ch = os.read(fd, 1).decode("ascii", errors="ignore")
            if not seq and ch in ("[", "O"):
                continue
            seq += ch
            if ch.isalpha() or ch == "~":
                break
        name = _POSIX_SPECIAL.get(seq)
        if name:
            self.keys.put(name)
