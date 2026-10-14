"""Agent Inbox user-action lifecycle.

Locks down three rules and the safety properties around them:

1. Deleting a Task deletes its Inbox conversations, messages and intervention requests.
2. Deleting an Inbox conversation removes ONLY that conversation (with its messages and
   requests). The Task is not deleted, cancelled or modified.
3. If a task that still exists needs a person again after its conversation was deleted, a NEW
   conversation (new id, no old messages) is opened. Repeated events for the same unresolved
   condition never create a second live conversation.

Authorization, the active-task restriction and the blockchain-audit protection must keep
working through all of it. External services are replaced with deterministic doubles.

Several tests deliberately build the tables WITHOUT foreign-key enforcement (the SQLite
default AirdropOS runs with), so a delete that relied on ON DELETE CASCADE would leave
orphans and fail here.
"""

from __future__ import annotations

import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import List

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.inbox.service import (
    CHANNEL_TASK_INBOX,
    ConversationService,
    InterventionService,
)
from backend.app.models import (
    AgentConversation,
    AuditLog,
    BlockchainTransaction,
    ConversationMessage,
    InterventionRequest,
    Task,
    Wallet,
)
from backend.app.security.auth import Scope
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

# Reuse the existing Inbox harness (in-memory database, authenticated operator sessions).
from backend.tests.test_agent_inbox import (  # noqa: F401  (fixtures are used by name)
    OPERATOR_A,
    OPERATOR_B,
    _client,
    _first_conversation,
    _make_task,
    _park,
    _session_for,
    _task_pk,
    factory,
    operator_a,
    operator_b,
)


# ======================================================================
# Helpers
# ======================================================================
async def _count(db, model, *where) -> int:
    stmt = select(func.count(model.id))
    for clause in where:
        stmt = stmt.where(clause)
    return int(await db.scalar(stmt) or 0)


async def _inbox_counts(factory, task_id: str) -> dict:
    async with factory() as db:
        pk = await _task_pk(db, task_id)
        return {
            "conversations": await _count(db, AgentConversation, AgentConversation.task_id == pk),
            "messages": await _count(db, ConversationMessage, ConversationMessage.task_id == pk),
            "requests": await _count(db, InterventionRequest, InterventionRequest.task_id == pk),
        }


async def _conversation_id(factory, task_id: str) -> str:
    async with factory() as db:
        return str((await _first_conversation(db, task_id)).id)


async def _set_status(factory, task_id: str, status: TaskStatus) -> None:
    async with factory() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        task.status = status.value
        await db.commit()


async def _task_snapshot(factory, task_id: str) -> dict:
    async with factory() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        assert task is not None, "the task must still exist"
        return {
            "id": str(task.id),
            "task_id": task.task_id,
            "title": task.title,
            "status": task.status,
            "progress": task.progress,
            "retry_count": task.retry_count,
            "owner_email": task.owner_email,
            "next_planned_action": task.next_planned_action,
            "updated_at": task.updated_at,
        }


async def _resolve_requests(factory, task_id: str) -> None:
    """Mark the task's open requests answered, as a successful reply would."""
    async with factory() as db:
        pk = await _task_pk(db, task_id)
        service = InterventionService(db)
        request = await service.active_for_task(pk)
        while request is not None:
            await service.mark_resolved(request, resolved_by=OPERATOR_A, outcome="Answered in test.")
            request = await service.active_for_task(pk)
        await db.commit()


async def _complete_task(factory, task_id: str) -> None:
    """Park, answer the request and finish the task: the 'completed user action' case."""
    await _resolve_requests(factory, task_id)
    await _set_status(factory, task_id, TaskStatus.COMPLETED)


async def _audit_actions(factory) -> List[AuditLog]:
    async with factory() as db:
        return list((await db.execute(select(AuditLog))).scalars())


async def _add_transaction(factory, task_id: str) -> None:
    async with factory() as db:
        pk = await _task_pk(db, task_id)
        wallet = Wallet(
            label="w",
            address="0x" + "1" * 40,
            chain_type="EVM",
            encrypted_secret="x",
            encryption_iv="x",
            encryption_tag="x",
            supported_networks=["ethereum"],
            is_active=True,
            is_locked=False,
        )
        db.add(wallet)
        await db.flush()
        db.add(
            BlockchainTransaction(
                task_id=pk,
                wallet_id=wallet.id,
                network="ethereum",
                to_address="0x" + "2" * 40,
                from_address=wallet.address,
                status="CONFIRMED",
            )
        )
        await db.commit()


