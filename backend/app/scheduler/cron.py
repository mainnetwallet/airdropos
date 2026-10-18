"""Small, dependency-free cron support for user-defined schedules.

Standard 5-field syntax: ``minute hour day-of-month month day-of-week``.

Supported: ``*``, lists (``1,15``), ranges (``9-17``), steps (``*/15``, ``0-30/10``),
month names (``JAN``..``DEC``) and weekday names (``SUN``..``SAT``). Day-of-week accepts
``0-7`` with both ``0`` and ``7`` meaning Sunday. As in Vixie cron, when BOTH day-of-month
and day-of-week are restricted a day matches if EITHER does.

Evaluation happens in the schedule's own timezone and the result is returned in UTC, so
"every day at 09:00 Asia/Dhaka" stays at 09:00 local across daylight-saving changes.
Local times that do not exist (a spring-forward gap) are skipped.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import FrozenSet, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CronError(ValueError):
    """The expression or timezone is not valid."""


_MONTHS = {n: i for i, n in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_DOW = {n: i for i, n in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}

# (name, lowest, highest, aliases)
_FIELDS: Tuple[Tuple[str, int, int, dict], ...] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day-of-month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day-of-week", 0, 7, _DOW),
)

# Searching further than this without a hit means the expression can never fire
# (for example ``0 0 31 2 *``).
_MAX_SEARCH_DAYS = 366 * 8


@dataclass(frozen=True)
class CronSpec:
    minutes: Tuple[int, ...]
    hours: Tuple[int, ...]
    days: FrozenSet[int]
    months: FrozenSet[int]
    weekdays: FrozenSet[int]  # 0=Sunday .. 6=Saturday
    dom_restricted: bool
    dow_restricted: bool
    expression: str

    def day_matches(self, year: int, month: int, day: int) -> bool:
        if month not in self.months:
            return False
        dom_ok = day in self.days
        # Python: Monday=0; cron: Sunday=0.
        dow = (datetime(year, month, day).weekday() + 1) % 7
        dow_ok = dow in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True


def _parse_value(token: str, lo: int, hi: int, aliases: dict, field: str) -> int:
    token = token.strip().upper()
    if token in aliases:
        return aliases[token]
    if not token.isdigit():
        raise CronError(f"Invalid value '{token}' in the {field} field")
    value = int(token)
    if value < lo or value > hi:
        raise CronError(f"{field} value {value} is outside {lo}-{hi}")
    return value


def _parse_field(text: str, lo: int, hi: int, aliases: dict, field: str) -> Tuple[FrozenSet[int], bool]:
    """Returns (values, restricted). Like Vixie cron, a field starting with ``*`` is unrestricted."""
    values: set = set()
    restricted = not text.strip().startswith(("*", "?"))
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"Empty item in the {field} field")
        step = 1
        if "/" in part:
            base, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"Invalid step '{step_text}' in the {field} field")
            step = int(step_text)
        else:
            base = part
        if base == "*" or base == "?":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            start = _parse_value(a, lo, hi, aliases, field)
            end = _parse_value(b, lo, hi, aliases, field)
            if start > end:
                raise CronError(f"Range {base} runs backwards in the {field} field")
        else:
            start = _parse_value(base, lo, hi, aliases, field)
            end = hi if "/" in part else start
        values.update(range(start, end + 1, step))
    return frozenset(values), restricted


def parse(expression: str) -> CronSpec:
    parts = (expression or "").split()
    if len(parts) != 5:
        raise CronError("A cron expression needs exactly 5 fields: minute hour day-of-month month day-of-week")
    parsed = []
    for text, (name, lo, hi, aliases) in zip(parts, _FIELDS):
        parsed.append(_parse_field(text, lo, hi, aliases, name))
    (minutes, _), (hours, _), (days, dom_r), (months, _), (dow, dow_r) = parsed
    weekdays = frozenset(0 if d == 7 else d for d in dow)
    return CronSpec(
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        days=days,
        months=months,
        weekdays=weekdays,
        dom_restricted=dom_r,
        dow_restricted=dow_r,
        expression=" ".join(parts),
    )


def get_timezone(name: Optional[str]) -> tzinfo:
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise CronError(f"Unknown timezone '{name}'") from exc


def _exists(naive: datetime, tz: tzinfo) -> Optional[datetime]:
    """The UTC instant for a local wall-clock time, or None if it falls in a DST gap."""
    aware = naive.replace(tzinfo=tz)
    back = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if back != naive:
        return None
    return aware.astimezone(timezone.utc)


def next_after(spec: CronSpec, after: datetime, tz: tzinfo = timezone.utc) -> datetime:
    """The first run strictly after ``after`` (an aware datetime), as UTC."""
    if after.tzinfo is None:
        raise CronError("'after' must be timezone-aware")
    cursor = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
    limit = cursor + timedelta(days=_MAX_SEARCH_DAYS)

    while cursor < limit:
        if not spec.day_matches(cursor.year, cursor.month, cursor.day):
            cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        hours = spec.hours
        hi = bisect_left(hours, cursor.hour)
        found: Optional[datetime] = None
        while hi < len(hours) and found is None:
            hour = hours[hi]
            min_floor = cursor.minute if hour == cursor.hour else 0
            mi = bisect_left(spec.minutes, min_floor)
            if mi < len(spec.minutes):
                found = cursor.replace(hour=hour, minute=spec.minutes[mi])
            else:
                hi += 1
        if found is None:
            cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        instant = _exists(found, tz)
        if instant is not None:
            return instant
        cursor = found + timedelta(minutes=1)  # DST gap: that wall-clock time never happens

    raise CronError("This expression never fires (for example, February 31st)")


def upcoming(expression: str, after: datetime, tz: tzinfo = timezone.utc, count: int = 5) -> List[datetime]:
    spec = parse(expression)
    out: List[datetime] = []
    cursor = after
    for _ in range(count):
        cursor = next_after(spec, cursor, tz)
        out.append(cursor)
    return out
