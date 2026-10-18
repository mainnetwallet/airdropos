# Realtime architecture audit (REST, WebSocket, SSE, workers)

Reviewed commit: `981f5ce`. Scope actually covered is listed at the end; anything not listed was not audited.

## Findings

### F1 - Streamed turn creates a task after the client disconnected (CONFIRMED, Medium)
- **Where:** `backend/app/agent/chat_loop.py`, `ChatToolLoop.run` (final `create_task` step).
- **Scenario:** the browser leaves while the model is answering its last round. `should_stop` is only polled before a model call and before each tool call, so `stopped` stays False and the task is persisted. This contradicts the documented rule "a stopped turn creates no task" (`run` docstring, `agent_chat_stream` docstring).
- **Evidence:** `test_disconnect_during_final_model_round_creates_no_task` failed before the fix (task persisted), passes after.
- **Fix:** re-check `should_stop` immediately before persisting the task. Connected clients are unaffected (`test_connected_client_still_gets_its_task`).

### F2 - Relay cursor reset when nobody listens or the gateway is down (NOT REPRODUCED as a defect)
- **Where:** `backend/app/live/relay.py`, `relay_loop`.
- **Reading:** `live.event` is documented as ephemeral activity ("only while someone is listening"); a new viewer is deliberately not flooded. The gateway returns `epoch` and handles a changed epoch. Not changed.
- **Open check:** that the dashboard restores authoritative task state from REST after a reconnect was not verified.

### F3 - SSE client parser and server framing (NOT REPRODUCED)
- **Where:** `frontend/src/services/sse.ts`, `api.ts` `agentChatStream`, `system.py` `_sse`.
- **Reading:** CRLF is normalised on the accumulated buffer (chunk-split safe), multi-line `data:` joined, comments ignored, corrupt frames skipped, `done` result used as the final answer, missing `done` raises. Server frames always end in a blank line. No frontend test files exist, so this is source review only.

### F4 - Unbounded stream queue after disconnect (NOT REPRODUCED)
- **Where:** `system.py` `agent_chat_stream`. The queue only grows until the turn reaches its next safe boundary; the turn is bounded by `max_rounds`, tool-call and time budgets. Task references are held in `_STREAMED_TURNS` and discarded on completion.

### F5 - Global concurrency check is not atomic with the claim (NEEDS ENVIRONMENTAL TEST, Low)
- **Where:** `backend/app/tasks/manager.py`, `claim_next_task` (count of IN_PROGRESS, then claim).
- **Reading:** with several independent claimers on PostgreSQL (`SKIP LOCKED` hands them different rows) the limit could be exceeded by a short window. The supported topology is one worker process with `MAX_CONCURRENT_TASKS` slots (compose runs one worker), where it cannot exceed the limit. Needs PostgreSQL and two worker processes to test; no change made.

### F6 - Disconnect before the response body starts (NEEDS ENVIRONMENTAL TEST)
- **Where:** `system.py` `event_source`. `client_gone` is set in a `finally` inside the generator; if the connection drops before the first iteration the generator body never runs. Not reproduced; needs a real ASGI server test.

## Verified by tests (new, passing)
- Two concurrent claims of one READY task: exactly one wins.
- `MAX_CONCURRENT_TASKS=1`: a second claim returns nothing while one task is IN_PROGRESS.
- Existing lease tests (live lease not stealable, expired/released leases, token-guarded renew) already cover ownership and pass.

## Not audited / not run
REST endpoint-by-endpoint auth and validation, rate limiting, DB rollback behaviour, retry/backoff and recovery paths (`worker.py`, `recovery.py`), migrations, Docker startup, WebSocket reconnect/duplicate-relay behaviour across multiple API processes, PostgreSQL-only tests (16 skipped).

---

# Part 2 - Retry, backoff and crash recovery

Base: `99b7191`. Read: `worker.py` (`task_execution_loop`, `_settle_failed_execution`, `recover_interrupted_tasks`, `sweep_orphaned_tasks`, `main`), `tasks/manager.py` (`claim_next_task`, `transition`), `tasks/concurrency.py` (lease acquire/renew/release), `agent/controller.py` (`execute_task`, retry scheduling sites), `docker-compose.yml`.

### F7 - Model outage during planning is retried with no backoff (CONFIRMED, Medium)
- **Where:** `agent/controller.py`, `execute_task`, the `except (PlanningError, LLMError)` handler.
- **Scenario:** the provider is unreachable while planning. The task goes to RETRYING and `retry_count` is incremented, but `next_execution_at` is never set, so an idle worker claims it again at its next poll. A short outage uses up `max_retries` within seconds and the task is marked FAILED. Every other retry path (`_settle_failed_execution`, step-level recovery) sets a schedule.
- **Evidence:** `test_planning_outage_is_retried_with_backoff_not_immediately` failed before the fix (`next_execution_at` was None), passes after.
- **Fix:** the same exponential backoff (30 s base, 600 s cap) is applied. The function moved to `tasks/recovery.py` and is re-exported from `worker.py` under its old names, so existing imports still work. No new limits were added.

### Verified, no defect found (tests pass)
- Backoff is exponential and capped, including negative and very large counts (`test_backoff_is_exponential_and_capped`).
- A failed attempt becomes RETRYING with a persisted `next_execution_at`, counted once; a repeat settle does not count again.
- Exhausted budget ends FAILED. A RETRYING task is not claimable before `next_execution_at` and is claimable after it (SQLite).
- Crash recovery (already covered by existing tests, which pass): expired lease re-queued, live lease left alone, concurrent recovery counts one retry, side-effecting or completed transaction steps parked as NEEDS_USER_ACTION and never replayed, claim released on failure or cancellation.

### Risks not confirmed / not verifiable here
- **Permanent provider errors are retried.** The planning handler ignores `LLMError.retryable`, so a non-retryable error (for example a bad key) still uses the retry budget before FAILED. Bounded, not infinite; changing it alters behaviour, so no change made. Needs a product decision.
- **Recovery re-queues without backoff** (`recover_interrupted_tasks`). Intentional reading: the worker died, the task did not fail; bounded by `max_retries`. Not changed.
- **Cross-process concurrency limit and PostgreSQL `SKIP LOCKED` behaviour** (see F5): not tested. No PostgreSQL or Docker is available in this environment.
- **Graceful shutdown:** loops only check the shutdown flag between tasks, so a running task is not interrupted by SIGTERM; compose sets no `stop_grace_period`, so a long task is killed after the Docker default and is then handled by lease expiry and recovery. Not exercised at runtime.
- Task deadlines are used for claim ordering only; no deadline enforcement exists and none was added.
- **Correction to Part 1:** the 16 skipped tests are Playwright/Chromium tests that cannot launch here, not PostgreSQL tests.
