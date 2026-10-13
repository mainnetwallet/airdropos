"""Regression tests for AIR-2026-000005: captured evidence was never linked to its task.

``capture_evidence`` saved a screenshot but nothing wrote a ``TaskEvidence`` row, so
``get_task`` kept reporting ``evidence_count == 0``. Separately, ``Task.description`` was stored
but never returned by ``get_task``.

These tests run the REAL tools, the REAL ``ToolRegistry.invoke`` (so the task context travels the
same way it does in production) and a real in-memory SQLite database (see ``conftest.py``). Only
the browser page is faked, so nothing here opens Chrome, a wallet, or the network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.browser import controller as controller_module
from backend.app.browser import profiles as profile_store
from backend.app.browser.controller import controller
from backend.app.database.session import session_scope
from backend.app.models import Task, TaskCheckpoint, TaskEvent, TaskEvidence
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus
from backend.app.tools.base import ToolResultStatus
from backend.app.tools.browser_tools import CaptureEvidenceTool
from backend.app.tools.context import get_current_task_id, task_context
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry
from backend.app.tools.task_tools import CreateTaskTool, GetTaskTool

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 8
POLICY = AuthorizationPolicy(enabled_permissions=["browser.read", "task.read", "task.write"])


class FakePage:
    """Stands in for a Playwright page: writes bytes where the controller asks."""

    def __init__(self, payload: bytes = PNG) -> None:
        self.payload = payload
        self.shots = 0

    def is_closed(self) -> bool:
        return False

    async def screenshot(self, path: str, full_page: bool = True, timeout: int = 0) -> None:
        self.shots += 1
        Path(path).write_bytes(self.payload + str(self.shots).encode())


@pytest.fixture
def shots_dir(tmp_path, monkeypatch) -> Path:
    """Screenshots go to a throwaway directory; profile metadata never touches the real one."""
    root = tmp_path / "userdata"
    root.mkdir()
    monkeypatch.setattr(controller_module, "USER_DATA_ROOT", root)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(profile_store, "ROOT", sessions)
    monkeypatch.setattr(profile_store, "_meta_path", lambda: sessions / "profiles.json")
    return root / "screenshots"


@pytest.fixture
def page(monkeypatch) -> FakePage:
    fake = FakePage()

    async def _get_page(_key):
        return fake

    monkeypatch.setattr(controller, "get_page", _get_page)
    return fake


@pytest.fixture
def reg() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(CaptureEvidenceTool())
    registry.register(GetTaskTool())
    registry.register(CreateTaskTool())
    registry.policy = POLICY
    return registry


async def _new_task(reg, title="Evidence task", description="Check the claim page and keep proof.") -> str:
    result = await reg.invoke(
        "create_agent_task", {"title": title, "description": description}, policy=POLICY
    )
    assert result.ok, result.error
    return result.data["task_id"]


async def _capture(reg, task_id, **params):
    params.setdefault("session_key", "evtest")
    return await reg.invoke("capture_evidence", params, policy=POLICY, task_id=task_id)


async def _get(reg, task_id) -> dict:
    result = await reg.invoke("get_task", {"task_id": task_id}, policy=POLICY)
    assert result.ok, result.error
    return result.data


async def _rows(task_id: str):
    async with session_scope() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        rows = (await db.execute(select(TaskEvidence).where(TaskEvidence.task_id == task.id))).scalars().all()
        return task.id, list(rows)


# ----------------------------------------------------------------------
# The bug itself: capture -> get_task
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_capture_is_linked_and_visible_through_get_task(reg, page, shots_dir):
    task_id = await _new_task(reg)
    assert (await _get(reg, task_id))["evidence_count"] == 0

    result = await _capture(reg, task_id, label="claim-page")

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["evidence_linked"] is True
    report = await _get(reg, task_id)
    assert report["evidence_count"] >= 1
    assert len(report["evidence"]) == report["evidence_count"] == 1


@pytest.mark.asyncio
async def test_evidence_references_the_correct_task_and_file(reg, page, shots_dir):
    task_id = await _new_task(reg)
    other_id = await _new_task(reg, title="A different task")
    result = await _capture(reg, task_id, label="proof")

    internal_pk, rows = await _rows(task_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == internal_pk  # internal UUID, not the public id
    assert row.uri_reference == result.data["screenshot_path"]
    assert row.evidence_type == "SCREENSHOT" and row.source == "capture_evidence"
    meta = json.loads(row.content)
    assert meta["task_public_id"] == task_id and meta["label"] == "proof"

    entry = (await _get(reg, task_id))["evidence"][0]
    assert entry["task_id"] == task_id and entry["uri"] == row.uri_reference
    assert entry["label"] == "proof" and entry["id"] == str(row.id)
    assert (await _get(reg, other_id))["evidence_count"] == 0  # not attached to the wrong task


@pytest.mark.asyncio
async def test_referenced_file_exists_and_hash_matches(reg, page, shots_dir):
    task_id = await _new_task(reg)
    await _capture(reg, task_id)

    entry = (await _get(reg, task_id))["evidence"][0]
    path = Path(entry["uri"])
    assert path.is_file() and path.parent == shots_dir
    data = path.read_bytes()
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert entry["size_bytes"] == len(data) > 0
    assert entry["captured_at"] and entry["recorded_at"]


@pytest.mark.asyncio
async def test_two_captures_make_two_distinct_records(reg, page, shots_dir):
    task_id = await _new_task(reg)
    first = await _capture(reg, task_id, label="before")
    second = await _capture(reg, task_id, label="after")

    assert first.data["evidence_count"] == 1 and second.data["evidence_count"] == 2
    report = await _get(reg, task_id)
    assert report["evidence_count"] == 2
    assert {e["label"] for e in report["evidence"]} == {"before", "after"}
    assert len({e["uri"] for e in report["evidence"]}) == 2
    assert len({e["sha256"] for e in report["evidence"]}) == 2  # distinct content, distinct hash
    assert all(Path(e["uri"]).is_file() for e in report["evidence"])  # nothing overwritten


@pytest.mark.asyncio
async def test_get_task_evidence_count_matches_authoritative_rows(reg, page, shots_dir):
    task_id = await _new_task(reg)
    for n in range(3):
        await _capture(reg, task_id, label=f"shot-{n}")
    _, rows = await _rows(task_id)
    async with session_scope() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        counted = await db.scalar(select(func.count(TaskEvidence.id)).where(TaskEvidence.task_id == task.id))
    assert (await _get(reg, task_id))["evidence_count"] == len(rows) == counted == 3


# ----------------------------------------------------------------------
# Input handling
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_label_over_100_chars_is_rejected_cleanly(reg, page, shots_dir):
    task_id = await _new_task(reg)
    too_long = "x" * 101

    via_registry = await _capture(reg, task_id, label=too_long)
    assert via_registry.status is ToolResultStatus.FAILURE
    assert via_registry.error_category == "INVALID_INPUT"

    with task_context(task_id):  # a direct caller bypasses schema validation
        direct = await CaptureEvidenceTool().execute({"session_key": "evtest", "label": too_long})
    assert direct.error_category == "INVALID_INPUT"

    assert page.shots == 0 and not shots_dir.exists()  # nothing was captured
    assert (await _get(reg, task_id))["evidence_count"] == 0

    exactly_100 = await _capture(reg, task_id, label="y" * 100)
    assert exactly_100.ok


@pytest.mark.asyncio
async def test_label_cannot_escape_the_screenshots_directory(reg, page, shots_dir):
    task_id = await _new_task(reg)
    result = await _capture(reg, task_id, label="../../escape")
    path = Path(result.data["screenshot_path"])
    assert path.parent == shots_dir
    assert (await _get(reg, task_id))["evidence"][0]["label"] == "../../escape"  # raw label kept for audit


# ----------------------------------------------------------------------
# Missing context and failure modes
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_task_context_is_explicit_and_creates_no_record(reg, page, shots_dir):
    task_id = await _new_task(reg)
    result = await reg.invoke("capture_evidence", {"session_key": "evtest"}, policy=POLICY)  # no task_id

    assert result.status is ToolResultStatus.SUCCESS  # the screenshot itself worked
    assert result.data["evidence_linked"] is False
    assert "NOT linked" in result.data["warning"]
    assert result.evidence["authoritative"] is False
    assert Path(result.data["screenshot_path"]).is_file()
    assert (await _get(reg, task_id))["evidence_count"] == 0
    async with session_scope() as db:
        assert await db.scalar(select(func.count(TaskEvidence.id))) == 0


@pytest.mark.asyncio
async def test_unknown_task_is_reported_and_creates_no_record(reg, page, shots_dir):
    result = await _capture(reg, "AIR-2026-999999")

    assert result.status is ToolResultStatus.FAILURE
    assert result.error_category == "TASK_NOT_FOUND"
    assert result.data["evidence_linked"] is False
    assert Path(result.data["screenshot_path"]).is_file()  # the file is kept, never deleted
    async with session_scope() as db:
        assert await db.scalar(select(func.count(TaskEvidence.id))) == 0


@pytest.mark.asyncio
async def test_database_write_failure_is_not_reported_as_success(reg, page, shots_dir, monkeypatch):
    task_id = await _new_task(reg)

    async def boom(self, *a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(TaskManager, "record_evidence", boom)
    result = await _capture(reg, task_id, label="will-not-link")

    assert result.status is ToolResultStatus.FAILURE
    assert result.error_category == "EVIDENCE_PERSIST_FAILED"
    assert result.retryable is True
    assert result.data["evidence_linked"] is False
    assert Path(result.data["screenshot_path"]).is_file()  # file preserved, honestly reported as unlinked
    assert (await _get(reg, task_id))["evidence_count"] == 0


@pytest.mark.asyncio
async def test_commit_failure_is_not_reported_as_success(reg, page, shots_dir, monkeypatch):
    """The failure can surface at COMMIT (session_scope exit), after the insert looked fine."""
    task_id = await _new_task(reg)
    from sqlalchemy.ext.asyncio import AsyncSession

    real_commit = AsyncSession.commit
    state = {"armed": True}

    async def failing_commit(self):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("disk I/O error")
        return await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    result = await _capture(reg, task_id)

    assert result.status is ToolResultStatus.FAILURE
    assert result.error_category == "EVIDENCE_PERSIST_FAILED"
    assert (await _get(reg, task_id))["evidence_count"] == 0  # rolled back, no phantom row


@pytest.mark.asyncio
async def test_failed_or_missing_screenshot_creates_no_false_record(reg, page, shots_dir, monkeypatch):
    task_id = await _new_task(reg)

    async def no_shot(*a, **k):
        return None

    monkeypatch.setattr(controller, "screenshot", no_shot)
    failed = await _capture(reg, task_id)
    assert failed.error_category == "CAPTURE_FAILED"

    async def ghost_path(*a, **k):
        return str(shots_dir / "never_written.png")  # a path, but no file behind it

    monkeypatch.setattr(controller, "screenshot", ghost_path)
    ghost = await _capture(reg, task_id)
    assert ghost.error_category == "CAPTURE_FAILED"

    shots_dir.mkdir(parents=True, exist_ok=True)
    empty = shots_dir / "empty.png"
    empty.write_bytes(b"")

    async def empty_file(*a, **k):
        return str(empty)

    monkeypatch.setattr(controller, "screenshot", empty_file)
    zero = await _capture(reg, task_id)
    assert zero.error_category == "CAPTURE_FAILED"

    assert (await _get(reg, task_id))["evidence_count"] == 0


@pytest.mark.asyncio
async def test_overlong_path_is_refused_not_truncated(reg, page, shots_dir, monkeypatch):
    task_id = await _new_task(reg)
    real = shots_dir.parent / ("d" * 120) / ("e" * 120) / ("f" * 120) / ("g" * 120)
    real.mkdir(parents=True)
    long_file = real / "shot.png"
    long_file.write_bytes(PNG)
    assert len(str(long_file)) > 512

    async def long_path(*a, **k):
        return str(long_file)

    monkeypatch.setattr(controller, "screenshot", long_path)
    result = await _capture(reg, task_id)
    assert result.error_category == "EVIDENCE_PERSIST_FAILED"
    assert (await _get(reg, task_id))["evidence_count"] == 0


# ----------------------------------------------------------------------
# Descriptions
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_task_description_is_retrievable_after_creation(reg):
    text = "Open the project's claim page.\nDo NOT connect a wallet.\nKeep a screenshot as proof."
    task_id = await _new_task(reg, description=text)
    assert (await _get(reg, task_id))["description"] == text


@pytest.mark.asyncio
async def test_task_without_description_reports_empty_string(reg):
    result = await reg.invoke("create_agent_task", {"title": "No description"}, policy=POLICY)
    assert (await _get(reg, result.data["task_id"]))["description"] == ""


# ----------------------------------------------------------------------
# Context plumbing
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_task_context_is_scoped_to_the_call(reg, page, shots_dir):
    assert get_current_task_id() is None
    task_id = await _new_task(reg)
    await _capture(reg, task_id)
    assert get_current_task_id() is None  # reset after the call, so it cannot leak into the next one


@pytest.mark.asyncio
async def test_model_cannot_supply_a_task_id_parameter(reg, page, shots_dir):
    task_id = await _new_task(reg)
    result = await reg.invoke(
        "capture_evidence", {"session_key": "evtest", "task_id": task_id}, policy=POLICY
    )
    assert result.error_category == "INVALID_INPUT"  # schema unchanged; context is not model-controlled
    assert "task_id" not in CaptureEvidenceTool.input_schema["properties"]


# ----------------------------------------------------------------------
# Existing behaviour must not change
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_status_events_and_checkpoints_still_correct(reg, page, shots_dir):
    task_id = await _new_task(reg)
    async with session_scope() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        db.add(TaskCheckpoint(task_id=task.id, checkpoint_name="cp-1", state={"step": 1}, iteration=1))
    await _capture(reg, task_id, label="evt")

    report = await _get(reg, task_id)
    assert report["status"] == TaskStatus.READY.value
    assert report["checkpoints"] == 1
    types = [e["type"] for e in report["recent_events"]]
    assert "TASK_CREATED" in types and "STATUS_TRANSITION" in types
    assert "EVIDENCE_CAPTURED" in types  # the capture is auditable in the event log too
    assert report["title"] == "Evidence task"


@pytest.mark.asyncio
async def test_emergency_stop_still_blocks_state_changes_but_not_evidence_reads(reg, page, shots_dir):
    task_id = await _new_task(reg)
    emergency_stop.activate("test stop", "test")

    blocked = await reg.invoke("create_agent_task", {"title": "Created during stop"}, policy=POLICY)
    assert blocked.error_category == "EMERGENCY_STOP"

    captured = await _capture(reg, task_id, label="during-stop")  # read-class tool, unchanged
    assert captured.ok and captured.data["evidence_linked"] is True
    async with session_scope() as db:
        titles = (await db.execute(select(Task.title))).scalars().all()
    assert "Created during stop" not in titles


@pytest.mark.asyncio
async def test_existing_evidence_rows_are_preserved_and_listed(reg, page, shots_dir):
    """A row written by older code (free-text content, no JSON) must still be counted and shown."""
    task_id = await _new_task(reg)
    async with session_scope() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        db.add(
            TaskEvidence(
                task_id=task.id, evidence_type="TEXT", source="legacy", content="plain note", uri_reference=None
            )
        )
    await _capture(reg, task_id)

    report = await _get(reg, task_id)
    assert report["evidence_count"] == 2
    legacy = next(e for e in report["evidence"] if e["source"] == "legacy")
    assert legacy["label"] is None and legacy["sha256"] is None and legacy["uri"] is None
