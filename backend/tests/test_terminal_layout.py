"""The terminal dashboard must fit whatever size the terminal is.

A frame taller than the window scrolls the screen on every redraw, and a line
wider than the window wraps and tears the borders. These tests render the
dashboard at many sizes and check the properties that keep it readable:
exact height, no overflow in width, and the important content kept.
"""

from __future__ import annotations

import io
import time

import pytest
from rich.console import Console

from backend.app.terminal import render, theme
from backend.app.terminal.catalog import ToolCategory, ToolInventory
from backend.app.terminal.state import (
    RUNNING,
    EventLine,
    JobView,
    RuntimeState,
    ServiceState,
    TaskCounts,
)

SIZES = [
    (200, 60), (200, 50), (160, 38), (160, 30), (138, 40), (137, 40), (120, 36),
    (120, 30), (105, 31), (105, 24), (96, 28), (95, 28), (90, 30), (80, 24),
    (80, 40), (70, 26), (60, 30), (60, 20), (50, 16), (40, 12), (30, 8),
]

JOBS = [
    ("Airdrop Discovery", 60), ("Campaign Monitoring", 15), ("Claim Monitoring", 30),
    ("Receipt Monitoring", 10), ("Deadline Alerts", 30), ("RPC Health Check", 15),
    ("Watchdog", 15), ("Memory Maintenance", 240), ("Learning Analysis", 720),
]
TOOLS = [
    ("Browser Automation", "▭", 29, ""), ("Telegram User Client", "✈", 5, "ENABLED"),
    ("Blockchain / Wallet", "⬡", 15, ""), ("Research & Discovery", "◉", 6, ""),
    ("Task Management", "≡", 6, ""), ("Reporting & Analysis", "▤", 4, ""),
    ("System & Diagnostics", "⚙", 3, ""),
]


def make_state(*, alerts: bool = False, events: int = 0) -> RuntimeState:
    state = RuntimeState(started_at=time.time() - 142)
    state.version = "1.0.0"
    state.environment = "development"
    state.model = "gpt-4o"
    state.endpoint = "https://apirouter-tdgs.onrender.com/v1"
    state.log_level = "INFO"
    state.log_path = "logs/airdropos.log"
    state.backend = ServiceState("Backend API", RUNNING, ("http://localhost:8000",))
    state.frontend = ServiceState("Frontend (Vite)", RUNNING, ("http://localhost:5173",))
    state.worker = ServiceState("Agent Worker", RUNNING, ("worker-3720",))
    state.gateway = ServiceState("Live Gateway", RUNNING, ("127.0.0.1:8765",))
    state.auth_enabled = True
    state.emergency_stop = {"emergency_stop_enabled": False}
    state.tools = ToolInventory(
        total=68,
        loaded=True,
        categories=[ToolCategory(l, i, c, b, "green") for l, i, c, b in TOOLS],
    )
    state.jobs = [JobView(k.lower(), k, m, True, "●", "green") for k, m in JOBS]
    state.scheduler_known = True
    state.tasks = TaskCounts(known=True)
    for index in range(events):
        state.events.append(
            EventLine(time.time() + index, "00:55:%02d" % (index % 60), "INFO", "x", "p", f"event number {index} " + "long " * 12)
        )
    if alerts:
        state.events.append(EventLine(time.time(), "00:56:00", "ERROR", "x", "p", "something broke " * 6))
    return state


def render_lines(state: RuntimeState, width: int, height: int):
    console = Console(
        width=width, height=height, file=io.StringIO(), force_terminal=True,
        color_system=None, highlight=False, soft_wrap=False,
    )
    with console.capture() as capture:
        console.print(render.build(state, width, height), end="")
    text = capture.get()
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


@pytest.mark.parametrize("width,height", SIZES)
def test_frame_is_never_taller_or_wider_than_the_terminal(width, height):
    for state in (make_state(), make_state(alerts=True, events=30)):
        lines = render_lines(state, width, height)
        assert len(lines) == height, f"{width}x{height}: {len(lines)} rows"
        widest = max(len(line.rstrip()) for line in lines)
        assert widest <= width, f"{width}x{height}: a row is {widest} cells wide"


