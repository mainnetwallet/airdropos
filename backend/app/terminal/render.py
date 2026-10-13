"""Turn a :class:`RuntimeState` into the AirdropOS terminal dashboard.

One renderable tree, one screen, redrawn in place by ``rich.live.Live``. Nothing
here is a second source of information: every string comes from the collected
state, including the empty states and the "unavailable" markers.

Terminal constraints worth stating plainly, because they are the only places the
reference image cannot be reproduced literally:

* A terminal cannot scale a glyph. The brand is therefore drawn from the same
  monochrome glyph family as the rest of the dashboard, three rows tall for the
  mark and two rows (gradient face plus a drop shadow) for the wordmark, instead
  of from a large pictographic rocket.
* Rounded corners need a font with the matching box-drawing glyphs. They are
  used throughout; on a font that lacks them they degrade to their plain
  square equivalents and nothing is lost but the rounding.
"""

from __future__ import annotations

from typing import List, Sequence

from rich.align import Align
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from backend.app.terminal import theme
from backend.app.terminal.state import (
    DISABLED,
    RUNNING,
    JobView,
    RuntimeState,
    ServiceState,
)

# The middle row needs a fixed number of rows (nine scheduled jobs), so only the
# activity feed is elastic. Its size is what adapts to the terminal height.
BOTTOM_MIN = 6
BOTTOM_MAX = 16

WIDE = 138

_STATE_TEXT = {
    RUNNING: "RUNNING",
    "starting": "STARTING",
    "stopped": "STOPPED",
    "error": "ERROR",
    DISABLED: "DISABLED",
    "unknown": "UNKNOWN",
}

_SERVICE_ICON = {
    "Backend API": theme.ICON_BACKEND,
    "Frontend (Vite)": theme.ICON_FRONTEND,
    "Agent Worker": theme.ICON_WORKER,
    "Live Gateway": theme.ICON_GATEWAY,
}

_TASK_CARDS = (
    ("Running", theme.ICON_RUNNING, "running", theme.CYAN),
    ("Pending", theme.ICON_PENDING, "pending", theme.YELLOW),
    ("Completed", theme.ICON_COMPLETED, "completed", theme.GREEN),
    ("Failed", theme.ICON_FAILED, "failed", theme.RED),
)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def build(state: RuntimeState, width: int, height: int) -> RenderableType:
    """The whole dashboard as one renderable for the given terminal size.

    Width picks an arrangement and height sizes the only elastic panel. Both are
    recomputed on every frame, so resizing the terminal re-lays the dashboard out
    without restarting anything.
    """
    compact = height < 42
    header_h = 4 if not compact else 3
    status_h = 10 if not compact else 9
    content = _content_height(state, compact)

    if width >= WIDE:
        middle, middle_h = _wide_middle(state, width, compact), content
    else:
        # Statistics and tools stacked beside the schedule. This is also the
        # fallback below WIDE: one arrangement that still fits a narrow terminal
        # beats a third one that only suits an extreme case.
        tools_h = (max(len(state.tools.categories), 1) + 3) if state.tools.loaded else 6
        middle_h = max(content, 7 + tools_h)
        middle = _narrow_middle(state, width, compact)

    # header + gap + status + gap + middle + gap + gap + footer
    fixed = header_h + status_h + middle_h + 7
    bottom = max(3, min(BOTTOM_MAX, height - fixed))

    blocks: List[RenderableType] = [
        _header(state, width, compact),
        "",
        _status_row(state, width, compact),
        "",
        middle,
        "",
        _bottom_row(state, width, bottom),
        "",
        _footer(state, width),
    ]
    # Padding is the only Group-level way to paint a background; `style=` on the
    # Group itself is not a constructor argument.
    return Padding(Group(*blocks), (0, 0), style=theme.BACKGROUND)


