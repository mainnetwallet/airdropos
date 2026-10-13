"""Colours and glyphs for the terminal dashboard.

One place decides both, so the panels cannot drift apart: every section uses the
same accent for the same meaning and the same shape of glyph for the same kind
of icon.

Glyphs are deliberately monochrome terminal characters from the Geometric
Shapes / Miscellaneous Technical blocks. Nothing here is an emoji. A terminal
cannot scale a glyph, so the brand mark is drawn from a small set of the same
characters at a larger size rather than from a pictographic symbol that would
render as a colourful emoji on some terminals and as tofu on others.
"""

from __future__ import annotations

from typing import Sequence

# ----------------------------------------------------------------------
# Palette
# ----------------------------------------------------------------------
BACKGROUND = "#060a14"
PANEL = "#0b1220"
PANEL_INNER = "#0d1526"
BORDER = "#1d4e73"
BORDER_BRIGHT = "#22d3ee"

CYAN = "#22d3ee"
BLUE = "#3b82f6"
PURPLE = "#a855f7"

GREEN = "#4ade80"
GREEN_DIM = "#166534"
YELLOW = "#fbbf24"
RED = "#f87171"

TEXT = "#cbd5e1"
TEXT_STRONG = "#f1f5f9"
MUTED = "#64748b"
FAINT = "#334155"

GRADIENT = (CYAN, BLUE, PURPLE)

# ----------------------------------------------------------------------
# Section headings
# ----------------------------------------------------------------------
# Every glyph here measures exactly one terminal cell. U+2630 (TRIGRAM FOR
# HEAVEN) is deliberately avoided: terminals disagree about whether it is
# narrow or wide, so it shifts a whole column. Same reason there is no emoji:
# an emoji renders as colour in one font and as a missing glyph in another.
ICON_SYSTEM = "◈"
ICON_CONFIG = "⚙"
ICON_TASKS = "▦"
ICON_SCHEDULER = "◷"
ICON_TOOLS = "⬡"
ICON_ACTIVITY = "≡"
ICON_ALERTS = "⚠"

# Service cards
ICON_BACKEND = "▤"
ICON_FRONTEND = "▭"
ICON_WORKER = "⚙"
ICON_GATEWAY = "◉"

# Task statistic cards
ICON_RUNNING = "↻"
ICON_PENDING = "◷"
ICON_COMPLETED = "✓"
ICON_FAILED = "✕"

# Tool categories
ICON_CAT_BROWSER = "▭"
ICON_CAT_TELEGRAM = "✈"
ICON_CAT_CHAIN = "⬡"
ICON_CAT_RESEARCH = "◉"
ICON_CAT_TASKS = "≡"
ICON_CAT_REPORT = "▤"
ICON_CAT_SYSTEM = "⚙"
ICON_CAT_OTHER = "⋯"

# Log level markers
DOT_INFO = "●"
DOT_SUCCESS = "●"
DOT_WARNING = "▲"
DOT_ERROR = "✕"
DOT_DEBUG = "·"

# Service state markers
STATE_MARK = {
    "running": ("●", GREEN),
    "starting": ("◌", YELLOW),
    "stopped": ("○", RED),
    "error": ("✕", RED),
    "disabled": ("○", MUTED),
    "unknown": ("?", YELLOW),
}

LEVEL_STYLE = {
    "DEBUG": (DOT_DEBUG, FAINT),
    "INFO": (DOT_INFO, CYAN),
    "SUCCESS": (DOT_SUCCESS, GREEN),
    "WARNING": (DOT_WARNING, YELLOW),
    "ERROR": (DOT_ERROR, RED),
    "CRITICAL": (DOT_ERROR, RED),
}

# ----------------------------------------------------------------------
# Brand mark
# ----------------------------------------------------------------------
# A three-row rocket assembled from the same glyph family as the rest of the
# dashboard: nose, body with fins, exhaust.
LOGO_MARK = ("▲", "◣█▌◣", "▀▀")
LOGO_STYLES = (CYAN, BLUE, YELLOW)

SUBTITLE = "A U T O N O M O U S   A I R D R O P   A G E N T"


def gradient_text(text: str, colors: Sequence[str] = GRADIENT, style: str = "bold") -> str:
    """Per-character colour interpolation, as an escaped Rich markup string.

    Interpolating between three anchors by character position keeps one word
    reading as a single gradient at any width instead of as three flat blocks.
    """
    if not text:
        return ""
    n = len(colors)
    out = []
    for i, char in enumerate(text):
        if char == " ":
            out.append(char)
            continue
        position = (i / max(1, len(text) - 1)) * (n - 1)
        lo = min(n - 2, int(position))
        frac = position - lo
        out.append(f"[{style} {blend(colors[lo], colors[lo + 1], frac)}]{char}[/]")
    return "".join(out)


def blend(left: str, right: str, fraction: float) -> str:
    """Linear RGB blend between two ``#rrggbb`` colours."""
    a = _rgb(left)
    b = _rgb(right)
    f = max(0.0, min(1.0, fraction))
    return "#{:02x}{:02x}{:02x}".format(
        round(a[0] + (b[0] - a[0]) * f),
        round(a[1] + (b[1] - a[1]) * f),
        round(a[2] + (b[2] - a[2]) * f),
    )


def _rgb(colour: str) -> tuple[int, int, int]:
    value = colour.lstrip("#")
    if len(value) != 6:
        return (255, 255, 255)
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return (255, 255, 255)


def gradient_rule(width: int, colors: Sequence[str] = GRADIENT, char: str = "─") -> str:
    """A full-width horizontal rule that fades cyan -> blue -> purple."""
    width = max(1, width)
    out = []
    n = len(colors)
    for i in range(width):
        position = (i / max(1, width - 1)) * (n - 1)
        lo = min(n - 2, int(position))
        out.append(f"[{blend(colors[lo], colors[lo + 1], position - lo)}]{char}")
    out.append("[/]")
    return "".join(out)