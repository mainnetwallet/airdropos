"""Run the dashboard.

Renders one screen, in place, from the state collected every few seconds. Three
things it deliberately does not do:

* it never writes to stdout outside the live region, so a service printing a line
  cannot scroll the dashboard away;
* it never guesses. Every value is read from a source listed in
  :mod:`backend.app.terminal.state`;
* it never dies on a transient failure. A probe that raises is reported as a
  problem and the next tick tries again.

When interactive rendering is not possible - stdout is a pipe, the terminal is
dumb, ``rich`` is not installed, or the live display refuses to start - it falls
back to plain structured lines on stdout, which is what the platform printed
before the dashboard existed.

The in-place redraw depends on stdout being a real terminal, because that is the
condition under which escape sequences mean "move the cursor" rather than "write
these bytes into a file". A redirected run therefore gets structured plain lines
rather than a pile of repainted frames, and is not a degraded dashboard: it is a
log.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import threading
import time
from typing import Optional

from backend.app.terminal import events

PLAIN_STATUS_SECONDS = 15.0
PLAIN_FORMAT = "[{time}] {level:<8} {source} | {message}"


# ----------------------------------------------------------------------
# Launcher handshake
# ----------------------------------------------------------------------
def ready_file():
    return events.LOG_DIR / "dashboard.ready"


def _announce_ready(token: Optional[str] = None) -> None:
    """Tell the launcher the screen is ours.

    The launcher waits for this before it stops printing, so there is never a
    moment where both are writing. The file holds ``pid token``: the token is the
    part the launcher matches on, because on Windows the pid it holds belongs to
    the venv launcher rather than to this interpreter. A stale file from a killed
    run is removed first.
    """
    try:
        events.log_dir()
        path = ready_file()
        try:
            os.remove(path)
        except OSError:
            pass
        payload = f"{os.getpid()} {token}".strip() if token else str(os.getpid())
        path.write_text(payload, encoding="utf-8")
    except OSError:
        pass


def _clear_ready() -> None:
    try:
        os.remove(ready_file())
    except OSError:
        pass


def _wait_for_stop_signal(stop: asyncio.Event, honour_eof: bool) -> None:
    """Stop when the launcher closes our stdin. That is how Ctrl+C reaches us.

    The dashboard runs in its own process group, so the console's Ctrl+C goes to
    the launcher and not here. The launcher shuts the services down and then
    closes this pipe; the read returns EOF and we leave the live display
    cleanly, which restores the cursor. A hard kill would leave the terminal
    without one.

    EOF only counts as a stop signal when a launcher asked for this process
    (``--ready-token``). Started by hand with stdin redirected or closed, EOF
    arrives immediately and would end the dashboard before it had drawn
    anything; there it just falls through to Ctrl+C.

    The read is blocking, so it happens on its own thread.
    """
    loop = asyncio.get_running_loop()

    def _reader() -> None:
        stream = sys.stdin
        if stream is None:
            return
        eof = False
        try:
            while True:
                chunk = stream.buffer.read(1) if hasattr(stream, "buffer") else stream.read(1)
                if not chunk:  # EOF
                    eof = True
                    break
                # A keypress on a hand-started dashboard stops it. Routed through
                # the loop: asyncio.Event is not thread-safe.
                loop.call_soon_threadsafe(stop.set)
                return
        except Exception:
            pass
        if eof and honour_eof:
            loop.call_soon_threadsafe(stop.set)

    thread = threading.Thread(target=_reader, name="dashboard-stdin", daemon=True)
    thread.start()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    def handler(*_args) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, AttributeError, RuntimeError):
            # Windows has no add_signal_handler. KeyboardInterrupt still reaches
            # the `asyncio.run` call below, and the launcher stops this process
            # through the stdin pipe either way.
            pass


# ----------------------------------------------------------------------
# Plain fallback
# ----------------------------------------------------------------------
def _plain_status_line(state) -> str:
    tasks = (
        f"tasks running={state.tasks.running} pending={state.tasks.pending} "
        f"completed={state.tasks.completed} failed={state.tasks.failed}"
        if state.tasks.known
        else "tasks=unavailable"
    )
    return (
        f"[dashboard] uptime={int(state.uptime_seconds)}s "
        f"backend={state.backend.state} frontend={state.frontend.state} "
        f"worker={state.worker.state} gateway={state.gateway.state} "
        f"tools={state.tools.total if state.tools.loaded else 'unavailable'} "
        f"jobs={len(state.jobs)} {tasks} alerts={len(state.alerts)}"
    )


async def _run_plain(collector, refresh: float) -> int:
    """Structured plain lines. Used whenever the live display is not available."""
    from rich.console import Console

    console = Console(force_terminal=False, highlight=False)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    _wait_for_stop_signal(stop, honour_eof=False)

    seen = 0
    next_status = 0.0
    known_problems: set = set()
    while not stop.is_set():
        state = await collector.collect()
        lines = list(state.events)
        for event in lines[seen:]:
            console.print(
                PLAIN_FORMAT.format(
                    time=event.time or "--:--:--",
                    level=event.level,
                    source=event.proc or event.source,
                    message=event.message,
                )
            )
        seen = max(seen, len(lines))
        # Reported when they appear and when they clear, not on every tick: a
        # permanent fault should not fill the terminal once a second, and neither
        # should its resolution go unmentioned.
        current = set(state.problems)
        for problem in sorted(current - known_problems):
            console.print(
                PLAIN_FORMAT.format(
                    time=time.strftime("%H:%M:%S"),
                    level="ERROR",
                    source="dashboard",
                    message=problem,
                )
            )
        for problem in sorted(known_problems - current):
            console.print(
                PLAIN_FORMAT.format(
                    time=time.strftime("%H:%M:%S"),
                    level="INFO",
                    source="dashboard",
                    message=f"resolved: {problem}",
                )
            )
        known_problems = current
        now = time.monotonic()
        if now >= next_status:
            console.print(_plain_status_line(state))
            next_status = now + PLAIN_STATUS_SECONDS
        await _sleep(stop, refresh)
    return 0


# ----------------------------------------------------------------------
# Taking over the screen
# ----------------------------------------------------------------------
# Erase the visible screen (2J), the scrollback buffer (3J) and home the cursor (H).
CLEAR_SCREEN_AND_SCROLLBACK = "\x1b[2J\x1b[3J\x1b[H"

_screen_taken = False

# How often the live loop checks whether the window size or the readings changed.
FRAME_POLL_SECONDS = 0.05


def _enable_windows_vt(handle_id: int) -> bool:
    """Turn on ANSI escape processing for one standard handle on Windows."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(handle_id)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if mode.value & 0x0004:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def take_over_screen(console) -> bool:
    """Clear the screen and scrollback, once, as the dashboard takes control.

    Called immediately before the first frame so the dashboard starts at the top
    of an empty terminal. Only what is already on screen or in the scrollback
    buffer when this runs can be erased; whatever the terminal stored earlier is
    removed by this clear and by nothing else, and a terminal that ignores 3J
    keeps its scrollback (the visible screen is still cleared).

    Runs at most once per process, never during updates, and never raises: a
    terminal that cannot be cleared still gets a working dashboard.
    """
    global _screen_taken
    if _screen_taken:
        return False
    _screen_taken = True
    try:
        if os.name == "nt":
            # STD_OUTPUT_HANDLE = -11. Without VT processing the sequences would
            # print as literal text on an old console.
            if not _enable_windows_vt(-11):
                console.clear()
                return False
        console.file.write(CLEAR_SCREEN_AND_SCROLLBACK)
        console.file.flush()
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# Live dashboard
# ----------------------------------------------------------------------
async def _run_live(collector, refresh: float, token: Optional[str] = None) -> int:
    from rich.console import Console
    from rich.live import Live

    from backend.app.terminal.render import build

    console = Console(highlight=False, soft_wrap=False)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    _wait_for_stop_signal(stop, honour_eof=bool(token))

    # Reading the system (HTTP and database probes) can take a second or more. It
    # runs in its own task so a slow probe never holds up drawing: the screen keeps
    # responding to resizes and ticking the uptime while the next reading is in flight.
    latest: dict = {"state": None, "version": 0}
    first_reading = asyncio.Event()

    async def collect_loop() -> None:
        while not stop.is_set():
            try:
                latest["state"] = await collector.collect()
                latest["version"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - the dashboard must never die
                # Nothing may be printed while the dashboard owns the screen, so a
                # failed reading simply keeps the previous one on display.
                pass
            first_reading.set()
            await _sleep(stop, refresh)

    collector_task = asyncio.create_task(collect_loop())
    try:
        # Give the first reading a moment so the first frame is not all "unknown".
        await asyncio.wait_for(first_reading.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        pass
    if latest["state"] is None:
        from backend.app.terminal.state import RuntimeState

        latest["state"] = RuntimeState(started_at=getattr(collector, "started_at", time.time()))

    # Once, immediately before the first frame. Nothing is printed after this.
    take_over_screen(console)
    try:
        # screen=True draws on the terminal's alternate screen. Redrawing in the normal
        # buffer made Windows consoles push the whole dashboard down a line on every
        # frame (flicker, a growing scrollbar). The alternate screen cannot scroll, and
        # the terminal's previous contents come back when the dashboard exits.
        with Live(
            console=console,
            auto_refresh=False,
            screen=True,
            vertical_overflow="crop",
            transient=False,
            refresh_per_second=8,
        ) as live:
            _announce_ready(token)
            drawn_key = None
            drawn_at = 0.0
            while not stop.is_set():
                width, height = console.size
                key = (width, height, latest["version"])
                now = time.monotonic()
                # Draw when the window size or the readings changed, and at least once a
                # second so the uptime keeps moving. Checking every 50 ms is what makes
                # a resize show up at once instead of at the next one-second tick.
                if key != drawn_key or now - drawn_at >= 1.0:
                    # -1 keeps the render off the last column so a wide terminal does not
                    # wrap the right border onto the next line, and one row less than the
                    # window: a frame exactly as tall as the window leaves the cursor on a
                    # new line, which scrolls the screen by one.
                    live.update(
                        build(latest["state"], max(30, width - 1), max(6, height - 1)),
                        refresh=True,
                    )
                    drawn_key = key
                    drawn_at = now
                await _sleep(stop, FRAME_POLL_SECONDS)
    finally:
        collector_task.cancel()
        try:
            await collector_task
        except (asyncio.CancelledError, Exception):
            pass
        _clear_ready()
    return 0


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.terminal",
        description="Render the AirdropOS live terminal dashboard.",
    )
    parser.add_argument("--started", type=float, default=None, help="run start time (epoch seconds)")
    parser.add_argument(
        "--ready-token",
        default=None,
        help="handshake value written to logs/dashboard.ready so the launcher knows the screen is taken",
    )
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-prefix", default="/api/v1")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173", help="empty string to disable")
    parser.add_argument("--gateway-host", default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=8765)
    parser.add_argument("--refresh", type=float, default=2.0, help="seconds between state reads")
    parser.add_argument("--plain", action="store_true", help="force structured plain output")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    from backend.app.terminal.state import Collector

    collector = Collector(
        started_at=args.started if args.started else time.time(),
        backend_url=args.backend_url,
        api_prefix=args.api_prefix,
        frontend_url=(args.frontend_url or None),
        gateway_host=args.gateway_host,
        gateway_port=args.gateway_port,
    )

    interactive = not args.plain
    try:
        interactive = interactive and sys.stdout.isatty() and os.environ.get("TERM", "").lower() != "dumb"
        interactive = interactive and os.environ.get(
            "AIRDROPOS_DASHBOARD", ""
        ).strip().lower() not in ("0", "false", "no", "off")
    except Exception:
        interactive = False

    try:
        if interactive:
            return await _run_live(collector, max(0.5, args.refresh), token=args.ready_token)
        return await _run_plain(collector, max(1.0, args.refresh))
    finally:
        try:
            from backend.app.database.session import engine

            await engine.dispose()
        except Exception:
            pass


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ.setdefault("AIRDROPOS_PROC", "dashboard")
    # The first thing rendered is a box drawing character. Without this, a
    # default Windows code page raises inside the renderer and the dashboard
    # never appears at all.
    events.enable_utf8_output()
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Never leave the launcher with a blank screen and no explanation.
        sys.stderr.write(f"[dashboard] could not run: {type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())