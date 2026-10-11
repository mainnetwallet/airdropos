# Task Lifecycle

## Task IDs

Format: `AIR-YYYY-NNNNNN` (e.g. `AIR-2026-000001`).

`id_generator.next_sequence()` allocates IDs under an asyncio lock and accepts an `is_taken`
callback so it skips collisions. A unique index on `tasks.task_id` is the final backstop.

## States

| State | Meaning |
|---|---|
| `CREATED` | Record exists, no work started. |
| `RESEARCHING` | Gathering information from external sources. |
| `PLANNING` | Decomposing into executable actions. |
| `READY` | Runnable; a worker may claim it. |
| `IN_PROGRESS` | A worker holds it and is executing. |
| `WAITING` | Parked on a time or resource condition. |
| `NEEDS_USER_ACTION` | Blocked on CAPTCHA, MFA, expiry, or credentials. |
| `RETRYING` | A recoverable failure occurred; retry is scheduled. |
| `PARTIALLY_COMPLETED` | Some actions succeeded; more remain. |
| `COMPLETED` | **All completion conditions verified against evidence.** |
| `FAILED` | Non-recoverable failure or retry budget exhausted. |
| `CANCELLED` | Terminal. Operator or agent cancelled. |
| `UNVERIFIED` | Work happened but could not be proven. |

## Transition Rules

The full table lives in `tasks/state_machine.py`. Key constraints:

- `CANCELLED` is terminal — nothing follows.
- `COMPLETED` can only be reached from `IN_PROGRESS`, `PARTIALLY_COMPLETED`, or `UNVERIFIED`.
- `COMPLETED` → `RETRYING` exists but means an *administrator* reopened it, not that the agent
  decided its own verification was wrong.

### The completion gate

```python
def assert_completion_verified(status, verified, evidence_count):
    if status is not TaskStatus.COMPLETED:
        return
    if not verified:
        raise InvalidTransition(COMPLETED, UNVERIFIED)
    if evidence_count == 0:
        raise InvalidTransition(COMPLETED, UNVERIFIED)
```

A task **cannot** reach `COMPLETED` without both a verified flag and at least one evidence
artifact. This is enforced in the backend, not requested of the LLM in a prompt.

## Execution

A worker claims a task with `TaskManager.claim_next_task()`:

1. Check the concurrency limit (`MAX_CONCURRENT_TASKS`).
2. Select the highest-priority runnable task whose `next_execution_at` has passed.
3. Lock with `FOR UPDATE SKIP LOCKED` so concurrent workers skip it.
4. Transition to `IN_PROGRESS` and record a `TASK_CLAIMED` event.

## Retry Behaviour

| Failure | Action |
|---|---|
| Retryable and under budget | `→ RETRYING`, retry count incremented, checkpoint saved |
| Non-retryable | `→ PARTIALLY_COMPLETED` with error details recorded |
| Retry budget exhausted | `→ FAILED` |
| Tool unavailable | Tool is **disabled** in the registry so work stops being dispatched to it |
| Not authorized | Recorded as a failure; the loop does not retry |

Permanent failures are never retried indefinitely.

## Blockers

`NEEDS_USER_ACTION` is used only when human intervention is genuinely unavoidable:

- CAPTCHA or anti-bot challenge
- MFA / 2FA prompt
- Session expiry requiring re-login
- Missing credentials
- Hardware-wallet interaction

When blocked, the agent: saves a checkpoint, records the reason, notifies the user, continues
unrelated tasks, and resumes when the condition is cleared. It never attempts to bypass these
controls.

## Status Reports

`TaskManager.get_status_report()` assembles the authoritative view from the database:

- Current status, progress, retry count
- Planned, pending, completed, and failed actions
- Checkpoint and evidence counts
- Recent executions with reasoning and durations
- Recent events with severity
- Associated transactions with confirmation counts

This is what `GET /tasks/{task_id}` returns and what the dashboard renders. Progress is never
inferred from conversation history.
