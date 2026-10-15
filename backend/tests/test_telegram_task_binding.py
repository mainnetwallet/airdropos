"""A task is bound to one Telegram account: locked while unfinished, closed when it ends,
and several tasks can run at once on the same or on different accounts."""

from __future__ import annotations

import pytest

import backend.app.database.session as session_module
from backend.app.integrations import telegram_accounts, telegram_leases
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus
from backend.app.tools import telegram_user_tools as tools


@pytest.fixture()
def two_accounts(monkeypatch, tmp_path):
    """Two registered accounts, 'main' (default) and 'farm-2', with session files present."""
    monkeypatch.setattr(telegram_accounts.settings, "TELEGRAM_SESSION_PATH", str(tmp_path / "tg.session"))
    for name in ("main", "farm-2"):
        (tmp_path / "accounts").mkdir(exist_ok=True)
        (tmp_path / "accounts" / f"{name}.session").write_bytes(b"s")
    me = type("Me", (), {"id": 1, "first_name": "A", "last_name": None, "username": "a"})()
    telegram_accounts.add_account("main", me, str(tmp_path / "accounts" / "main"), make_default=True)
    telegram_accounts.add_account("farm-2", me, str(tmp_path / "accounts" / "farm-2"))
    tools._extra_sessions.clear()
    yield
    tools._extra_sessions.clear()


async def _task(db, account=None, status=TaskStatus.READY):
    manager = TaskManager(db)
    task = await manager.create_task("t", "d", telegram_account=account)
    await manager.transition(task, status, reason="test")
    await db.commit()
    return task


async def test_unknown_account_is_rejected_not_defaulted(two_accounts):
    with pytest.raises(ValueError):
        telegram_leases.normalize_label("nope")
    assert telegram_leases.normalize_label("FARM-2") == "farm-2"  # registered spelling
    assert telegram_leases.normalize_label("") is None


async def test_unfinished_task_locks_its_account_and_finishing_unlocks_it(two_accounts):
    async with session_module.AsyncSessionLocal() as db:
        task = await _task(db, "farm-2")
        with pytest.raises(telegram_leases.AccountInUse) as busy:
            await telegram_leases.assert_free(db, "farm-2")
        assert task.task_id in busy.value.task_ids
        await telegram_leases.assert_free(db, "main")  # other account is untouched

        assert (await telegram_leases.labels_in_use(db)) == {"farm-2": [task.task_id]}

        task.status = "COMPLETED"
        await db.commit()
        await telegram_leases.assert_free(db, "farm-2")


async def test_many_tasks_can_share_an_account_and_it_frees_only_when_all_end(two_accounts):
    async with session_module.AsyncSessionLocal() as db:
        a = await _task(db, "main")
        b = await _task(db, "main")
        a.status = "COMPLETED"
        await db.commit()
        with pytest.raises(telegram_leases.AccountInUse) as busy:
            await telegram_leases.assert_free(db, "main")
        assert busy.value.task_ids == [b.task_id]
        # the running task itself may still act on its own account
        await telegram_leases.assert_free(db, "main", ignore_task=b.task_id)


async def test_task_bound_account_wins_over_what_the_model_asks_for(two_accounts):
    telegram_leases.begin_run("AIR-2026-10-16-000001", "farm-2")
    try:
        from backend.app.tools.context import task_context

        with task_context("AIR-2026-10-16-000001"):
            assert tools.session_for(None).label == "farm-2"
            assert tools.session_for("main").label == "farm-2"  # cannot cross over
        assert tools.session_for(None).label == "main"  # outside the task: the default
    finally:
        telegram_leases.end_run("AIR-2026-10-16-000001")


async def test_first_use_binds_an_unbound_task(two_accounts):
    async with session_module.AsyncSessionLocal() as db:
        task = await _task(db, None)
        tid = task.task_id
    telegram_leases.begin_run(tid, None)
    try:
        from backend.app.tools.context import task_context

        with task_context(tid):
            sess = tools.session_for(None)
            await sess._bind_current_task()
        assert sess.bound_by_task and telegram_leases.bound_label(tid) == "main"
        async with session_module.AsyncSessionLocal() as db:
            from backend.app.models import Task
            from sqlalchemy import select

            row = await db.scalar(select(Task).where(Task.task_id == tid))
            assert row.telegram_account == "main"
    finally:
        telegram_leases.end_run(tid)


async def test_idle_sweep_closes_only_accounts_nobody_needs(two_accounts, monkeypatch):
    from backend.app.scheduler import worker

    monkeypatch.setattr(worker, "AsyncSessionLocal", session_module.AsyncSessionLocal)

    class FakeClient:
        def __init__(self):
            self.connected = True

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.connected = False

    busy, idle = tools.session_for("farm-2"), tools.session_for("main")
    for s in (busy, idle):
        s._client, s.bound_by_task = FakeClient(), True

    async with session_module.AsyncSessionLocal() as db:
        await _task(db, "farm-2")  # still unfinished: keeps farm-2 open

    closed = await worker.release_idle_telegram_sessions(force=True)
    assert closed == 1
    assert idle._client.connected is False and busy._client.connected is True