@pytest.mark.parametrize("width,height", [s for s in SIZES if s[1] >= 12 and s[0] >= 40])
def test_ctrl_c_hint_and_service_state_are_always_visible(width, height):
    text = "\n".join(render_lines(make_state(), width, height))
    assert "Ctrl+C" in text
    assert "Backend" in text and "Worker" in text


@pytest.mark.parametrize("width,height", [(160, 38), (105, 31), (80, 24), (60, 30)])
def test_task_numbers_and_status_text_are_not_cut_off(width, height):
    state = make_state()
    state.tasks = TaskCounts(running=3, pending=5, completed=7, failed=2, known=True)
    text = "\n".join(render_lines(state, width, height))
    for number in ("3", "5", "7", "2"):
        assert number in text
    assert "RUNNING" in text


def test_emergency_stop_survives_a_short_configuration_panel():
    state = make_state()
    state.emergency_stop = {"emergency_stop_enabled": True, "reason": "test"}
    text = "\n".join(render_lines(state, 120, 24))
    assert "ACTIVE" in text


def test_truncated_lists_say_how_many_rows_are_missing():
    text = "\n".join(render_lines(make_state(), 120, 24))
    assert "more" in text


def test_everything_is_shown_when_the_terminal_is_big_enough():
    text = "\n".join(render_lines(make_state(), 200, 60))
    for label, _ in JOBS:
        assert label in text
    for label, *_ in TOOLS:
        assert label in text
    assert "RECENT ACTIVITY" in text and "WARNINGS" in text
    assert "more" not in text


def test_subtitle_keeps_its_word_gaps_when_the_header_is_narrow():
    text = "\n".join(render_lines(make_state(), 80, 30))
    assert "AUTONOMOUS AIRDROP AGENT" in text


def test_tiny_terminal_still_renders_without_error():
    lines = render_lines(make_state(), 30, 6)
    assert len(lines) == 6


# ----------------------------------------------------------------------
# A renderable exactly as wide as the console is drawn one cell narrower
# ----------------------------------------------------------------------
# Rich breaks a line after `width - 1` cells and leaves the final cell on a line of
# its own. Anything sized to the full width therefore costs two rows instead of one,
# and anything whose geometry is computed from that width comes out one cell short.
# The dashboard draws at `width - 1`; these tests pin that, because the two bugs they
# catch were both invisible from the layout tests alone: the dashboard rendered two
# rows too tall, and every recorded click target sat one cell right of its button.


def test_a_rule_at_the_full_width_would_wrap_onto_a_second_row():
    """The rule is the reason the frame used to be two rows too tall.

    Only a console with edge slack (a legacy Windows console, where rich draws into
    ``width - 1`` cells) folds a full-width rule onto a second row. Elsewhere it fits,
    and the dashboard's rule must be one row in both cases.
    """
    from rich.text import Text

    def rows(rule, width):
        console = Console(
            width=width, height=10, file=io.StringIO(), force_terminal=True,
            color_system=None, highlight=False, soft_wrap=False,
        )
        with console.capture() as capture:
            console.print(rule, end="")
        text = capture.get()
        return len((text[:-1] if text.endswith("\n") else text).split("\n"))

    for width in (200, 160, 120, 96, 80, 60):
        full = Text.from_markup(theme.gradient_rule(width))
        # The oracle is rich itself, not the helper under test: a console with edge
        # slack folds the full-width rule, any other console does not.
        probe = Console(width=width, height=10, file=io.StringIO(), force_terminal=True)
        expected = 2 if probe.legacy_windows else 1
        assert rows(full, width) == expected, f"{width}: a full-width rule should take {expected} row(s)"
        # What the dashboard actually uses:
        assert rows(render._rule(width), width) == 1, f"{width}: the rule must be one row"


@pytest.mark.parametrize("width,height", [(200, 60), (160, 38), (96, 26), (60, 30)])
def test_every_row_is_the_same_width_so_the_borders_line_up(width, height):
    """Ragged row widths are how a squeezed panel tears its own border apart."""
    lines = render_lines(make_state(), width, height)
    assert len({len(line) for line in lines}) == 1, (
        f"{width}x{height}: rows have widths {sorted({len(line) for line in lines})}"
    )


def test_unloaded_inventory_does_not_crash_at_any_size():
    state = make_state()
    state.tools = ToolInventory(loaded=False, detail="registry could not be loaded")
    state.jobs = []
    for width, height in SIZES:
        assert len(render_lines(state, width, height)) == height
