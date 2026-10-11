"""Future test suite: Task ID generation, state machine, and transitions.

NOT EXECUTED during the creation phase, per specification Part 37.
Created for use in a later authorized development phase.
"""

import pytest

from backend.app.tasks.id_generator import format_task_id, next_sequence, parse_task_id, validate_task_id
from backend.app.tasks.state_machine import (
    TaskStatus,
    assert_completion_verified,
    assert_transition,
    can_transition,
    is_claimable,
)


class TestTaskIdGeneration:
    def test_format_is_readable(self):
        assert format_task_id(1, 2026) == "AIR-2026-000001"
        assert format_task_id(999999, 2026) == "AIR-2026-999999"

    def test_parse_round_trips(self):
        task_id = format_task_id(4242, 2026)
        year, sequence = parse_task_id(task_id)
        assert (year, sequence) == (2026, 4242)

    @pytest.mark.parametrize("bad", ["", "AIR-2026", "XXX-2026-000001", "AIR-20xx-1", "AIR-2026-0"])
    def test_malformed_ids_rejected(self, bad):
        assert validate_task_id(bad) is False

    @pytest.mark.asyncio
    async def test_collision_skips_to_next_free_id(self):
        taken = {"AIR-2026-000001", "AIR-2026-000002"}
        sequence = await next_sequence(
            max_sequence=0,
            year=2026,
            is_taken=lambda candidate: candidate in taken,
        )
        assert sequence == 3


class TestStateMachine:
    def test_valid_forward_transition(self):
        assert can_transition(TaskStatus.READY, TaskStatus.IN_PROGRESS)
        assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED)

    def test_cancelled_is_terminal(self):
        assert not can_transition(TaskStatus.CANCELLED, TaskStatus.IN_PROGRESS)

    def test_invalid_transition_raises(self):
        with pytest.raises(Exception):
            assert_transition(TaskStatus.CREATED, TaskStatus.COMPLETED)

    def test_completed_requires_verified_evidence(self):
        # Claiming completion without evidence must fail.
        with pytest.raises(Exception):
            assert_completion_verified(TaskStatus.COMPLETED, verified=False, evidence_count=3)

        with pytest.raises(Exception):
            assert_completion_verified(TaskStatus.COMPLETED, verified=True, evidence_count=0)

        # Properly verified completion passes.
        assert_completion_verified(TaskStatus.COMPLETED, verified=True, evidence_count=1) is None

    def test_claimable_states(self):
        assert is_claimable(TaskStatus.READY)
        assert is_claimable(TaskStatus.RETRYING)
        assert not is_claimable(TaskStatus.NEEDS_USER_ACTION)
        assert not is_claimable(TaskStatus.COMPLETED)
