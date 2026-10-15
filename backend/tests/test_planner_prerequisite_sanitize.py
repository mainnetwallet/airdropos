"""A plan step must never be gated on retry/execution state (e.g. "retry_count<3")."""
from types import SimpleNamespace

from backend.app.planning.planner import Planner, sanitize_prerequisites


def test_retry_prerequisites_are_dropped():
    assert sanitize_prerequisites(["retry_count<3", "max_retries>=1", "Status == X"]) == []


def test_other_prerequisites_are_kept():
    assert sanitize_prerequisites(["deadline_at>2026-01-01", "  "]) == ["deadline_at>2026-01-01"]


def test_bad_input_is_empty():
    assert sanitize_prerequisites(None) == []
    assert sanitize_prerequisites("retry_count<3") == []


def test_retry_3_of_3_would_have_blocked_before():
    task = SimpleNamespace(retry_count=3, max_retries=3)
    assert Planner._check_expression(task, "retry_count<3") is False  # the old failure
    assert sanitize_prerequisites(["retry_count<3"]) == []             # now never reaches it