# ======================================================================
# 1. Deleting a Task deletes its Inbox records
# ======================================================================
class TestTaskDeletionRemovesItsInbox:
    @pytest.mark.asyncio
    async def test_completed_task_deletion_removes_conversation_messages_and_requests(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _complete_task(factory, task_id)
        before = await _inbox_counts(factory, task_id)
        assert before["conversations"] == 1 and before["messages"] >= 1 and before["requests"] == 1

        async with _client(operator_a) as client:
            response = await client.delete(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        assert response.json() == {"task_id": task_id, "deleted": True}

        async with factory() as db:
            assert await TaskManager(db).get_by_task_id(task_id) is None
            assert await _count(db, AgentConversation) == 0
            assert await _count(db, ConversationMessage) == 0
            assert await _count(db, InterventionRequest) == 0

    @pytest.mark.asyncio
    async def test_a_pending_request_goes_with_its_task(self, factory, operator_a):
        task_id = await _make_task(factory)
        await _park(factory, task_id)  # leaves a PENDING request; NEEDS_USER_ACTION is deletable
        async with _client(operator_a) as client:
            assert (await client.delete(f"/api/v1/tasks/{task_id}")).status_code == 200
        async with factory() as db:
            assert await _count(db, InterventionRequest) == 0
            assert await _count(db, AgentConversation) == 0

    @pytest.mark.asyncio
    async def test_other_tasks_inbox_is_untouched(self, factory, operator_a):
        doomed = await _make_task(factory)
        kept = await _make_task(factory)
        await _park(factory, doomed)
        await _park(factory, kept)
        kept_before = await _inbox_counts(factory, kept)

        async with _client(operator_a) as client:
            assert (await client.delete(f"/api/v1/tasks/{doomed}")).status_code == 200

        assert await _inbox_counts(factory, kept) == kept_before
        async with factory() as db:
            assert await TaskManager(db).get_by_task_id(kept) is not None

    @pytest.mark.asyncio
    async def test_orphaned_messages_and_requests_of_the_task_are_removed_too(
        self, factory, operator_a
    ):
        """Rows whose conversation row is already gone still belong to the task."""
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            pk = await _task_pk(db, task_id)
            conversation = await _first_conversation(db, task_id)
            conversation_pk = conversation.id
            # Simulate a pre-fix orphan: the conversation row is gone, its children remain.
            from sqlalchemy import delete

            await db.execute(delete(AgentConversation).where(AgentConversation.id == conversation_pk))
            await db.commit()
            assert await _count(db, ConversationMessage, ConversationMessage.task_id == pk) >= 1

        async with _client(operator_a) as client:
            assert (await client.delete(f"/api/v1/tasks/{task_id}")).status_code == 200
        async with factory() as db:
            assert await _count(db, ConversationMessage) == 0
            assert await _count(db, InterventionRequest) == 0

    @pytest.mark.asyncio
    async def test_task_deletion_is_audited_with_the_inbox_removal_counts(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with _client(operator_a) as client:
            assert (await client.delete(f"/api/v1/tasks/{task_id}")).status_code == 200
        rows = [r for r in await _audit_actions(factory) if r.action == "TASK_DELETE"]
        assert len(rows) == 1
        assert rows[0].entity_id == task_id
        assert rows[0].actor == OPERATOR_A
        assert rows[0].before_state["inbox_removed"]["conversations"] == 1


class TestTaskDeletionRestrictionsAreIntact:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [TaskStatus.IN_PROGRESS, TaskStatus.RETRYING])
    async def test_an_active_task_cannot_be_deleted_and_keeps_its_inbox(
        self, factory, operator_a, status
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _set_status(factory, task_id, status)
        before = await _inbox_counts(factory, task_id)

        async with _client(operator_a) as client:
            response = await client.delete(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 409
        assert "Cancel it first" in response.json()["detail"]
        assert await _inbox_counts(factory, task_id) == before
        assert (await _task_snapshot(factory, task_id))["status"] == status.value

    @pytest.mark.asyncio
    async def test_a_task_with_blockchain_transactions_cannot_be_deleted(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _complete_task(factory, task_id)
        await _add_transaction(factory, task_id)
        before = await _inbox_counts(factory, task_id)

        async with _client(operator_a) as client:
            response = await client.delete(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 409
        assert "blockchain transaction" in response.json()["detail"]

        # The refusal happens before anything is removed: Inbox, task and the audit record.
        assert await _inbox_counts(factory, task_id) == before
        async with factory() as db:
            assert await TaskManager(db).get_by_task_id(task_id) is not None
            assert await _count(db, BlockchainTransaction) == 1

    @pytest.mark.asyncio
    async def test_task_deletion_still_requires_authentication_and_task_write(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        before = await _inbox_counts(factory, task_id)

        async with _client(None) as client:
            assert (await client.delete(f"/api/v1/tasks/{task_id}")).status_code == 401

        read_only = await _session_for("ro@test.local", [Scope.READ], factory)
        async with _client(read_only) as client:
            assert (await client.delete(f"/api/v1/tasks/{task_id}")).status_code == 403

        assert await _inbox_counts(factory, task_id) == before


# ======================================================================
# 2. Deleting an Inbox conversation leaves the Task alone
# ======================================================================
class TestInboxConversationDeletion:
    @pytest.mark.asyncio
    async def test_deleting_a_conversation_removes_only_that_conversation(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        other = await _make_task(factory)
        await _park(factory, task_id)
        await _park(factory, other)
        await _complete_task(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)
        other_before = await _inbox_counts(factory, other)

        async with _client(operator_a) as client:
            response = await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["deleted"] is True
            assert body["conversation_id"] == conversation_id
            assert body["task_id"] == task_id
            assert body["messages"] >= 1 and body["intervention_requests"] == 1

            assert (
                await client.get(f"/api/v1/inbox/conversations/{conversation_id}")
            ).status_code == 404

        assert await _inbox_counts(factory, task_id) == {
            "conversations": 0, "messages": 0, "requests": 0,
        }
        assert await _inbox_counts(factory, other) == other_before

    @pytest.mark.asyncio
    async def test_the_task_is_left_completely_untouched(self, factory, operator_a):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _complete_task(factory, task_id)
        before = await _task_snapshot(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)

        async with _client(operator_a) as client:
            assert (
                await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")
            ).status_code == 200

        after = await _task_snapshot(factory, task_id)
        assert after == before, "deleting a conversation must not modify the task in any way"
        assert after["status"] == TaskStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_the_task_can_still_be_found_and_listed_after_the_inbox_delete(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _complete_task(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)

        async with _client(operator_a) as client:
            await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")
            listing = await client.get("/api/v1/inbox/conversations")
            assert listing.json()["total"] == 0
            task = await client.get(f"/api/v1/tasks/{task_id}")
            assert task.status_code == 200

    @pytest.mark.asyncio
    async def test_deletion_is_audited_without_message_content(self, factory, operator_a):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _complete_task(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)

        async with _client(operator_a) as client:
            await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")

        rows = [r for r in await _audit_actions(factory) if r.action == "AGENT_INBOX_CONVERSATION_DELETE"]
        assert len(rows) == 1
        row = rows[0]
        assert row.actor == OPERATOR_A
        assert row.entity_id == conversation_id
        assert row.before_state == {"task_id": task_id}
        assert row.after_state["deleted"] is True
        assert "What happened" not in str(row.before_state) + str(row.after_state)

    @pytest.mark.asyncio
    async def test_deleting_twice_is_a_404_not_a_second_deletion(self, factory, operator_a):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _complete_task(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)
        async with _client(operator_a) as client:
            assert (await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")).status_code == 200
            assert (await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")).status_code == 404

    @pytest.mark.asyncio
    async def test_a_malformed_id_is_rejected(self, factory, operator_a):
        async with _client(operator_a) as client:
            assert (await client.delete("/api/v1/inbox/conversations/not-a-uuid")).status_code == 400


class TestInboxDeletionAuthorization:
    @pytest.mark.asyncio
    async def test_requires_authentication(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)
        before = await _inbox_counts(factory, task_id)

        async with _client(None) as client:
            assert (await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")).status_code == 401
        assert await _inbox_counts(factory, task_id) == before

    @pytest.mark.asyncio
    async def test_another_operator_cannot_delete_it(self, factory, operator_a, operator_b):
        task_id = await _make_task(factory, owner=OPERATOR_A)
        await _park(factory, task_id)
        await _complete_task(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)
        before = await _inbox_counts(factory, task_id)

        async with _client(operator_b) as client:
            response = await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")
            assert response.status_code == 404, "must be indistinguishable from 'not found'"

        assert await _inbox_counts(factory, task_id) == before
        assert not [r for r in await _audit_actions(factory) if r.action == "AGENT_INBOX_CONVERSATION_DELETE"]

    @pytest.mark.asyncio
    async def test_a_read_only_session_cannot_delete_it(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _complete_task(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)
        before = await _inbox_counts(factory, task_id)

        read_only = await _session_for("ro@test.local", [Scope.READ, Scope.WALLET_READ], factory)
        async with _client(read_only) as client:
            assert (await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")).status_code == 403
        assert await _inbox_counts(factory, task_id) == before


class TestOpenRequestsAreProtected:
    @pytest.mark.asyncio
    async def test_a_conversation_waiting_on_the_operator_cannot_be_deleted(
        self, factory, operator_a
    ):
        """Deleting it would park the task with no way to reply and resume it."""
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        conversation_id = await _conversation_id(factory, task_id)
        before = await _inbox_counts(factory, task_id)
        task_before = await _task_snapshot(factory, task_id)

        async with _client(operator_a) as client:
            response = await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")
        assert response.status_code == 409
        assert "still waiting on your input" in response.json()["detail"]
        assert await _inbox_counts(factory, task_id) == before
        assert await _task_snapshot(factory, task_id) == task_before

    @pytest.mark.asyncio
    async def test_a_stale_request_on_a_finished_task_does_not_block_deletion(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        await _set_status(factory, task_id, TaskStatus.CANCELLED)
        conversation_id = await _conversation_id(factory, task_id)

        async with _client(operator_a) as client:
            assert (await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")).status_code == 200
        assert (await _task_snapshot(factory, task_id))["status"] == TaskStatus.CANCELLED.value


# ======================================================================
# 3. A new user action opens a NEW conversation
# ======================================================================
class TestANewUserActionCreatesAFreshConversation:
    @pytest.mark.asyncio
    async def test_fresh_conversation_with_a_new_id_and_no_old_messages(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        old_id = await _conversation_id(factory, task_id)
        async with factory() as db:
            old_messages = {
                str(m.id)
                for m in await ConversationService(db).list_messages(uuid.UUID(old_id))
            }
        assert old_messages
        await _resolve_requests(factory, task_id)

        async with _client(operator_a) as client:
            assert (await client.delete(f"/api/v1/inbox/conversations/{old_id}")).status_code == 200

        # The task still exists and later needs a person again.
        await _set_status(factory, task_id, TaskStatus.IN_PROGRESS)
        result = await _park(
            factory, task_id, error_text="A captcha is blocking the sign-up page.",
            error_category=None, integration_ref=None,
        )
        assert result is not None and result["new_request"] is True

        new_id = await _conversation_id(factory, task_id)
        assert new_id != old_id, "the deleted conversation must never be reused"
        assert result["conversation_id"] == new_id

        async with factory() as db:
            new_messages = await ConversationService(db).list_messages(uuid.UUID(new_id))
            assert len(new_messages) == 1
            assert str(new_messages[0].id) not in old_messages
            assert new_messages[0].seq == 1, "the sequence restarts with the new conversation"
            assert "captcha" in new_messages[0].content.lower()
            assert await db.get(AgentConversation, uuid.UUID(old_id)) is None

        async with _client(operator_a) as client:
            assert (await client.get(f"/api/v1/inbox/conversations/{old_id}")).status_code == 404
            fresh = await client.get(f"/api/v1/inbox/conversations/{new_id}")
            assert fresh.status_code == 200
            body = fresh.json()
            assert body["message_count"] == 1
            assert body["requires_reply"] is True
            assert body["open_request"] is not None

    @pytest.mark.asyncio
    async def test_deleted_messages_never_reappear_anywhere(self, factory, operator_a):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            pk = await _task_pk(db, task_id)
            old_ids = {
                str(r)
                for r in (
                    await db.execute(
                        select(ConversationMessage.id).where(ConversationMessage.task_id == pk)
                    )
                ).scalars()
            }
            old_request_ids = {
                r.request_id
                for r in (
                    await db.execute(select(InterventionRequest).where(InterventionRequest.task_id == pk))
                ).scalars()
            }
        old_id = await _conversation_id(factory, task_id)
        await _resolve_requests(factory, task_id)
        async with _client(operator_a) as client:
            await client.delete(f"/api/v1/inbox/conversations/{old_id}")

        await _set_status(factory, task_id, TaskStatus.IN_PROGRESS)
        await _park(factory, task_id, error_text="Telegram session is unavailable for this task.")

        async with factory() as db:
            pk = await _task_pk(db, task_id)
            now_messages = {
                str(r)
                for r in (
                    await db.execute(
                        select(ConversationMessage.id).where(ConversationMessage.task_id == pk)
                    )
                ).scalars()
            }
            now_requests = {
                r.request_id
                for r in (
                    await db.execute(select(InterventionRequest).where(InterventionRequest.task_id == pk))
                ).scalars()
            }
        assert now_messages and not (now_messages & old_ids)
        assert now_requests and not (now_requests & old_request_ids)

        async with _client(operator_a) as client:
            listing = (await client.get("/api/v1/inbox/conversations")).json()
        assert listing["total"] == 1
        assert listing["conversations"][0]["conversation_id"] != old_id

    @pytest.mark.asyncio
    async def test_the_delete_and_recreate_cycle_can_repeat(self, factory, operator_a):
        task_id = await _make_task(factory)
        seen = set()
        for _ in range(3):
            await _set_status(factory, task_id, TaskStatus.IN_PROGRESS)
            await _park(factory, task_id)
            conversation_id = await _conversation_id(factory, task_id)
            assert conversation_id not in seen
            seen.add(conversation_id)
            await _resolve_requests(factory, task_id)
            async with _client(operator_a) as client:
                assert (
                    await client.delete(f"/api/v1/inbox/conversations/{conversation_id}")
                ).status_code == 200
        assert len(seen) == 3
        assert await _inbox_counts(factory, task_id) == {"conversations": 0, "messages": 0, "requests": 0}

    @pytest.mark.asyncio
    async def test_a_task_without_a_deletion_keeps_reusing_its_one_conversation(self, factory):
        """The lifecycle only changes after an explicit delete."""
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        first = await _conversation_id(factory, task_id)
        await _resolve_requests(factory, task_id)
        await _set_status(factory, task_id, TaskStatus.IN_PROGRESS)
        await _park(factory, task_id, error_text="A captcha is blocking the sign-up page.")
        assert await _conversation_id(factory, task_id) == first
        assert (await _inbox_counts(factory, task_id))["conversations"] == 1


class TestNoDuplicateActiveConversations:
    @pytest.mark.asyncio
    async def test_repeated_events_for_the_same_condition_create_one_conversation(self, factory):
        task_id = await _make_task(factory)
        for _ in range(4):
            await _park(factory, task_id)
        counts = await _inbox_counts(factory, task_id)
        assert counts["conversations"] == 1
        assert counts["requests"] == 1
        assert counts["messages"] == 1, "a worker retry must not repeat the agent's message"

    @pytest.mark.asyncio
    async def test_repeated_events_after_a_delete_create_exactly_one_new_conversation(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        old_id = await _conversation_id(factory, task_id)
        await _resolve_requests(factory, task_id)
        async with _client(operator_a) as client:
            await client.delete(f"/api/v1/inbox/conversations/{old_id}")

        await _set_status(factory, task_id, TaskStatus.IN_PROGRESS)
        for _ in range(3):
            await _park(factory, task_id)
        counts = await _inbox_counts(factory, task_id)
        assert counts == {"conversations": 1, "messages": 1, "requests": 1}

    @pytest.mark.asyncio
    async def test_the_database_refuses_a_second_live_conversation_for_one_task(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            pk = await _task_pk(db, task_id)
            db.add(
                AgentConversation(
                    channel=CHANNEL_TASK_INBOX,
                    channel_conversation_id=ConversationService.conversation_key(task_id),
                    task_id=pk,
                    owner_email=OPERATOR_A,
                )
            )
            with pytest.raises(IntegrityError):
                await db.flush()
            await db.rollback()

    @pytest.mark.asyncio
    async def test_the_key_is_free_again_once_the_conversation_is_deleted(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            service = ConversationService(db)
            conversation = await service.get_for_task(task.id, OPERATOR_A)
            old_pk = conversation.id
            await service.delete_conversation(conversation)
            fresh = await service.get_or_create_for_task(task, OPERATOR_A)
            await db.commit()
            assert fresh.id != old_pk
            assert fresh.channel_conversation_id == ConversationService.conversation_key(task_id)

    @pytest.mark.asyncio
    async def test_losing_a_creation_race_returns_the_winner_and_keeps_earlier_work(
        self, factory, monkeypatch
    ):
        """The race path must undo only its own insert, not the caller's transaction."""
        task_id = await _make_task(factory)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            winner = AgentConversation(
                channel=CHANNEL_TASK_INBOX,
                channel_conversation_id=ConversationService.conversation_key(task_id),
                task_id=task.id,
                owner_email=OPERATOR_A,
            )
            db.add(winner)
            await db.commit()
            winner_id = winner.id

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            task.owner_email = None  # as for a task created before ownership was tracked
            task.progress = 0.77  # earlier, still-uncommitted work in the same transaction
            await db.flush()

            service = ConversationService(db)
            real = service.get_for_task
            calls = {"n": 0}

            async def blind_first_time(task_pk, owner_email):
                calls["n"] += 1
                return None if calls["n"] == 1 else await real(task_pk, owner_email)

            monkeypatch.setattr(service, "get_for_task", blind_first_time)
            result = await service.get_or_create_for_task(task, OPERATOR_A)
            assert result.id == winner_id
            await db.commit()

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.progress == 0.77, "the savepoint must not roll back the caller's work"
            assert task.owner_email == OPERATOR_A
            assert (await _count(db, AgentConversation, AgentConversation.task_id == task.id)) == 1


class TestServiceLevelDeletion:
    @pytest.mark.asyncio
    async def test_delete_conversation_never_names_the_task(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        before = await _task_snapshot(factory, task_id)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            service = ConversationService(db)
            conversation = await service.get_for_task(task.id, OPERATOR_A)
            counts = await service.delete_conversation(conversation)
            await db.commit()
        assert counts["messages"] >= 1 and counts["intervention_requests"] == 1
        assert await _task_snapshot(factory, task_id) == before

    @pytest.mark.asyncio
    async def test_a_deleted_conversation_is_not_handed_back_by_the_session(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            service = ConversationService(db)
            conversation = await service.get_for_task(task.id, OPERATOR_A)
            pk = conversation.id
            await service.delete_conversation(conversation)
            assert await service.get_by_id(pk, OPERATOR_A) is None
            assert await service.get_for_task(task.id, OPERATOR_A) is None
            assert await InterventionService(db).active_for_task(task.id) is None


# ======================================================================
# 4. Migration: orphans from before this lifecycle existed are cleaned up
# ======================================================================
REPO = Path(__file__).resolve().parents[2]


class TestOrphanCleanupMigration:
    def _alembic(self, db_path: Path, *args: str) -> subprocess.CompletedProcess:
        from backend.tests.test_migrations_sqlite import _run_alembic

        return _run_alembic(db_path, *args)

    @staticmethod
    def _insert(con: sqlite3.Connection, table: str, **values) -> None:
        """Insert a row, filling every other NOT NULL column with a neutral default."""
        columns = {r[1]: r for r in con.execute(f"PRAGMA table_info({table})")}
        row = dict(values)
        for name, (_cid, _n, ctype, notnull, default, _pk) in columns.items():
            # Columns defaulting to now() cannot be left to SQLite, which has no such function.
            if name in row or not notnull:
                continue
            if default is not None and "now" not in str(default).lower():
                continue
            ctype = (ctype or "").upper()
            if "INT" in ctype or "FLOAT" in ctype or "BOOL" in ctype or "REAL" in ctype:
                row[name] = 0
            elif "DATE" in ctype or "TIME" in ctype:
                row[name] = "2026-01-01 00:00:00"
            elif "JSON" in ctype:
                row[name] = "{}"
            else:
                row[name] = "x"
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        con.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(row.values()))

    def test_orphans_are_removed_and_live_rows_survive(self, tmp_path):
        db_path = tmp_path / "inbox.db"
        assert self._alembic(db_path, "upgrade", "6_task_context").returncode == 0

        def uid() -> str:
            return uuid.uuid4().hex

        live_task, gone_task = uid(), uid()
        live_conv, orphan_conv, console_conv = uid(), uid(), uid()
        con = sqlite3.connect(db_path)
        try:
            self._insert(con, "tasks", id=live_task, task_id="AIR-LIVE", title="live", owner_email="o@x")
            # A live conversation with a message and a request.
            self._insert(con, "agent_conversations", id=live_conv, channel="TASK_INBOX",
                         channel_conversation_id="task:AIR-LIVE", task_id=live_task, owner_email="o@x")
            self._insert(con, "conversation_messages", id=uid(), conversation_id=live_conv,
                         task_id=live_task, role="assistant", content="keep me")
            self._insert(con, "intervention_requests", id=uid(), request_id="IR-LIVE", task_id=live_task,
                         conversation_id=live_conv, owner_email="o@x", reason_code="UNKNOWN",
                         problem_summary="live")
            # A conversation whose task was deleted without a cascade, with its children.
            self._insert(con, "agent_conversations", id=orphan_conv, channel="TASK_INBOX",
                         channel_conversation_id="task:AIR-GONE", task_id=gone_task, owner_email="o@x")
            self._insert(con, "conversation_messages", id=uid(), conversation_id=orphan_conv,
                         task_id=gone_task, role="assistant", content="orphan")
            self._insert(con, "intervention_requests", id=uid(), request_id="IR-ORPHAN", task_id=gone_task,
                         conversation_id=orphan_conv, owner_email="o@x", reason_code="UNKNOWN",
                         problem_summary="orphan")
            # Children whose own parents are gone.
            self._insert(con, "conversation_messages", id=uid(), conversation_id=uid(),
                         task_id=live_task, role="assistant", content="dangling message")
            self._insert(con, "intervention_requests", id=uid(), request_id="IR-DANGLING", task_id=gone_task,
                         conversation_id=live_conv, owner_email="o@x", reason_code="UNKNOWN",
                         problem_summary="dangling")
            # The general console conversation has no task and must never be touched.
            self._insert(con, "agent_conversations", id=console_conv, channel="DASHBOARD",
                         channel_conversation_id="console", task_id=None, owner_email=None)
            self._insert(con, "conversation_messages", id=uid(), conversation_id=console_conv,
                         task_id=None, role="user", content="console message")
            con.commit()
        finally:
            con.close()

        result = self._alembic(db_path, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        con = sqlite3.connect(db_path)
        try:
            def one(sql: str):
                return con.execute(sql).fetchall()

            assert {r[0] for r in one("SELECT channel_conversation_id FROM agent_conversations")} == {
                "task:AIR-LIVE", "console",
            }
            assert {r[0] for r in one("SELECT content FROM conversation_messages")} == {
                "keep me", "console message",
            }
            assert {r[0] for r in one("SELECT request_id FROM intervention_requests")} == {"IR-LIVE"}
            assert one("SELECT COUNT(*) FROM tasks")[0][0] == 1, "tasks are never touched"
        finally:
            con.close()

        # Re-running is a no-op and the downgrade is supported.
        assert self._alembic(db_path, "upgrade", "head").returncode == 0
        assert self._alembic(db_path, "downgrade", "6_task_context").returncode == 0
        assert self._alembic(db_path, "upgrade", "head").returncode == 0
