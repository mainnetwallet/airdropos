"""The dashboard layout on both kinds of console rich can be talking to.

A legacy Windows console cannot draw into its last column, so rich renders into
``width - 1`` cells there and folds a renderable exactly ``width`` cells wide onto a
second row. Every other terminal draws the full width. ``render._edge_slack`` reads that
from rich; these tests pin the layout on both kinds of console by building the console
with ``legacy_windows`` set explicitly, so they hold on any machine rather than only on
the one the suite happens to run on.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text

from backend.app.terminal import controls, render, theme
from backend.tests.test_terminal_logs import _busy_state

SIZES = [(200, 60), (160, 38), (120, 30), (100, 28), (96, 26), (80, 24), (60, 30)]
LABELS = {
    controls.COPY_ACTIVITY: "[a] Copy", controls.CLEAR_ACTIVITY: "[x] Clear",
    controls.COPY_ALERTS: "[w] Copy", controls.CLEAR_ALERTS: "[z] Clear",
}


def _console(width: int, height: int, legacy: bool) -> Console:
    return Console(
        width=width, height=height, file=io.StringIO(), force_terminal=True,
        color_system=None, highlight=False, soft_wrap=False, legacy_windows=legacy,
    )


def _lines(renderable, width: int, height: int, legacy: bool):
    console = _console(width, height, legacy)
    with console.capture() as capture:
        console.print(renderable, end="")
    text = capture.get()
    return (text[:-1] if text.endswith("\n") else text).split("\n")


@pytest.fixture(params=[False, True], ids=["normal-terminal", "legacy-windows"])
def legacy(request, monkeypatch):
    monkeypatch.setattr(render, "_edge_slack", lambda: int(request.param))
    return request.param


def test_the_helper_agrees_with_what_rich_actually_does():
    """The unpatched helper must report the slack a default console really has."""
    render._edge_slack.cache_clear()
    probe = Console(width=100, height=10, file=io.StringIO(), force_terminal=True, color_system=None)
    full = Text.from_markup(theme.gradient_rule(100))
    observed_extra_rows = len(_lines(full, 100, 10, bool(probe.legacy_windows))) - 1
    assert render._edge_slack() == observed_extra_rows == int(bool(probe.legacy_windows))


@pytest.mark.parametrize("detected, expected", [(True, 1), (False, 0)])
def test_the_helper_follows_rich_legacy_windows_detection(monkeypatch, detected, expected):
    """Exercise both branches of the helper itself, whatever platform this runs on."""
    import rich.console as rich_console

    monkeypatch.setattr(rich_console, "detect_legacy_windows", lambda: detected)
    render._edge_slack.cache_clear()
    try:
        assert render._edge_slack() == expected
    finally:
        render._edge_slack.cache_clear()


@pytest.mark.parametrize("width", [200, 120, 96, 80, 60])
def test_the_rule_is_one_row_and_a_full_width_rule_folds_only_with_slack(legacy, width):
    full = Text.from_markup(theme.gradient_rule(width))
    assert len(_lines(full, width, 10, legacy)) == (2 if legacy else 1)
    assert len(_lines(render._rule(width), width, 10, legacy)) == 1


@pytest.mark.parametrize("width,height", SIZES)
def test_the_frame_fits_the_window_exactly_on_both_consoles(legacy, width, height):
    lines = _lines(render.build(_busy_state(), width, height), width, height, legacy)
    assert len(lines) == height, f"{width}x{height}: {len(lines)} rows"
    assert max(len(line) for line in lines) <= width


@pytest.mark.parametrize("width,height", SIZES)
def test_click_targets_sit_exactly_on_the_drawn_buttons_on_both_consoles(legacy, width, height):
    state = _busy_state()
    lines = _lines(render.build(state, width, height), width, height, legacy)
    assert state.click_targets, f"{width}x{height}: no click targets were registered"
    for first, last, y, action in state.click_targets:
        assert lines[y][first : last + 1] == LABELS[action], (
            f"{width}x{height} {action}: {lines[y][first:last + 1]!r}"
        )
