"""Turn a :class:`RuntimeState` into the AirdropOS terminal dashboard.

One renderable tree, one screen, redrawn in place by ``rich.live.Live``. Nothing
here is a second source of information: every string comes from the collected
state, including the empty states and the "unavailable" markers.

Responsive layout
-----------------
Nothing here assumes a terminal size. ``build`` is given the real width and height
on every frame and decides, section by section, how much room each one gets:

* The width picks the arrangement (three columns, two columns, or one column) and
  how much detail a row carries (labels, badges, endpoints).
* The height is shared out by ``_allocate``: every section has a minimum, the
  most important ones grow first, and the activity feed soaks up whatever is left.
  Sections that cannot get their minimum are dropped instead of being cut in half,
  and a list that does not fit says ``… +N more`` instead of silently losing rows.
* A section that is short on rows switches to a smaller form of itself (cards,
  then a list, then a one-line strip), so the content stays readable.

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

from typing import Dict, List, Optional, Sequence, Tuple

from rich.align import Align
from rich.cells import cell_len
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

# Width tiers. WIDE gets three columns in the middle row, MEDIUM two, anything
# narrower one. Below MEDIUM the agent-configuration panel is dropped because the
# header already carries environment, model and endpoint.
WIDE = 138
MEDIUM = 96

MIN_WIDTH = 30
MIN_HEIGHT = 6

# Rows a section is happy with once everything else has its minimum.
BOTTOM_PREF = 10
STATUS_LIST = 6   # four service rows plus the borders
CARD_MIN_WIDTH = 22  # widest service name plus its icon, padding and borders
STATUS_CARDS = 9  # bordered service cards with a status pill
STATS_FULL = 7    # four bordered cards plus the borders

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

_SHORT_NAME = {
    "Backend API": "Backend",
    "Frontend (Vite)": "Frontend",
    "Agent Worker": "Worker",
    "Live Gateway": "Gateway",
}

_TASK_CARDS = (
    ("Running", "Run", theme.ICON_RUNNING, "running", theme.CYAN),
    ("Pending", "Wait", theme.ICON_PENDING, "pending", theme.YELLOW),
    ("Completed", "Done", theme.ICON_COMPLETED, "completed", theme.GREEN),
    ("Failed", "Fail", theme.ICON_FAILED, "failed", theme.RED),
)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def build(state: RuntimeState, width: int, height: int) -> RenderableType:
    """The whole dashboard as one renderable for the given terminal size.

    The result is laid out to be exactly ``height`` rows tall and never wider than
    ``width`` cells, so a frame can neither be cropped nor scroll the screen.
    """
    width = max(MIN_WIDTH, int(width))
    height = max(MIN_HEIGHT, int(height))

    header, header_rows = _header_block(state, width, height)
    footer, footer_rows = _footer_block(state, width, height)
    body = _body(state, width, max(1, height - header_rows - footer_rows))

    # Padding is the only Group-level way to paint a background; `style=` on the
    # Group itself is not a constructor argument.
    return Padding(Group(header, body, footer), (0, 0), style=theme.BACKGROUND)


# ----------------------------------------------------------------------
# Space allocation
# ----------------------------------------------------------------------
def _allocate(
    total: int,
    gap: int,
    minimums: Sequence[Tuple[str, int]],
    steps: Sequence[Tuple[str, int]],
    elastic: Optional[str] = None,
) -> Dict[str, int]:
    """Share ``total`` rows between sections, most important first.

    ``minimums`` is ordered by importance: a section is only shown if it and every
    section before it can have its minimum, so nothing is ever shown half-cut.
    ``steps`` then grows sections towards their preferred sizes in the order given
    (so an important section can reach a useful size before a decorative one grows
    further). Rows still unused go to ``elastic`` (default: the last section shown).
    ``gap`` rows of spacing are reserved between shown sections.
    """
    chosen: List[str] = []
    sizes: Dict[str, int] = {}
    used = 0
    for key, minimum in minimums:
        cost = minimum + (gap if chosen else 0)
        if used + cost > total:
            break
        chosen.append(key)
        sizes[key] = minimum
        used += cost
    if not chosen:
        return {minimums[0][0]: max(1, total)}

    free = total - used
    for key, target in steps:
        if key in sizes and free > 0 and target > sizes[key]:
            grow = min(free, target - sizes[key])
            sizes[key] += grow
            free -= grow
    if free > 0:
        receiver = elastic if elastic in sizes else chosen[-1]
        sizes[receiver] += free
    return sizes


def _clip(value: str, limit: int) -> str:
    """``value`` cut to ``limit`` terminal cells, ending in an ellipsis if cut."""
    value = value or ""
    if limit <= 0:
        return ""
    if cell_len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    out = ""
    for char in value:
        if cell_len(out + char) > limit - 1:
            break
        out += char
    return out + "…"


# ----------------------------------------------------------------------
# Body: status, middle, bottom
# ----------------------------------------------------------------------
def _body(state: RuntimeState, width: int, rows: int) -> RenderableType:
    gap = 1 if rows >= 38 else 0
    cards_fit = _card_width(width) >= CARD_MIN_WIDTH
    sizes = _allocate(
        rows,
        gap,
        minimums=[("status", 3), ("middle", 5), ("bottom", 5)],
        steps=[
            ("status", STATUS_LIST),
            ("middle", _middle_pref(state, width)),
            ("status", STATUS_CARDS if cards_fit else STATUS_LIST),
            ("bottom", BOTTOM_PREF),
        ],
        elastic="bottom",
    )

    sections: List[RenderableType] = []
    for key in ("status", "middle", "bottom"):
        if key not in sizes:
            continue
        if sections and gap:
            sections.append(Text(""))
        height = sizes[key]
        if key == "status":
            sections.append(_status_row(state, width, height))
        elif key == "middle":
            sections.append(_middle_row(state, width, height))
        else:
            sections.append(_bottom_row(state, width, height))

    shown = sum(sizes.values()) + (gap * (len(sizes) - 1))
    for _ in range(max(0, rows - shown)):
        sections.append(Text(""))
    return Group(*sections)


def _stats_pref(width: int) -> int:
    """Rows the statistics panel can use: cards when they fit, else a single line."""
    inner = (width - 4)
    return STATS_FULL if (inner - 3) // 4 >= 7 else 3


def _middle_pref(state: RuntimeState, width: int) -> int:
    jobs = max(len(state.jobs), 1) + 2
    tools = (len(state.tools.categories) if state.tools.loaded else 4) + 2
    if width >= WIDE:
        return max(jobs, tools, 10)
    if width >= MEDIUM:
        return max(jobs, _stats_pref(_side_width(width)) + tools)
    return _stats_pref(width) + jobs + tools


# ----------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------
def _header_block(state: RuntimeState, width: int, height: int) -> Tuple[RenderableType, int]:
    if height >= 26 and width >= 70:
        return _header(state, width), 4
    return _mini_header(state, width), 1


def _header(state: RuntimeState, width: int) -> RenderableType:
    facts_w = max(30, int(width * 0.38))
    left_w = width - facts_w
    version_w = 10 if left_w >= 52 else 0
    word_w = left_w - 4 - 2 - version_w - 3
    spaced = word_w >= cell_len(theme.SUBTITLE)

    mark = Table.grid(padding=(0, 1))
    mark.add_column(width=4, no_wrap=True)
    # Hug the wordmark: the version badge sits next to it, not at the far edge.
    mark.add_column(width=max(10, min(word_w, cell_len(theme.SUBTITLE) + 1)), no_wrap=True)
    if version_w:
        mark.add_column(width=version_w, no_wrap=True)
    cells: List[RenderableType] = [
        Align.center(Group(*_logo_lines()), vertical="middle"),
        Group(*_wordmark_lines(spaced)),
    ]
    if version_w:
        cells.append(
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
            )
        )
    mark.add_row(*cells)

    value_w = max(8, facts_w - 14)
    facts = Table.grid(padding=(0, 1))
    facts.add_column(justify="right", no_wrap=True, width=12)
    facts.add_column(justify="left", no_wrap=True, width=value_w)
    facts.add_row(Text("Environment:", style=theme.MUTED), Text(_clip(state.environment or "-", value_w), style=theme.CYAN))
    facts.add_row(Text("Model:", style=theme.MUTED), Text(_clip(state.model or "-", value_w), style=theme.CYAN))
    facts.add_row(Text("API:", style=theme.MUTED), Text(_clip(state.endpoint or "-", value_w), style=theme.CYAN))

    columns = Table.grid(padding=0)
    columns.add_column(width=left_w, no_wrap=True)
    columns.add_column(width=facts_w, no_wrap=True)
    columns.add_row(mark, Align.right(facts))

    return Group(columns, Text.from_markup(theme.gradient_rule(width)))


def _mini_header(state: RuntimeState, width: int) -> RenderableType:
    """One row for short terminals: brand, version, then as many facts as fit."""
    parts: List[Tuple[str, str]] = [
        ("▲ ", theme.CYAN),
        ("AirdropOS", "bold " + theme.CYAN),
        (f" v{state.version or '1.0.0'}", theme.GREEN),
    ]
    extras = [state.environment or "", state.model or "", state.endpoint or ""]
    line = Text.assemble(*parts)
    for extra in extras:
        if not extra:
            continue
        candidate = Text.assemble(*parts, ("  │  ", theme.FAINT), (extra, theme.MUTED))
        if candidate.cell_len > width:
            break
        parts += [("  │  ", theme.FAINT), (extra, theme.MUTED)]
        line = candidate
    line.truncate(width, overflow="ellipsis")
    return line


def _logo_lines() -> List[RenderableType]:
    return [
        Text(glyph, style=style, justify="center")
        for glyph, style in zip(theme.LOGO_MARK, theme.LOGO_STYLES)
    ]


def _wordmark_lines(spaced: bool) -> List[RenderableType]:
    """Wordmark with a one-cell drop shadow, so it reads large in a fixed grid."""
    title = Text.from_markup(theme.gradient_text("AirdropOS", style="bold"))
    shadow = Text(" AirdropOS", style="bold #1e3a8a")
    subtitle = theme.SUBTITLE if spaced else "AUTONOMOUS AIRDROP AGENT"
    return [title, shadow, Text(subtitle, style="bold " + theme.PURPLE, no_wrap=True)]


# ----------------------------------------------------------------------
# System status + agent configuration
# ----------------------------------------------------------------------
def _side_width(width: int) -> int:
    """Width of the right-hand column. Status, middle and bottom rows share it so
    their borders line up from the top of the screen to the bottom."""
    return max(38, int(width * 0.33))


def _config_width(width: int, height: int) -> int:
    """Width of the configuration panel, or 0 when there is no room for it."""
    if width < MEDIUM or height < 6:
        return 0
    return _side_width(width)


def _card_width(width: int) -> int:
    """Width each of four side-by-side service cards would get at this width."""
    status_w = width - _config_width(width, STATUS_CARDS)
    return (status_w - 4 - 3) // 4


def _status_row(state: RuntimeState, width: int, height: int) -> RenderableType:
    config_w = _config_width(width, height)
    status_w = width - config_w
    inner_w = status_w - 4
    card_w = (inner_w - 3) // 4
    services = [state.backend, state.frontend, state.worker, state.gateway]

    if height >= 7 and card_w >= CARD_MIN_WIDTH:
        body: RenderableType = _service_cards(services, card_w, height - 2)
    elif height >= STATUS_LIST:
        body = _service_list(services, inner_w)
    else:
        body = _service_strip(services, inner_w)

    system = _panel(f"{theme.ICON_SYSTEM}  SYSTEM STATUS", body, height=height)
    if not config_w:
        return system

    columns = Table.grid(expand=True, padding=0)
    columns.add_column(width=status_w, no_wrap=True)
    columns.add_column(ratio=1)
    columns.add_row(
        system,
        _panel(
            f"{theme.ICON_CONFIG}  AGENT CONFIGURATION",
            _agent_config(state, height - 2, width - status_w - 4),
            height=height,
        ),
    )
    return columns


def _service_cards(services: Sequence[ServiceState], card_w: int, card_h: int) -> RenderableType:
    cards = Table.grid(padding=0)
    cells: List[RenderableType] = []
    for index in range(4):
        if index:
            cards.add_column(width=1, no_wrap=True)
        cards.add_column(width=card_w, no_wrap=True)
    for service in services:
        cells.append(_service_card(service, card_h))
    cards.add_row(*_interleave(cells))
    return cards


def _service_card(service: ServiceState, height: int) -> RenderableType:
    marker, colour = theme.STATE_MARK.get(service.state, ("?", theme.YELLOW))
    icon = _SERVICE_ICON.get(service.name, "◈")
    label = _STATE_TEXT.get(service.state, service.state.upper())
    inner = height - 2

    body: List[RenderableType] = [
        Text.assemble((f"{icon}  ", colour), (service.name, "bold " + theme.TEXT_STRONG), no_wrap=True, overflow="ellipsis")
    ]
    # The bordered pill needs three rows; with fewer it is a plain line of text.
    if inner >= 5:
        body.append(_pill(f"{marker} {label}", colour))
        remaining = inner - 4
    else:
        body.append(Text(f"{marker} {label}", style="bold " + colour, no_wrap=True))
        remaining = inner - 2
    extra: List[Tuple[str, str]] = [(line, theme.CYAN) for line in service.detail]
    if service.note:
        extra.append((service.note, _note_style(service.state)))
    for text, style in extra[: max(0, remaining)]:
        body.append(Text(text, style=style, overflow="ellipsis", no_wrap=True))
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


def _service_list(services: Sequence[ServiceState], inner_w: int) -> RenderableType:
    """One row per service: the form used when four cards do not fit."""
    short = inner_w < 46
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(width=9 if short else 17, no_wrap=True)
    grid.add_column(width=9 if short else 10, no_wrap=True)
    show_detail = inner_w >= 34
    if show_detail:
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    for service in services:
        marker, colour = theme.STATE_MARK.get(service.state, ("?", theme.YELLOW))
        name = _SHORT_NAME.get(service.name, service.name) if short else service.name
        detail = service.detail[0] if service.detail else (service.note or "")
        row: List[RenderableType] = [
            Text(marker + " ", style=colour),
            Text(name, style="bold " + theme.TEXT_STRONG, no_wrap=True),
            Text(_STATE_TEXT.get(service.state, service.state.upper()), style=colour, no_wrap=True),
        ]
        if show_detail:
            row.append(Text(detail, style=theme.CYAN if service.detail else _note_style(service.state)))
        grid.add_row(*row)
    return grid


def _service_strip(services: Sequence[ServiceState], inner_w: int) -> RenderableType:
    """The smallest form: a single line of coloured dots and short names."""
    for with_state in (True, False):
        line = Text(no_wrap=True)
        for index, service in enumerate(services):
            marker, colour = theme.STATE_MARK.get(service.state, ("?", theme.YELLOW))
            if index:
                line.append("   ")
            line.append(marker + " ", style=colour)
            line.append(_SHORT_NAME.get(service.name, service.name), style=theme.TEXT_STRONG)
            if with_state and service.state != RUNNING:
                line.append(" " + _STATE_TEXT.get(service.state, service.state.upper()), style=colour)
        if line.cell_len <= inner_w:
            break
    line.truncate(max(1, inner_w), overflow="ellipsis")
    return line


def _note_style(state: str) -> str:
    if state == RUNNING:
        return theme.MUTED
    if state == DISABLED:
        return theme.MUTED
    return theme.YELLOW


# Rows kept first when the panel is too short for all seven.
_CONFIG_PRIORITY = {
    "Emergency Stop": 0,
    "Model": 1,
    "Registered Tools": 2,
    "Scheduler Jobs": 3,
    "Model Endpoint": 4,
    "Auth Enabled": 5,
    "Environment": 6,
}


def _agent_config(state: RuntimeState, rows: int, inner_w: int) -> RenderableType:
    # 17 for the label, 1 for the colon, 2 for the spacing between the columns.
    # Long values are clipped here so they can never squeeze the labels.
    value_w = max(6, inner_w - 17 - 1 - 2)
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left", no_wrap=True, width=17)
    table.add_column(justify="left", width=1)
    table.add_column(width=value_w, no_wrap=True)

    entries: List[Tuple[str, RenderableType]] = [
        ("Model", Text(_clip(state.model or "-", value_w), style=theme.CYAN)),
        ("Model Endpoint", Text(_clip(state.endpoint or "-", value_w), style=theme.CYAN)),
        (
            "Registered Tools",
            Text(str(state.tools.total) if state.tools.loaded else "unavailable", style=theme.CYAN),
        ),
        ("Scheduler Jobs", Text(str(len(state.jobs)), style=theme.CYAN)),
        ("Emergency Stop", _emergency_stop(state)),
        (
            "Auth Enabled",
            Text("YES", style=theme.GREEN)
            if state.auth_enabled
            else Text("NO", style=theme.YELLOW)
            if state.auth_enabled is False
            else Text("UNKNOWN", style=theme.YELLOW),
        ),
        ("Environment", Text(_clip(state.environment or "-", value_w), style=theme.CYAN)),
    ]
    keep = sorted(range(len(entries)), key=lambda i: _CONFIG_PRIORITY[entries[i][0]])[: max(1, rows)]
    for index in sorted(keep):
        label, value = entries[index]
        table.add_row(Text(label, style=theme.TEXT), Text(":", style=theme.MUTED), value)
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
# Middle row: task statistics / scheduler / tools
# ----------------------------------------------------------------------
def _middle_row(state: RuntimeState, width: int, height: int) -> RenderableType:
    if width >= WIDE:
        return _wide_middle(state, width, height)
    if width >= MEDIUM:
        return _medium_middle(state, width, height)
    return _narrow_middle(state, width, height)


def _wide_middle(state: RuntimeState, width: int, height: int) -> RenderableType:
    # Fixed widths, not three equal ratios: the statistics panel carries four
    # side-by-side cards and needs the extra cells.
    # The tools column takes the shared side width so its border lines up with the
    # configuration panel above and the alerts panel below.
    tools_w = _side_width(width)
    stats_w = max(34, round((width - tools_w) * 0.54))
    jobs_w = width - tools_w - stats_w
    columns = Table.grid(expand=True, padding=0)
    columns.add_column(width=stats_w, no_wrap=True)
    columns.add_column(width=jobs_w, no_wrap=True)
    columns.add_column(ratio=1)
    columns.add_row(
        _stats_panel(state, stats_w, height),
        _jobs_panel(state, jobs_w, height),
        _tools_panel(state, tools_w, height),
    )
    return columns


def _medium_middle(state: RuntimeState, width: int, height: int) -> RenderableType:
    """Two columns: the schedule beside statistics stacked above tools."""
    right_w = _side_width(width)
    left_w = width - right_w
    tools_pref = (len(state.tools.categories) if state.tools.loaded else 4) + 2
    sizes = _allocate(
        height,
        0,
        minimums=[("stats", 3), ("tools", 4)],
        steps=[("stats", _stats_pref(right_w)), ("tools", tools_pref)],
        elastic="tools",
    )
    right = Table.grid(expand=True, padding=0)
    right.add_column(ratio=1)
    right.add_row(_stats_panel(state, right_w, sizes["stats"]))
    if "tools" in sizes:
        right.add_row(_tools_panel(state, right_w, sizes["tools"]))

    columns = Table.grid(expand=True, padding=0)
    columns.add_column(width=left_w, no_wrap=True)
    columns.add_column(ratio=1)
    columns.add_row(_jobs_panel(state, left_w, height), right)
    return columns


def _narrow_middle(state: RuntimeState, width: int, height: int) -> RenderableType:
    """One column: a statistics strip, the schedule, then tools if rows remain."""
    jobs_pref = max(len(state.jobs), 1) + 2
    tools_pref = (len(state.tools.categories) if state.tools.loaded else 4) + 2
    minimums = [("jobs", 5), ("stats", 3)]
    # Tools only appear once the schedule is complete too; a lone row plus
    # "+N more" tells the operator nothing, and the schedule matters more.
    if height >= 3 + jobs_pref + tools_pref:
        minimums.append(("tools", tools_pref))
    sizes = _allocate(
        height,
        0,
        minimums=minimums,
        steps=[("jobs", jobs_pref), ("stats", _stats_pref(width))],
        elastic="jobs",
    )
    stack: List[RenderableType] = []
    if "stats" in sizes:
        stack.append(_stats_panel(state, width, sizes["stats"]))
    stack.append(_jobs_panel(state, width, sizes["jobs"]))
    if "tools" in sizes:
        stack.append(_tools_panel(state, width, sizes["tools"]))
    return Group(*stack)


def _stats_panel(state: RuntimeState, width: int, height: int) -> RenderableType:
    return _panel(
        f"{theme.ICON_TASKS}  TASK STATISTICS",
        _task_body(state, width - 4, height - 2),
        height=height,
    )


def _jobs_panel(state: RuntimeState, width: int, height: int) -> RenderableType:
    return _panel(
        f"{theme.ICON_SCHEDULER}  SCHEDULER JOBS ({len(state.jobs)})",
        _job_list(state.jobs, height - 2, width - 4),
        height=height,
    )


def _tools_panel(state: RuntimeState, width: int, height: int) -> RenderableType:
    return _panel(
        f"{theme.ICON_TOOLS}  {_tools_title(state)}",
        _tool_list(state, height - 2, width - 4),
        height=height,
    )


def _task_body(state: RuntimeState, inner_w: int, inner_h: int) -> RenderableType:
    card_w = (inner_w - 3) // 4
    if inner_h < 4 or card_w < 7:
        return _task_line(state, inner_w)

    card_h = 5 if inner_h >= 5 else 4
    full_labels = card_w >= 11
    grid = Table.grid(padding=0)
    for index in range(4):
        if index:
            grid.add_column(width=1, no_wrap=True)
        grid.add_column(width=card_w, no_wrap=True)
    cells: List[RenderableType] = []
    for label, short, icon, attribute, colour in _TASK_CARDS:
        value = getattr(state.tasks, attribute)
        shown = str(value) if state.tasks.known else "—"
        title = Align.center(Text(label if full_labels else short, style=colour, no_wrap=True))
        if card_h == 5:
            lines: List[RenderableType] = [
                title,
                Align.center(Text(icon, style=colour)),
                Align.center(Text(shown, style=f"bold {colour}")),
            ]
        else:
            lines = [title, Align.center(Text(f"{icon} {shown}", style=f"bold {colour}", no_wrap=True))]
        cells.append(
            Panel(
                Group(*lines),
                box=_rounded(),
                border_style=theme.BORDER,
                padding=(0, 0),
                style=theme.PANEL_INNER,
                height=card_h,
            )
        )
    grid.add_row(*_interleave(cells))

    body: List[RenderableType] = [grid]
    if state.tasks.other and inner_h > card_h:
        body.append(
            Text(
                _clip(f"other states: {state.tasks.other}   total: {state.tasks.total}", inner_w),
                style=theme.MUTED,
                no_wrap=True,
            )
        )
    return Group(*body)


def _task_line(state: RuntimeState, inner_w: int) -> RenderableType:
    """Statistics on a single line, for panels too short or narrow for cards."""
    for with_labels in (True, False):
        line = Text(no_wrap=True)
        for index, (label, _short, icon, attribute, colour) in enumerate(_TASK_CARDS):
            value = getattr(state.tasks, attribute)
            shown = str(value) if state.tasks.known else "—"
            if index:
                line.append("   " if with_labels else "  ")
            line.append(f"{icon} {shown}", style=f"bold {colour}")
            if with_labels:
                line.append(" " + label.lower(), style=colour)
        if line.cell_len <= inner_w:
            break
    line.truncate(max(1, inner_w), overflow="ellipsis")
    return line


def _job_list(jobs: Sequence[JobView], rows: int, inner_w: int) -> RenderableType:
    if not jobs:
        return Text("scheduler configuration unavailable", style=theme.YELLOW)
    rows = max(1, rows)
    # The note column only exists when something has a note and there is room.
    with_notes = any(job.detail for job in jobs) and inner_w >= 70
    hidden = max(0, len(jobs) - rows)
    shown = list(jobs[: rows - 1]) if hidden else list(jobs)

    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    grid.add_column(width=13, justify="right", no_wrap=True)
    if with_notes:
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(overflow="ellipsis", no_wrap=True)
    for job in shown:
        interval = f"every {job.interval_minutes} min" if job.interval_minutes else "no interval"
        cells: List[RenderableType] = [
            Text(job.marker + " ", style=job.marker_style),
            Text(job.label, style=theme.TEXT if job.active is not False else theme.MUTED),
            Text(interval, style=theme.CYAN),
        ]
        if with_notes:
            cells += [Text(" "), Text(job.detail, style=theme.FAINT)]
        grid.add_row(*cells)
    if hidden:
        more: List[RenderableType] = [Text(""), Text(f"… +{len(jobs) - len(shown)} more", style=theme.MUTED), Text("")]
        if with_notes:
            more += [Text(""), Text("")]
        grid.add_row(*more)
    return grid


def _tools_title(state: RuntimeState) -> str:
    if not state.tools.loaded:
        return "REGISTERED TOOLS (unavailable)"
    return f"REGISTERED TOOLS ({state.tools.total})"


def _tool_list(state: RuntimeState, rows: int, inner_w: int) -> RenderableType:
    if not state.tools.loaded:
        detail = state.tools.detail or "registry could not be loaded"
        return Text(detail, style=theme.YELLOW, overflow="ellipsis")
    rows = max(1, rows)
    categories = list(state.tools.categories)
    hidden = max(0, len(categories) - rows)
    shown = categories[: rows - 1] if hidden else categories

    with_badge = inner_w >= 46
    bare_count = inner_w < 30
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    grid.add_column(justify="right", no_wrap=True, width=3 if bare_count else 9)
    if with_badge:
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(width=8, no_wrap=True)
    for category in shown:
        row: List[RenderableType] = [
            Text(category.icon + " ", style=theme.CYAN),
            Text(category.label, style=theme.TEXT),
            Text(str(category.count) if bare_count else f"{category.count} tools", style=theme.CYAN),
        ]
        if with_badge:
            row += [Text(""), Text(category.badge, style=category.badge_style) if category.badge else Text("")]
        grid.add_row(*row)
    if hidden:
        row = [Text(""), Text(f"… +{len(categories) - len(shown)} more", style=theme.MUTED), Text("")]
        if with_badge:
            row += [Text(""), Text("")]
        grid.add_row(*row)
    return grid


# ----------------------------------------------------------------------
# Activity + alerts
# ----------------------------------------------------------------------
def _hint(state: RuntimeState, keyword: str, hint: str) -> str:
    """Bottom-border text of a log panel: the last confirmation if it concerns
    this panel and is still fresh, otherwise the key hints."""
    notice = state.notice or ""
    if keyword in notice:
        return notice
    if state.keys_error and keyword == "ACTIVITY":
        return f"keys off: {state.keys_error}"
    return hint


def _activity_hint(state: RuntimeState, rows: int) -> str:
    total = len(state.events)
    offset = max(0, min(state.activity_scroll, total - max(1, rows)))
    if offset:
        return f"↑ {offset} older lines · [End] latest"
    if total > rows:
        return "[a] copy · [x] clear · ↑↓ / wheel scroll"
    return "[a] copy all · [x] clear"


def _bottom_row(state: RuntimeState, width: int, height: int) -> RenderableType:
    activity = _panel(
        f"{theme.ICON_ACTIVITY}  RECENT ACTIVITY ({len(state.events)})",
        _activity(state, height - 2),
        height=height,
        subtitle=_hint(state, "ACTIVITY", _activity_hint(state, height - 2)),
    )
    alerts = _panel(
        f"{theme.ICON_ALERTS}  WARNINGS & ERRORS ({len(state.alerts)})",
        _alerts(state, height - 2),
        height=height,
        border_style=theme.BORDER if not state.alerts else theme.YELLOW,
        subtitle=_hint(state, "WARNINGS", "[w] copy all · [z] clear"),
    )
    if width < 90:
        # Not enough width for both: show whichever matters right now.
        return alerts if state.alerts else activity
    columns = Table.grid(expand=True, padding=0)
    if width >= MEDIUM:
        columns.add_column(width=width - _side_width(width), no_wrap=True)
        columns.add_column(ratio=1)
    else:
        columns.add_column(ratio=2)
        columns.add_column(ratio=1)
    columns.add_row(activity, alerts)
    return columns


def _activity(state: RuntimeState, rows: int) -> RenderableType:
    rows = max(1, rows)
    state.activity_rows = rows
    all_events = list(state.events)
    if not all_events:
        return Text("waiting for events…", style=theme.MUTED)
    # Scrolled back by `activity_scroll` lines from the newest; 0 follows live.
    offset = max(0, min(state.activity_scroll, len(all_events) - rows))
    end = len(all_events) - offset
    events = all_events[max(0, end - rows) : end]
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
    rows = max(1, rows)
    if not alerts:
        if state.everything_operational:
            subtitle = "All systems operational"
            subtitle_style = theme.MUTED
        else:
            subtitle = "No logged warnings or errors"
            subtitle_style = theme.YELLOW
        if rows < 8:
            # Too short for the framed check mark: the same message on plain lines.
            lines: List[RenderableType] = [
                Text.assemble((theme.DOT_SUCCESS + " ", "bold " + theme.GREEN), ("No warnings or errors", theme.TEXT_STRONG), no_wrap=True)
            ]
            if rows >= 2:
                lines.append(Text(subtitle, style=subtitle_style, no_wrap=True))
            return Group(*lines)
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

    room = rows - 1 if rows > 1 else 1
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=9, no_wrap=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    for event in alerts[: max(1, room)]:
        marker, colour = theme.LEVEL_STYLE.get(event.level, (theme.DOT_ERROR, theme.RED))
        grid.add_row(
            Text(event.time + " ", style=theme.MUTED),
            Text(marker + " " + event.message, style=colour),
        )
    if rows == 1:
        return grid
    return Group(grid, Text(f"{len(alerts)} active alert(s)", style=theme.MUTED))


# ----------------------------------------------------------------------
# Footer
# ----------------------------------------------------------------------
def _footer_block(state: RuntimeState, width: int, height: int) -> Tuple[RenderableType, int]:
    line = _footer_line(state, width)
    if height >= 30:
        return Group(Text.from_markup(theme.gradient_rule(width)), line), 2
    return line, 1


def _footer_line(state: RuntimeState, width: int) -> Text:
    """One footer row; low-priority items are dropped until the row fits."""
    seconds = int(state.uptime_seconds)
    uptime = f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"
    sep = ("  │  ", theme.FAINT)

    lefts = [
        [("AirdropOS", "bold " + theme.CYAN), sep, ("Autonomous Airdrop Agent", theme.MUTED), sep,
         ("Press ", theme.MUTED), ("Ctrl+C", "bold " + theme.YELLOW), (" to stop everything", theme.MUTED)],
        [("AirdropOS", "bold " + theme.CYAN), sep,
         ("Press ", theme.MUTED), ("Ctrl+C", "bold " + theme.YELLOW), (" to stop", theme.MUTED)],
        [("Ctrl+C", "bold " + theme.YELLOW), (" to stop", theme.MUTED)],
    ]
    up = (f"Uptime: {uptime}", theme.MUTED)
    level = (f"Log level: {(state.log_level or '?').upper()}", theme.MUTED)
    logs = (f"Logs: {state.log_path or 'logs/airdropos.log'}", theme.MUTED)
    rights = [
        [up, sep, level, sep, logs],
        [up, sep, level],
        [up],
        [],
    ]
    for left_index, right_index in ((0, 0), (1, 0), (1, 1), (1, 2), (2, 2), (2, 3)):
        left = Text.assemble(*lefts[left_index])
        right = Text.assemble(*rights[right_index]) if rights[right_index] else Text("")
        if left.cell_len + right.cell_len + (2 if right.cell_len else 0) <= width:
            padding = max(1 if right.cell_len else 0, width - left.cell_len - right.cell_len)
            return Text.assemble(left, (" " * padding, ""), right, no_wrap=True)
    fallback = Text.assemble(*lefts[2])
    fallback.truncate(width, overflow="ellipsis")
    return fallback


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


def _panel(
    title: str,
    body: RenderableType,
    *,
    height: int,
    border_style: str = theme.BORDER,
    subtitle: str = "",
) -> RenderableType:
    return Panel(
        body,
        title=Text(title, style="bold " + theme.CYAN, no_wrap=True),
        title_align="left",
        subtitle=Text(subtitle, style=theme.MUTED, no_wrap=True) if subtitle else None,
        subtitle_align="right",
        border_style=border_style,
        box=_rounded(),
        padding=(0, 1),
        style=theme.PANEL,
        height=height,
    )


def _pill(label: str, colour: str) -> RenderableType:
    return Panel(
        Text(label, style="bold " + colour, no_wrap=True),
        box=_rounded(),
        border_style=colour,
        padding=(0, 1),
        style=theme.PANEL_INNER,
        height=3,
    )


def _rounded():
    from rich import box

    return box.ROUNDED


__all__ = ["build"]