# ----------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------
def _header(state: RuntimeState, width: int, compact: bool) -> RenderableType:
    mark = Table.grid(padding=(0, 1))
    mark.add_column(width=4, no_wrap=True)
    mark.add_column(ratio=1)
    mark.add_column(width=2, no_wrap=True)
    mark.add_column(width=10, no_wrap=True)
    mark.add_row(
        Align.center(Group(*_logo_lines()), vertical="middle"),
        Group(*_wordmark_lines(compact)),
        Text(""),
        Align.center(
            Panel(
                Text(f"v{state.version or '1.0.0'}", style="bold " + theme.GREEN),
                box=_rounded(),
                border_style=theme.GREEN,
                padding=(0, 1),
                height=3,
                style=theme.PANEL_INNER,
            ),
            vertical="middle",
        ),
    )

    facts = Table.grid(padding=(0, 1), expand=False)
    facts.add_column(justify="right", no_wrap=True)
    facts.add_column(justify="left", ratio=1, overflow="ellipsis")
    facts.add_row(Text("Environment:", style=theme.MUTED), Text(state.environment or "-", style=theme.CYAN))
    facts.add_row(Text("Model:", style=theme.MUTED), Text(state.model or "-", style=theme.CYAN))
    facts.add_row(Text("API:", style=theme.MUTED), Text(state.endpoint or "-", style=theme.CYAN))

    columns = Table.grid(expand=True)
    columns.add_column(ratio=3)
    columns.add_column(ratio=2)
    columns.add_row(mark, Align.right(facts))

    return Group(columns, Text.from_markup(theme.gradient_rule(width)))


def _logo_lines() -> List[RenderableType]:
    return [
        Text(glyph, style=style, justify="center")
        for glyph, style in zip(theme.LOGO_MARK, theme.LOGO_STYLES)
    ]


def _wordmark_lines(compact: bool) -> List[RenderableType]:
    """Wordmark with a one-cell drop shadow, so it reads large in a fixed grid."""
    title = Text.from_markup(theme.gradient_text("AirdropOS", style="bold"))
    lines: List[RenderableType] = [title]
    if not compact:
        shadow = Text(" AirdropOS", style="bold #1e3a8a")
        lines.append(shadow)
        lines.append(Text(theme.SUBTITLE, style="bold " + theme.PURPLE))
    else:
        lines.append(Text(theme.SUBTITLE.replace("   ", " "), style=theme.PURPLE))
    return lines


