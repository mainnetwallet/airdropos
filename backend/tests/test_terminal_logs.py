"""RECENT ACTIVITY and WARNINGS & ERRORS keep the newest 150 lines, and each can be
copied or cleared on its own."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from backend.app.terminal import controls
from backend.app.terminal.state import HISTORY_LIMIT, Collector, EventLine, EventTail, RuntimeState


class _Tail(EventTail):
    """EventTail fed from memory instead of a file."""

    def __init__(self) -> None:
        self.batch: list = []

    def poll(self, limit: int = 400):
        out, self.batch = self.batch, []
        return out


def make_collector() -> Collector:
    c = Collector.__new__(Collector)
    from collections import deque

    c.tail = _Tail()
    c.activity = deque(maxlen=HISTORY_LIMIT)
    c.alert_log = deque(maxlen=HISTORY_LIMIT)
    c._seq = 0
    return c


def feed(c: Collector, n: int, level: str = "INFO", start: int = 0) -> None:
    c.tail.batch = [
        EventLine(time.time() + i, "00:00:%02d" % (i % 60), level, "x", "p", f"msg {start + i}")
        for i in range(n)
    ]
    # the same steps collect() performs, without the network/database probes
    for event in c.tail.poll():
        c._seq += 1
        event.seq = c._seq
        c.activity.append(event)
        if event.is_alert:
            c.alert_log.append(event)


def test_keeps_only_the_newest_150_and_drops_the_oldest():
    c = make_collector()
    feed(c, 100)
    feed(c, 100, start=100)
    messages = [e.message for e in c.activity]
    assert len(messages) == HISTORY_LIMIT
    assert messages[0] == "msg 50" and messages[-1] == "msg 199"


def test_history_survives_between_readings():
    c = make_collector()
    feed(c, 10)
    feed(c, 0)  # a reading with no new events must not empty the panel
    assert len(c.activity) == 10


def test_alerts_have_their_own_150_and_are_not_pushed_out_by_info():
    c = make_collector()
    feed(c, 5, level="ERROR")
    feed(c, 400, level="INFO", start=1000)
    assert len(c.alert_log) == 5
    assert len(c.activity) == HISTORY_LIMIT


def test_clear_is_independent_per_panel():
    c = make_collector()
    feed(c, 7, level="WARNING")
    feed(c, 3, level="INFO", start=50)
    assert c.clear_activity() == 10
    assert len(c.activity) == 0 and len(c.alert_log) == 7
    assert c.clear_alerts() == 7
    assert len(c.alert_log) == 0


def test_cleared_alerts_are_not_rebuilt_from_activity():
    state = RuntimeState(started_at=time.time())
    state.events.append(EventLine(time.time(), "00:00:01", "ERROR", "x", "p", "boom"))
    state.alerts_tracked = True  # a collector owns the alert buffer, and it is empty
    assert state.alerts == []


def test_copy_text_is_one_line_per_record_oldest_first():
    events = [
        EventLine(1.0, "10:00:00", "INFO", "api", "backend", "started"),
        EventLine(2.0, "10:00:05", "ERROR", "api", "worker", "failed"),
    ]
    assert controls.format_lines(events).split("\n") == [
        "10:00:00 INFO     backend | started",
        "10:00:05 ERROR    worker | failed",
    ]