# ----------------------------------------------------------------------
# System status + agent configuration
# ----------------------------------------------------------------------
def _status_row(state: RuntimeState, width: int, compact: bool) -> RenderableType:
    card_height = 5 if compact else 6
    outer = 7 if compact else 8
    # Fixed widths, not ratios. Ratio columns hand the leftover cells to the
    # first column, so four cards came out one cell apart and the address on the
    # third one ellipsised while the first one did not.
    config_w = max(38, int(width * 0.33))
    status_w = width - config_w
    card_w = max(20, (status_w - 7) // 4)
    cards = Table.grid(padding=0)
    for index in range(4):
        if index:
            cards.add_column(width=1, no_wrap=True)
        cards.add_column(width=card_w, no_wrap=True)
    cards.add_row(
        _service_card(state.backend, card_height),
        Text(""),
        _service_card(state.frontend, card_height),
        Text(""),
        _service_card(state.worker, card_height),
        Text(""),
        _service_card(state.gateway, card_height),
    )

    columns = Table.grid(expand=True, padding=0)
    columns.add_column(width=status_w, no_wrap=True)
    columns.add_column(ratio=1)
    columns.add_row(
        _panel(
            f"{theme.ICON_SYSTEM}  SYSTEM STATUS",
            cards,
            height=outer + 2,
            border_style=theme.BORDER,
        ),
        _panel(
            f"{theme.ICON_CONFIG}  AGENT CONFIGURATION",
            _agent_config(state),
            height=outer + 2,
            border_style=theme.BORDER,
        ),
    )
    return columns


def _service_card(service: ServiceState, height: int) -> RenderableType:
    marker, colour = theme.STATE_MARK.get(service.state, ("?", theme.YELLOW))
    icon = _SERVICE_ICON.get(service.name, "◈")

    body: List[RenderableType] = [
        Text.assemble((f"{icon}  ", colour), (service.name, "bold " + theme.TEXT_STRONG)),
        _pill(f"{marker} {_STATE_TEXT.get(service.state, service.state.upper())}", colour),
    ]
    detail = list(service.detail)
    while len(body) < height - 2:
        if detail:
            body.append(Text(detail.pop(0), style=theme.CYAN, overflow="ellipsis", no_wrap=True))
        elif service.note:
            body.append(Text(service.note, style=_note_style(service.state), overflow="ellipsis", no_wrap=True))
        else:
            body.append(Text(""))
    body = body[: height - 2]
    return Panel(
        Group(*body),
        box=_rounded(),
        border_style=theme.BORDER if service.state in (RUNNING, DISABLED) else colour,
        # One space of horizontal padding. Two would cost exactly the two
        # characters a `http://host:port` address needs at this width.
        padding=(0, 1),
        style=theme.PANEL_INNER,
        height=height,
    )


def _note_style(state: str) -> str:
    if state == RUNNING:
        return theme.MUTED
    if state == DISABLED:
        return theme.MUTED
    return theme.YELLOW


def _agent_config(state: RuntimeState) -> RenderableType:
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left", no_wrap=True, width=17)
    table.add_column(justify="left", width=1)
    table.add_column(ratio=1, overflow="ellipsis")

    def row(label: str, value: RenderableType) -> None:
        table.add_row(Text(label, style=theme.TEXT), Text(":", style=theme.MUTED), value)

    row("Model", Text(state.model or "-", style=theme.CYAN))
    row("Model Endpoint", Text(state.endpoint or "-", style=theme.CYAN))
    row("Registered Tools", Text(str(state.tools.total) if state.tools.loaded else "unavailable", style=theme.CYAN))
    row("Scheduler Jobs", Text(str(len(state.jobs)), style=theme.CYAN))
    row("Emergency Stop", _emergency_stop(state))
    row(
        "Auth Enabled",
        Text("YES", style=theme.GREEN)
        if state.auth_enabled
        else Text("NO", style=theme.YELLOW)
        if state.auth_enabled is False
        else Text("UNKNOWN", style=theme.YELLOW),
    )
    row("Environment", Text(state.environment or "-", style=theme.CYAN))
    return table


def _emergency_stop(state: RuntimeState) -> RenderableType:
    """Never shows INACTIVE unless the shared state actually says so."""
    if state.emergency_stop_error:
        return Text("UNKNOWN", style="bold " + theme.YELLOW)
    enabled = state.emergency_stop.get("emergency_stop_enabled")
    if enabled is True:
        reason = str(state.emergency_stop.get("reason") or "").strip()
        label = "ACTIVE"
        if reason:
            return Text.assemble(
                (" ACTIVE ", "bold " + theme.RED),
                (f" {reason[:60]}", theme.MUTED),
            )
        return Text(label, style="bold " + theme.RED)
    if enabled is False:
        return Text("INACTIVE", style="bold " + theme.GREEN)
    return Text("UNKNOWN", style="bold " + theme.YELLOW)


# ----------------------------------------------------------------------
# Task statistics / scheduler / tools
# ----------------------------------------------------------------------
def _wide_middle(state: RuntimeState, width: int, compact: bool) -> RenderableType:
    # Fixed widths, not three equal ratios: the statistics panel carries four
    # side-by-side cards and needs the extra cells, which is also how the
    # reference lays it out (its statistics panel is the widest of the three).
    stats_w = max(34, round(width * 0.36))
    jobs_w = max(30, round(width * 0.30))
    tools_w = max(30, width - stats_w - jobs_w)
    height = _middle_height(state, compact)
    columns = Table.grid(expand=True, padding=0)
    columns.add_column(width=stats_w, no_wrap=True)
    columns.add_column(width=jobs_w, no_wrap=True)
    columns.add_column(ratio=1)
    columns.add_row(
        _panel(
            f"{theme.ICON_TASKS}  TASK STATISTICS",
            _task_cards(state, compact, stats_w - 4),
            height=height,
        ),
        _panel(
            f"{theme.ICON_SCHEDULER}  SCHEDULER JOBS ({len(state.jobs)})",
            _job_list(state.jobs, compact),
            height=height,
        ),
        _panel(
            f"{theme.ICON_TOOLS}  {_tools_title(state)}",
            _tool_list(state),
            height=height,
        ),
    )
    return columns


def _narrow_middle(state: RuntimeState, width: int, compact: bool) -> RenderableType:
    """Two columns instead of three: the schedule beside statistics and tools.

    Stacking the two short panels on the right keeps the row shorter than
    stacking statistics above the schedule, which matters most exactly where
    there is least height to spend.
    """
    height = _middle_height(state, compact)
    tools_h = max(len(state.tools.categories), 1) + 3 if state.tools.loaded else 6
    right = Table.grid(expand=True, padding=0)
    right.add_column(ratio=1)
    right.add_row(_panel(f"{theme.ICON_TASKS}  TASK STATISTICS", _task_cards(state, compact, width // 3 - 4), height=7))
    right.add_row(
        _panel(
            f"{theme.ICON_TOOLS}  {_tools_title(state)}",
            _tool_list(state),
            height=tools_h,
        )
    )
    columns = Table.grid(expand=True, padding=0)
    columns.add_column(ratio=2)
    columns.add_column(ratio=1)
    columns.add_row(
        _panel(
            f"{theme.ICON_SCHEDULER}  SCHEDULER JOBS ({len(state.jobs)})",
            _job_list(state.jobs, compact),
            height=max(height, 7 + tools_h),
        ),
        right,
    )
    return columns


def _middle_height(state: RuntimeState, compact: bool) -> int:
    return _content_height(state, compact)


def _content_height(state: RuntimeState, compact: bool) -> int:
    """Rows the middle row needs for its content: title, N lines, two borders."""
    rows = max(len(state.jobs), len(state.tools.categories)) if state.tools.loaded else len(state.jobs)
    return max(rows, 8) + 3


def _task_cards(state: RuntimeState, compact: bool, available: int) -> RenderableType:
    card_w = max(9, (available - 3) // 4)
    grid = Table.grid(padding=0)
    for index in range(4):
        if index:
            grid.add_column(width=1, no_wrap=True)
        grid.add_column(width=card_w, no_wrap=True)
    cells = []
    for label, icon, attribute, colour in _TASK_CARDS:
        value = getattr(state.tasks, attribute)
        shown = str(value) if state.tasks.known else "—"
        note = "" if state.tasks.known else "unknown"
        body = Group(
            Align.center(Text(label, style=colour), vertical="middle"),
            Align.center(Text(icon, style=colour), vertical="middle"),
            Align.center(Text(shown, style=f"bold {colour}"), vertical="middle"),
            Align.center(Text(note, style=theme.FAINT), vertical="middle"),
        )
        cells.append(
            Panel(
                body,
                box=_rounded(),
                border_style=theme.BORDER,
                padding=(0, 0),
                style=theme.PANEL_INNER,
                height=5 if not compact else 4,
            )
        )
    grid.add_row(*_interleave(cells))
    body: List[RenderableType] = [grid]
    if state.tasks.other:
        body.append(
            Text(
                f"other states: {state.tasks.other}   total: {state.tasks.total}",
                style=theme.MUTED,
            )
        )
    return Group(*body)


def _job_list(jobs: Sequence[JobView], compact: bool) -> RenderableType:
    if not jobs:
        return Text("scheduler configuration unavailable", style=theme.YELLOW)
    # The note column only exists when something has a note; at ordinary widths
    # it is the column that squeezes the job name into two lines.
    with_notes = any(job.detail for job in jobs)
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    grid.add_column(width=13, justify="right", no_wrap=True)
    if with_notes:
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(overflow="ellipsis", no_wrap=True)
    for job in jobs:
        interval = f"every {job.interval_minutes} min" if job.interval_minutes else "no interval"
        cells = [
            Text(job.marker + " ", style=job.marker_style),
            Text(job.label, style=theme.TEXT if job.active is not False else theme.MUTED),
            Text(interval, style=theme.CYAN),
        ]
        if with_notes:
            cells += [Text(" "), Text(job.detail, style=theme.FAINT)]
        grid.add_row(*cells)
    return grid


def _tools_title(state: RuntimeState) -> str:
    if not state.tools.loaded:
        return "REGISTERED TOOLS (unavailable)"
    return f"REGISTERED TOOLS ({state.tools.total})"


def _tool_list(state: RuntimeState) -> RenderableType:
    if not state.tools.loaded:
        detail = state.tools.detail or "registry could not be loaded"
        return Text(detail, style=theme.YELLOW, overflow="ellipsis")
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(width=8, no_wrap=True)
    for category in state.tools.categories:
        grid.add_row(
            Text(category.icon + " ", style=theme.CYAN),
            Text(category.label, style=theme.TEXT),
            Text(f"{category.count} tools", style=theme.CYAN),
            Text(""),
            Text(category.badge, style=category.badge_style) if category.badge else Text(""),
        )
    return grid


# ----------------------------------------------------------------------
# Activity + alerts
# ----------------------------------------------------------------------
def _bottom_row(state: RuntimeState, width: int, height: int) -> RenderableType:
    columns = Table.grid(expand=True)
    columns.add_column(ratio=2)
    columns.add_column(ratio=1)
    columns.add_row(
        _panel(
            f"{theme.ICON_ACTIVITY}  RECENT ACTIVITY",
            _activity(state, height - 2),
            height=height,
        ),
        _panel(
            f"{theme.ICON_ALERTS}  WARNINGS & ERRORS",
            _alerts(state, height - 2),
            height=height,
            border_style=theme.BORDER if not state.alerts else theme.YELLOW,
        ),
    )
    return columns


def _activity(state: RuntimeState, rows: int) -> RenderableType:
    events = list(state.events)[-max(1, rows) :]
    if not events:
        return Text("waiting for events…", style=theme.MUTED)
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=9, no_wrap=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    for event in events:
        marker, colour = theme.LEVEL_STYLE.get(event.level, (theme.DOT_INFO, theme.CYAN))
        grid.add_row(
            Text(event.time + " ", style=theme.MUTED),
            Text(marker + " " + event.message, style=colour if event.rank >= 3 else theme.TEXT),
        )
    return grid


def _alerts(state: RuntimeState, rows: int) -> RenderableType:
    alerts = state.alerts
    if not alerts:
        if state.everything_operational:
            subtitle = "All systems operational"
            subtitle_style = theme.MUTED
        else:
            subtitle = "No logged warnings or errors"
            subtitle_style = theme.YELLOW
        check = Panel(
            Text(theme.DOT_SUCCESS, style="bold " + theme.GREEN),
            box=_rounded(),
            border_style=theme.GREEN,
            padding=(0, 1),
            style=theme.PANEL_INNER,
        )
        return Group(
            "",
            Align.center(check, height=5),
            Align.center(Text("No warnings or errors", style=theme.TEXT_STRONG), height=1),
            Align.center(Text(subtitle, style=subtitle_style), height=1),
        )

    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=9, no_wrap=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    for event in alerts[: max(1, rows)]:
        marker, colour = theme.LEVEL_STYLE.get(event.level, (theme.DOT_ERROR, theme.RED))
        grid.add_row(
            Text(event.time + " ", style=theme.MUTED),
            Text(marker + " " + event.message, style=colour),
        )
    return Group(grid, Text(f"{len(alerts)} active alert(s)", style=theme.MUTED))


# ----------------------------------------------------------------------
# Footer
# ----------------------------------------------------------------------
def _footer(state: RuntimeState, width: int) -> RenderableType:
    seconds = int(state.uptime_seconds)
    uptime = f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"
    left = Text.assemble(
        ("AirdropOS", "bold " + theme.CYAN),
        ("  │  ", theme.FAINT),
        ("Autonomous Airdrop Agent", theme.MUTED),
        ("  │  ", theme.FAINT),
        ("Press ", theme.MUTED),
        ("Ctrl+C", "bold " + theme.YELLOW),
        (" to stop everything", theme.MUTED),
    )
    right = Text.assemble(
        (f"Uptime: {uptime}", theme.MUTED),
        ("  │  ", theme.FAINT),
        (f"Log level: {(state.log_level or '?').upper()}", theme.MUTED),
        ("  │  ", theme.FAINT),
        (f"Logs: {state.log_path or 'logs/airdropos.log'}", theme.MUTED),
    )
    columns = Table.grid(expand=True, padding=(0, 1))
    columns.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    columns.add_column(justify="right", no_wrap=True)
    columns.add_row(left, right)
    return Group(Text.from_markup(theme.gradient_rule(width)), columns)


# ----------------------------------------------------------------------
# Small shared pieces
# ----------------------------------------------------------------------
def _interleave(cells: Sequence[RenderableType]) -> List[RenderableType]:
    """Cells separated by the blank spacer columns a spaced grid declares."""
    out: List[RenderableType] = []
    for index, cell in enumerate(cells):
        if index:
            out.append(Text(""))
        out.append(cell)
    return out


def _panel(title: str, body: RenderableType, *, height: int, border_style: str = theme.BORDER) -> RenderableType:
    return Panel(
        body,
        title=Text(title, style="bold " + theme.CYAN),
        title_align="left",
        border_style=border_style,
        box=_rounded(),
        padding=(0, 1),
        style=theme.PANEL,
        height=height,
    )


def _pill(label: str, colour: str) -> RenderableType:
    return Panel(
        Text(label, style="bold " + colour),
        box=_rounded(),
        border_style=colour,
        padding=(0, 1),
        style=theme.PANEL_INNER,
        height=1,
    )


def _rounded():
    from rich import box

    return box.ROUNDED


__all__ = ["build"]