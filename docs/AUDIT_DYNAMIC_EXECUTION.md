# AirdropOS — Audit of Dynamic Execution, Limits and Completion Verification

Branch: `main` · Commit: `bb6ced763e64c53ed2b35cee8dd47eb77eb26a06` ("Live feed: poll slower while the WebSocket is connected")
Working tree at audit start: **clean** (`git status --porcelain` empty), no local unpushed commits.
No `AGENTS.md` / `CLAUDE.md` in the repository. No CI (`.github/` does not exist).

Everything below is taken from the actual source. File paths are relative to the repository root.

---

## 1. Actual execution paths

There are exactly **two** execution surfaces, and they do not overlap.

### 1.1 Operator chat (API process) — a conversation turn, not a task

```
POST /api/v1/agent/chat              backend/app/api/v1/endpoints/system.py:296
POST /api/v1/agent/chat/stream       backend/app/api/v1/endpoints/system.py:371
  -> ChatToolLoop.run                 backend/app/agent/chat_loop.py:191
       registry.catalog_for_llm      chat_loop.py:217
       llm.complete / stream_complete chat_loop.py:428 / 441 / 453
       registry.invoke                chat_loop.py:486 -> tools/registry.py:300
       create_task(spec)              chat_loop.py:361 -> system.py:517 _persist_chat_task
```

Chat runs tools **inline** in the request process. It never executes an `AgentController`
loop; it only *creates* a task (inserted directly as `READY`, `system.py:568-570`), which the
worker later claims.

### 1.2 Background task execution (worker process)

```
run.py:876  python -m backend.app.scheduler.worker
  scheduler/worker.py:649 main()
    recover_interrupted_tasks         worker.py:563
    asyncio.gather(task_execution_loop x MAX_CONCURRENT_TASKS)  worker.py:696-698
      TaskManager.claim_next_task     worker.py:415  -> tasks/manager.py:328
      AgentController.execute_task    worker.py:451  -> agent/controller.py:300
        _run_iteration                controller.py:880
          planner.evaluate_prerequisites      controller.py:940
          registry.invoke                    controller.py:1118
          _verify_step                       controller.py:1868
          _absorb_result                     controller.py:1242
      _settle_failed_execution       worker.py:328
```

`execute_task` has exactly **one** call site in the repository (`worker.py:451`). There is no
in-process resume path from the API; `resume_task` / `POST /tasks/{id}/retry` only move the
row to `READY`/`RETRYING` and let the worker pick it up.

### 1.3 Concurrency / duplicate-execution surfaces

* Claim is a compare-and-set `UPDATE ... WHERE status IN ('READY','RETRYING')`
  (`manager.py:363-379`) — the real atomicity mechanism on SQLite.
* `LeaseManager.acquire` (`tasks/concurrency.py:66`) additionally guards one live lease per
  task (`uq_execution_lease_task`).
* **Two gaps found:**
  * The lease `worker_id` is `f"controller-{id(self):x}"` (`controller.py:320`). CPython
    recycles `id()`, so two controllers in the same process can resolve to the *same* worker
    id and `concurrency.py:82` (`if live and existing.worker_id != worker_id`) then silently
    **takes over** a live lease instead of raising `LeaseConflict`.
  * `LeaseManager.renew` returns `False` on token mismatch but the return value is discarded
    at `controller.py:427`, so a run whose lease was taken over keeps executing.
* The API process and the worker both call `registry.invoke` for the same tool surface.
  There is **no** per-task mutual exclusion between the two processes; only the
  authorization / emergency-stop gating in `registry.invoke` (`registry.py:330-398`).

---

## 2. Every limit that can stop a task

| Limit | Value | Where | Source | Result when exhausted |
|---|---|---|---|---|
| `MAX_REPLANS_PER_TASK` | **2** | `controller.py:73`, read `controller.py:1647` | hard-coded module constant | **`TaskStatus.FAILED`**, terminal (`controller.py:1653-1657`) |
| `MAX_EXECUTION_LOOP_ITERATIONS` | 20 | `core/config.py:323`, read `controller.py:303` | env | `LoopOutcome.BUDGET_EXHAUSTED` then **`TaskStatus.FAILED`** (`controller.py:509-531`) |
| `MAX_DISCOVERY_REPLANS_PER_RUN` | 3 | `core/config.py:338`, read `controller.py:1359` | env | replan silently **skipped**; loop continues on the old plan |
| `MAX_TOOL_ROUNDS` | **6** | `chat_loop.py:46`, read `chat_loop.py:231` | hard-coded | chat turn ends (no task executed) |
| `MAX_TOOL_CALLS_PER_TURN` | **12** | `chat_loop.py:47`, read `chat_loop.py:164`, `308` | hard-coded | `truncated=True`, turn ends; also **silently truncates** the model's requested call list |
| `TURN_TIME_BUDGET_SECONDS` | 150.0 | `chat_loop.py:48`, read `:239`, `:315` | hard-coded | turn ends (checked only at loop boundaries, not around an in-flight call) |
| `MAX_TASK_RETRIES` / `task.max_retries` | 3 / 3 | `config.py:319`, `models/tasks.py:40` | env + column | step `failed` → replan → `FAILED` |
| `PlanStep.retry_limit` | 2 | `planning/planner.py:157` | model/decomposed | `decide_retry` returns False |
| `PlanConstraints.max_steps` | `None` | `planner.py:354` | — | **no plan-size ceiling** (deliberate, documented) |
| `MAX_CONCURRENT_TASKS` | 5 | `config.py:315`, read `manager.py:342`, `worker.py:696` | env | task stays `READY`/`RETRYING`, not claimed |
| `DEFAULT_LEASE_TTL_MINUTES` | 30 | `tasks/concurrency.py:30` | hard-coded | lease expires; `break_stale_leases` is **not scheduled anywhere** |
| `BaseTool.timeout_seconds` | 60 (10–300) | `tools/base.py:107` | per-tool | `ToolResult.failure(category="TIMEOUT", retryable=True)` |
| `BaseTool.max_retries` | 2 | `tools/base.py:108` | declared | **never read anywhere** |
| `LLM_MAX_RETRIES` | 1, **hard-clamped to ≤1** | `config.py:138`, `client.py:182` | env | `LLMError` re-raised |
| LLM client request timeout | `None` | `client.py:176-178` | hard-coded | a slow model is never cut off |
| `LLM_CONTEXT_*` | window 300000, trigger 230400, target 178560 | `token_budget.py` | env | `ContextCompactionError` → `LLMError(CONTEXT_TOO_LARGE, retryable=False)` |

### Termination paths and the status each leaves behind

| Path | Status |
|---|---|
| `execute_task` LLMError (`controller.py:373-382`) | status unchanged (`IN_PROGRESS`) → worker `_settle_failed_execution` → `RETRYING`/`FAILED` |
| `execute_task` PlanningError (`:383-391`) | same |
| `_replan` replan ceiling hit (`:1647-1662`) | **`FAILED`** |
| `_replan` PlanningError (`:1687-1700`) | **`FAILED`** |
| `_settle_budget_exhausted`, plan incomplete (`:509-531`) | **`FAILED`** with `error_details={"budget_exhausted": N}` |
| `_settle_budget_exhausted`, plan complete (`:491-507`) | `_finalize_plan` → `COMPLETED` or `FAILED` |
| `_finalize_plan` model says complete (`:1751-1767`) | **`COMPLETED`** |
| `_finalize_plan` fallback (`:1799-1816`) | **`COMPLETED`** |
| `_finalize_plan` otherwise (`:1770`, `:1848`) | **`FAILED`** |
| step prerequisite unmet (`:940-962`) | `WAITING` (terminal for this run) |
| no tool for a non-passive step (`:973-1008`) | `NEEDS_USER_ACTION` |
| tool `NEEDS_USER_ACTION` / `diagnosis.requires_human` (`:1525-1543`) | `NEEDS_USER_ACTION` |
| retryable failure in budget (`:1586-1607`) | `RETRYING` (+`retry_count`) |
| permanent failure (`:1609-1622`) | replan → ceiling → `FAILED` |
| emergency stop (`:310-315`, `:889-896`) | `NEEDS_USER_ACTION` |

---

## 3. Completion verification: the confirmed defects

`TaskStatus.COMPLETED` has exactly **two** producers, both in `_finalize_plan`.

### 3.1 `verified=True` set from the model's assertion alone — `controller.py:1739-1767`

```python
done_steps = max(
    1, sum(1 for s in plan.steps if s.tool and s.status in ("completed", "unverified"))
)
if complete:
    await self.tasks.transition(
        task, TaskStatus.COMPLETED,
        reason=f"Agent judged the objective complete: {why}"[:1000],
        verified=True, evidence_count=done_steps, progress=1.0, ...)
```

Three separate defects in five lines:

1. **`max(1, ...)` manufactures evidence.** The code comment says so explicitly:
   *"At least 1, because COMPLETED refuses a zero evidence count."*
2. **Steps explicitly classified `unverified` count as evidence.** This directly contradicts
   the fallback branch's own comment at `controller.py:1785-1787`:
   *"It must never be reported as COMPLETED."*
3. **No `_verify_step` call and no `authoritative` inspection on this path.** The only
   authority is `result["complete"] is True` from `llm.complete_json`.

`assert_completion_verified` (`state_machine.py:175-186`) does not catch any of this: it
accepts two caller-supplied scalars and performs **no database query** against `TaskEvidence`
or `TaskObservation`.

### 3.2 The judge is not shown the evidence facts it needs — `controller.py:1982-1987`

The payload passes `outcome`, `evidence_kind`, `observed_state`, `error` — but **not**
`evidence_authoritative`, which is a column on `TaskObservation` (`models/context.py:189`).
`ObservationRecord.proved_completion` (`context.py:580`, = `outcome == "confirmed" AND
evidence_authoritative`) exists and is used by `confirmed_fingerprint`, `_next_step_rationale`
and `_maybe_replan_for_discovery` — but is **never consulted** by `_finalize_plan` or
`_agent_completion_verdict`.

`observations(task, limit=60)` (`controller.py:1965`) truncates history to the most recent 60,
and `outstanding[:10]` (`controller.py:1988`) drops the rest.

### 3.3 `_verify_step` payload fallback is not gated on the declared method — `controller.py:1938-1944`

```python
if not evidence:
    ...
    if data.get("expected_text_seen") is True:
        return True, 1
```

This line is **not gated on `verification_method`**. Any tool that returns no `evidence` block
and sets `expected_text_seen is True` verifies a step — including one that declared
`chain_receipt`. It bypasses the exact check the surrounding comment says exists to prevent.

### 3.4 `GENERIC_EVIDENCE_METHODS` — `controller.py:79`

`frozenset({"", "tool_evidence", "evidence_present"})` means a plan step that declares *no*
verification method is satisfied by **any** authoritative evidence of **any** kind
(`controller.py:1889-1891`). Combined with 3.1, a step whose tool returned anything
authoritative can be counted toward `COMPLETED`.

### 3.5 The guard is caller-asserted everywhere

`grep evidence_count` across `backend/app` returns only `state_machine.py:185`,
`manager.py:128,139` and `controller.py:1757,1805`. `TaskEvidence.is_verified`
(`models/tasks.py:127`) is **never assigned `True`** anywhere in the backend.
`transition_if_current` (`manager.py:418-426`) writes a status through a guarded `UPDATE`
and by its own docstring *"does not run the COMPLETED verification guard"*.

---

## 4. Confirmed root causes of premature termination

**RC-1 — Recovery replan ceiling is terminal.**
`MAX_REPLANS_PER_TASK = 2` (`controller.py:73`) → `_replan` transitions the task to `FAILED`
with `error_details={"replan_limit": 2}` (`controller.py:1647-1662`). A task that needs three
genuine, justified recovery replans is abandoned at the third. This is the single most
aggressive terminator in the codebase.

**RC-2 — Execution-window exhaustion is a terminal failure.**
`_settle_budget_exhausted` (`controller.py:472-531`) sets `TaskStatus.FAILED` when the
20-iteration window ends with an incomplete plan. The docstring even argues the alternative
("A task is either done or failed; there is no in-between status"). The result is that a task
needing 25 iterations is failed at 20 and requires a human to press Retry. There is **no**
automatic continuation, and the worker has no job that re-queues a yielded task.

**RC-3 — Discovery replan budget is per-run and in memory.**
`self.discovery_replans = 0` (`controller.py:287`) is a plain instance attribute. A fresh
`AgentController` is constructed for every task execution (`worker.py:448-450`), so the
effective budget is **3 discovery replans per attempt**, reset on every retry, worker restart
and re-claim. Its sibling `_count_recovery_replans` (`controller.py:1425-1452`) reads the
`PlanRevision` table precisely because it must survive a restart; discovery does not.

**RC-4 — Chat turn bounds are hard-coded and unreachable from configuration.**
`MAX_TOOL_ROUNDS = 6` / `MAX_TOOL_CALLS_PER_TURN = 12` / `TURN_TIME_BUDGET_SECONDS = 150.0`
(`chat_loop.py:46-48`) are module constants. Neither call site
(`system.py:322`, `system.py:394`) passes an override, so no operator can change them.
`chat_loop.py:164` (`raw[:MAX_TOOL_CALLS_PER_TURN]`) silently discards tool calls the model
asked for without telling the model.

**RC-5 — `verified=True` is derivable from a model assertion and a manufactured count.**
Section 3.1 above.

**RC-6 — `_settle_budget_exhausted` can leave the outcome and the status disagreeing.**
`report.outcome` is `BUDGET_EXHAUSTED` (`"budget_exhausted"`), but the worker only calls
`_settle_failed_execution` when `report.outcome.value == "failed"` (`worker.py:466`). The
`FAILED` written at `controller.py:519` is therefore the only settlement, and it does not
consume a retry — so the task sits in `FAILED` until someone acts.

**RC-7 — No meaningful-progress guard, and no no-op-plan guard.**
Nothing compares a proposed replan against the previous one. `_maybe_replan_for_discovery`
returns `None` when the discovery budget is spent, so the task silently keeps executing a plan
that provably does not cover the outstanding requirements.

**RC-8 — Per-task LLM telemetry does not exist.**
`record_usage` (`token_budget.py:154-190`) writes to a process-global mutable dataclass
(`token_budget.py:151`) that is lost on restart and never attributed to a task. Its reader
`usage_snapshot()` (`token_budget.py:193`) is **dead code**. `stream_complete`
(`client.py:756-823`) never calls `record_usage` at all, and no `stream_options.include_usage`
is sent (`client.py:238-239`), so streamed operator traffic is invisible to accounting.
`completion_tokens` / `total_tokens` are never read anywhere in the backend.
`ConversationMessage.tokens_used` (`models/scheduler.py:250`) exists in the schema and is
**never written**. Missing provider usage is dropped by a bare `return` at
`token_budget.py:165` — indistinguishable from "not counted yet".

**RC-9 — Checkpoints persist far more than they resume from.**
`_checkpoint` (`controller.py:2094-2117`) writes `completed_action_ids`, `pending_actions`,
`plan_revision` and `plan_steps`. `_resume_iteration` (`controller.py:871-875`) reads **only**
`state["iteration"]`. `CheckpointManager.load_latest` (`checkpoints.py:41-47`) orders by
`(iteration DESC, created_at DESC)` with no validity predicate and no tiebreak on `id`.

**RC-10 — `TaskStatus.UNVERIFIED` and `PARTIALLY_COMPLETED` are unreachable.**
`grep` shows both names only in the transition table, the progress map, `InvalidTransition`
construction, the severity map and the inbox resume set. Neither is ever assigned to
`Task.status` anywhere in application code. `_finalize_plan` uses `FAILED` for every kind of
"not done", so a partially finished task and a permanently broken one are indistinguishable.

**RC-11 — Live resume bypasses the intervention lifecycle.**
`api/v1/endpoints/live.py:245-248` moves `NEEDS_USER_ACTION → READY` with a plain
`transition`, without `InterventionService.mark_resolved` and without clearing
`conversation.requires_reply`. The `InterventionRequest` stays `PENDING`, and a later Inbox
reply is then rejected by `_resumable` because `READY ∉ _RESUMABLE_FROM` (`replies.py:1013`).

---

## 5. Existing recovery mechanisms and their gaps

| Mechanism | Location | Gap |
|---|---|---|
| `FailureDiagnosisEngine` — 20 `ErrorCategory` values, `RETRYABLE`/`PERMANENT`/`REQUIRES_HUMAN` sets, ordered regex classification | `tasks/recovery.py:27-196` | Its `recovery_strategy` is **prose**, never executed. It contains no replanning. The controller's retry decision uses `planner.decide_retry`, not `diagnosis.retryable` (`controller.py:1570`). |
| `next_retry_at(backoff)` | `recovery.py:267` | Backoff `min(300, 2**attempt)` with `attempt = task.retry_count`; `controller.py:1587` then applies `or 5`, so the real minimum delay is 5 s, not the 30 s base in `worker.py:300`. |
| `Task.retry_count` / `max_retries` / `next_execution_at` | `models/tasks.py:39,40,62` | **Persisted.** The one counter that is safe across restarts. |
| Recovery replan count | `controller.py:1425-1452` (`SELECT count(PlanRevision) WHERE trigger NOT IN (DISCOVERY, INITIAL)`) | **Persisted**, but the threshold it gates is 2. |
| Discovery replan count | `controller.py:287` | **In-memory.** Resets every run. |
| `DiscoveryBudget.used` (prose extraction LLM calls) | `agent/discovery.py:155` | **In-memory**, per `AgentController`. `MAX_PROSE_EXTRACTIONS_PER_RUN` defaults to `None` (unbounded). |
| `ExecutionLease.last_iteration` | `models/planning.py:109` | Column **never written**; `controller.py:428` sets the attribute on the in-memory `LeaseInfo` dataclass instead. |
| `break_stale_leases` | `concurrency.py:148-253` | **Not scheduled anywhere** (`concurrency.py:158-161`); only startup recovery covers crashes. |
| Startup recovery | `worker.py:563-643` | Single-shot at boot. Parks on in-flight side effects, else `RETRYING`/`FAILED`. |
| Non-idempotent duplicate suppression | `controller.py:1063-1112` via `context.confirmed_fingerprint` | Sound, but requires an authoritative prior observation. |

---

## 6. Usage accounting and observability gaps

* **Per-task attribution: none.** Zero of the twelve production LLM call sites
  (`planner.py:654,675,1084`, `chat_loop.py:428,441,453`, `controller.py:2007`,
  `discovery.py:310`, `guidance.py:184`, `interpretation.py:406`, `engine.py:96`,
  `telegram_campaign_service.py:1391`) record per-task usage.
* **Streamed calls: not counted.** `stream_complete` never calls `record_usage`; no
  `stream_options: {"include_usage": true}` is ever sent.
* **Failed requests: not counted.** `record_usage` at `client.py:488` sits after `break`, so
  only the successful attempt is recorded.
* **Output tokens: never read.** Only `prompt_tokens` / `input_tokens`
  (`token_budget.py:163`). `completion_tokens` appears zero times in `backend/`.
* **Unknown vs zero: conflated.** `usage=raw.get("usage") or {}` (`client.py:533`) means an
  omitted block becomes `{}`, and `record_usage` returns silently (`token_budget.py:165`).
* **Served model:** `LLMResponse.model` keeps requested and served separate
  (`client.py:150-157`, `:531`) and `_record_served_model` (`client.py:538-569`) logs a
  mismatch — but both gates default to `False` (`config.py:243`, `:254`; `.env:73`), the
  observed set is process-local and in-memory (`client.py:186`), and nothing persists it per
  task. **`assert_model_locked` (`client.py:195`) exists and is never called.**
* **No tool-call, retry, replan, checkpoint or verification telemetry per task.** `TaskExecution`
  rows (`manager.py:227-239`) record one row per iteration but are not aggregated.

---

## 7. Test coverage gaps

641 tests collected at baseline (see §8). Relevant coverage:

* **Present:** `test_completion_verdict_and_logs.py` (4 tests — asserts the verdict is
  *echoed back*, not that evidence gates it), `test_cancel_stops_run.py` (3),
  `test_agent_inbox.py` checkpoint reuse (73 tests), `test_llm_retry_cap.py` (2).
* **Absent entirely:**
  * `backend/app/llm/compaction.py` (1348 lines) — zero tests import it.
  * `backend/app/llm/token_budget.py` — zero tests.
  * `backend/app/tasks/recovery.py` — zero tests.
  * `backend/app/tasks/concurrency.py` `LeaseManager` — only monkeypatched, never tested.
  * Any test of `MAX_REPLANS_PER_TASK`, `_count_recovery_replans`, `discovery_replans`.
  * Any test of `MAX_TOOL_ROUNDS`, `MAX_TOOL_CALLS_PER_TURN`, `TURN_TIME_BUDGET_SECONDS`.
  * Any test asserting that a run **continues** past an exhausted budget.
  * Any test asserting the controller **refuses** `COMPLETED` without evidence.
  * Any test of the served-model policy.
  * No `test_api_auth.py`, `test_wallets.py`, `test_chains.py`, `test_migrations.py`,
    `test_dynamic_tools.py` despite `docs/ARCHITECTURE.md:364-368` referencing them.
* **Known pre-existing failure:** `docs/ARCHITECTURE.md:374-378` records
  `test_inbox_llm_interpretation.py::TestClaimsAreVerifiedForReal::test_a_login_claim_is_checked_by_the_real_telegram_readiness_check`
  failing with `assert 'telegram_worker_session' == 'telegram_session_readiness'`.
* `test_context_secret_exposure.py:1-16` is a deliberately expected-to-fail proof of concept.

---

## 8. Baseline

Command (must be run from the repository root — the same command from `backend/` fails with
`ModuleNotFoundError: No module named 'backend'`, recorded at
`IMPLEMENTATION_CHECKLIST.md:511-513`):

```
.venv\Scripts\python.exe -m pytest backend/tests/ -q
```

Results are recorded in §"Test results" of the final report. Python 3.11.9, pytest 9.1.1,
pytest-asyncio 1.4.0, SQLAlchemy 2.1.4.

---

## 9. Implementation plan

### 9.1 Per-run window, task lifetime (`_settle_budget_exhausted`, `controller.py:472-531`)

* Exhausting `MAX_EXECUTION_LOOP_ITERATIONS` becomes a **yield**, not a failure: checkpoint,
  then transition `IN_PROGRESS → READY` with `next_execution_at` set a short configurable
  delay, so the existing `claim_next_task` re-claims it without consuming a retry.
  *Requires* adding `IN_PROGRESS → READY` to `VALID_TRANSITIONS` (`state_machine.py:83-95`).
* A genuinely stuck run is still stopped: an **unproductive-window** counter
  (`MAX_UNPRODUCTIVE_EXECUTION_WINDOWS`, default 3) counts consecutive windows that produced
  no verified step, no plan revision and no new evidence. Reaching it produces an accurate
  blocker, not a silent stall.
* `report.outcome` becomes `CONTINUED` so the worker does not treat a yield as a crash.

### 9.2 Recovery replans (`_replan`, `controller.py:1625-1726`)

* Replace the hard-coded `MAX_REPLANS_PER_TASK = 2` with `settings.MAX_RECOVERY_REPLANS_PER_TASK`
  (default 12), keeping the DB-derived count.
* Add a **plan-signature** guard: if the new plan's signature equals the previous revision's,
  that replan produced nothing new and does not consume budget. Two consecutive no-change
  replans stop the run with `reason="Replanning produced no new plan"` rather than spending
  calls forever.

### 9.3 Discovery replans (`_maybe_replan_for_discovery`, `controller.py:1345-1422`)

* Count from `PlanRevision` like `_count_recovery_replans` (persisted across restarts).
* `MAX_DISCOVERY_REPLANS_PER_TASK` (default 12, per *task*, not per run).
* Novelty gate: a replan that adds no step addressing the outstanding requirement is not
  charged and is not repeated.

### 9.4 Evidence-driven completion (`_finalize_plan`, `_agent_completion_verdict`, `_verify_step`)

* New `_completion_gate(task, plan)` computing a deterministic proof from **persisted
  observations**, not from step statuses: authoritative confirmations present; no `failed`
  / `blocked` steps; no `unverified` tool-bearing steps; no outstanding requirements.
* Path A requires model `complete=True` **and** the gate. Otherwise the work is preserved and
  the task goes to `UNVERIFIED` (or `PARTIALLY_COMPLETED`) with the exact missing items, or
  re-verifies via a replan when that is possible.
* `evidence_count` is the real count of `proved_completion` observations. `max(1, ...)` is
  deleted.
* The judge payload gains `evidence_authoritative` / `proved_completion` per observation,
  plus the failed/blocked step lists and the outstanding-requirement count.
* `_verify_step`'s `expected_text_seen` fallback is gated to text-observation methods only.

### 9.5 Enforcement in `TaskManager` / the state machine

* `assert_completion_verified` gains a `CompletionProof` argument and rejects a proof whose
  entries are not authoritative confirmations.
* `TaskManager.transition` recomputes the authoritative-confirmation count from
  `TaskObservation` and refuses `COMPLETED` when the persisted record does not support it —
  so the manager enforces the same invariant as the controller.

### 9.6 Telemetry

* `llm/token_budget.py` gains per-task accumulation keyed by the existing
  `tools.context` task-id contextvar, distinguishes `None` (unknown) from `0`, and
  `usage_snapshot()` finally gets a reader.
* `llm/client.py` records every attempt (including streamed and failed ones) with requested
  and served model, and sends `stream_options.include_usage`.
* New cumulative, restart-safe columns on `Task` (LLM requests, input/output/unknown tokens,
  served-model mismatch flag, tool calls, execution windows, recovery/discovery replans,
  unproductive windows, last blocker), written by the controller and exposed through the
  existing `get_status_report`.
* One additive Alembic migration for Postgres installs; SQLite picks the columns up through
  the existing `schema_sync` path.

### 9.7 Chat turn bounds

* `MAX_TOOL_ROUNDS`, `MAX_TOOL_CALLS_PER_TURN` and `TURN_TIME_BUDGET_SECONDS` become settings
  with larger, configurable defaults; the model's requested call list is no longer silently
  truncated without telling the model.

### 9.8 Frontend / API visibility

* Task detail query gains polling so `NEEDS_USER_ACTION`, blockers, recovery and final
  completion stay visible.
* Chat-created task chips link to the task and to the Inbox when waiting.
* The live resume path uses the guarded transition and resolves the intervention request.

---

## 10. Security controls that must not change

Single fixed model (`client.py:171`, `_build_payload:227`, no second model id anywhere in
`backend/app`); tool registry validation and registered schemas (`registry.py:344-362`);
authorization policy incl. network/wallet allowlists (`registry.py:78-152`);
autonomous policy; transaction-bound approvals (`transfer_tools.py:132-148`,
`_approval_fields:122-148`); wallet vault isolation (`wallet_tools.py:145-152`); emergency
stop at controller, registry and worker level; task leases; per-task context isolation
(`models/context.py:94-97`, `agent/context.py:790-796`); protected-file restrictions;
API authentication and audit logging. None of these are relaxed by this work.

---

## 11. What was implemented

### 11.1 Per-run window, task lifetime

`_settle_budget_exhausted` no longer decides the task's fate. It now:

1. Gives finalization its extra iteration first. A **terminal** finalization decides the
   outcome and returns.
2. A **non-terminal** finalization (the planner was asked for a new plan rather than
   ending the task) falls through to the yield instead of returning — a replan transitions
   nothing, so returning there would strand the task `IN_PROGRESS`, unclaimable and
   holding an execution slot for ever.
3. Checkpoints, counts the execution window, and if the task is still `IN_PROGRESS`,
   hands it back as `READY` with `next_execution_at` set to
   `EXECUTION_WINDOW_YIELD_DELAY_SECONDS`. No retry is consumed; the worker re-claims it
   and resumes from the latest checkpoint. The outcome is `LoopOutcome.CONTINUED`, which
   the worker treats as neither success nor failure.
4. The yield is a **compare-and-set** on the *committed* status
   (`transition_if_current(IN_PROGRESS → READY)`). The in-memory task can be stale, and a
   plain transition would write `READY` straight over a task the operator cancelled in the
   last few milliseconds of its final iteration — and `READY` is claimable, so it would
   then be executed.
5. A window that achieved **nothing** — no plan revision, no new authoritative
   confirmation, no step reaching `completed`, no change in outstanding requirements — is
   counted. `MAX_UNPRODUCTIVE_EXECUTION_WINDOWS` of those in a row stops the task with a
   precise blocker. Running iterations is explicitly *not* progress.

`IN_PROGRESS → READY` was added to `VALID_TRANSITIONS`. It is reachable only from
`IN_PROGRESS`, so it cannot resurrect a cancelled, completed or operator-parked task.

### 11.2 Recovery and discovery replanning

* `MAX_REPLANS_PER_TASK = 2` is no longer read. The bound is
  `settings.MAX_RECOVERY_REPLANS_PER_TASK` (12), still counted from the persisted
  `PlanRevision` history.
* `_plan_signature()` fingerprints what a plan says to *do* (index, kind, tool,
  normalised description; deliberately not step status). A replan that returns the same
  signature does not consume the recovery budget, and
  `MAX_REPEATED_NO_CHANGE_REPLANS` consecutive no-change replans stop the task with
  "replanning returned the same plan N time(s) in a row" — the precise diagnosis, and the
  thing that actually stops an unfixable failure from looping.
* Discovery replans are counted from `PlanRevision` by a new
  `_count_discovery_replans()` and bounded per *task* by
  `MAX_DISCOVERY_REPLANS_PER_TASK`, so a long task keeps learning across execution
  windows instead of getting three attempts per run.
* `ReplanTrigger.EVIDENCE_GAP` was added for "the plan ran to the end but the objective is
  not proven".

### 11.3 Evidence-driven completion

`_completion_proof()` computes what the system can actually prove, from the task's
**persisted observations and requirements** rather than from step statuses or anything the
model said. It produces a `CompletionProof`, which:

* `TaskStatus.COMPLETED` requires — the model saying `complete: true` **and**
  `proof.is_sufficient`.
* `assert_completion_verified(status, verified, evidence_count, proof)` rejects an
  insufficient proof outright.
* `TaskManager.transition` then **re-reads** `TaskObservation` rows for this task and
  refuses `COMPLETED` when no authoritative confirming observation and no linked
  `TaskEvidence` row exist. The caller's scalars are an assertion; the rows are the fact.

The old `done_steps = max(1, sum(... "completed" or "unverified"))` is gone. A step the
model ran but could not prove contributes nothing, exactly as the comment three lines below
it always claimed.

When the two disagree the evidence wins, and the model is told why through the judge
payload, which now carries `evidence_authoritative`, `proved_completion`, the failed /
blocked / **skipped** step lists and the deterministic gate summary.

`_verify_step`'s `expected_text_seen` fallback is now gated to
`TEXT_OBSERVATION_METHODS`: a tool claiming it saw text can satisfy "visible text", never
`chain_receipt`, a screenshot or a membership record.

Honest endings when the objective is not achieved:
* an unverified step, a blocked step or an outstanding requirement → **replan and continue**;
* the model says it is not done, or the only gap is a **policy-refused step** → `FAILED`,
  naming the refusal, because a permission is not something replanning can grant;
* no verdict and nothing recoverable → **`UNVERIFIED`**, which `TaskStatus` already had but
  which no code had ever assigned. Completed work is preserved and the task is resumable.

### 11.4 Retries and refusals

`_handle_tool_failure` is unchanged, but a permission already refused for this task is now
checked **before** the tool call (`context.refused_permission`, new). The check is scoped to
the execution window, so it cannot loop inside a window while an operator granting the
permission still takes effect on the next one.

### 11.5 Telemetry

New cumulative, restart-safe columns on `Task`: `execution_windows`,
`unproductive_windows`, `llm_requests`, `llm_input_tokens`, `llm_output_tokens`,
`llm_calls_without_usage`, `served_model_mismatches`, `last_served_model`, `tool_calls`,
`tool_failures`, `replans_total`, `recovery_replans_total`, `discovery_replans_total`,
`last_blocker`.

`backend/app/tasks/telemetry.py` accumulates per task using the existing
`tools.context` task-id contextvar, which the controller now publishes around a whole
execution window — so planner, replan, discovery, guidance and completion-verdict calls all
land on the right task. `llm/client.py` records **every** attempt including streamed and
failed ones, sends `stream_options.include_usage`, and keeps requested and served model
separate. `usage_snapshot()` is no longer the only reader: the totals are exposed through
`get_status_report()["telemetry"]`.

Unknown is never zero: a request with no usage block increments `llm_calls_without_usage`
and leaves the token columns `NULL`.

`last_blocker` is written through `_record_blocker()`, which applies the same `redact()` the
run log uses, because a blocker can quote a provider or tool error verbatim and is served by
the API.

### 11.6 Chat turns

`MAX_TOOL_ROUNDS`, `MAX_TOOL_CALLS_PER_TURN` and `TURN_TIME_BUDGET_SECONDS` are now
settings (`CHAT_*`) with larger, configurable defaults, and `ChatToolLoop` takes overrides.
Tool calls beyond the ceiling are reported **back to the model** by name instead of being
silently dropped, and the deferred list is per-loop-instance (it was module-global, which
would have leaked one operator's requested tools into another operator's prompt).

### 11.7 Other fixes found on the way

* `LeaseManager.acquire` compared a stored (naive, on SQLite) timestamp against an aware
  `now()`, raising `TypeError` instead of reporting the lease state.
* The live-resume endpoint used a plain `transition`, leaving the `InterventionRequest`
  `PENDING` so the Inbox kept asking for an answer it could no longer accept. It now uses
  the compare-and-set and resolves the request, attributed to the authenticated principal.
* The controller now checks the value returned by `LeaseManager.renew`; a lease taken over
  by another worker stops the run instead of being silently ignored.
* A `PlanningError`/`LLMError` before the first iteration is caught and reported with a
  precise blocker and a re-queued task, instead of escaping as a generic worker message.
* `get_status_report` surfaces the latest transition reason for `UNVERIFIED`,
  `PARTIALLY_COMPLETED`, `FAILED` and `WAITING`, not only for `NEEDS_USER_ACTION`.

---

## 12. Adversarial review of this change, and what it found

An independent adversarial pass over the finished diff found four real defects, all fixed
and all now covered by regression tests in `test_dynamic_execution.py`:

| Finding | Severity | Status |
|---|---|---|
| A step the policy **refused** (`skipped`) was invisible to the completion gate, so a plan whose value-moving step was denied could reach `COMPLETED` on another step's evidence | HIGH | `CompletionProof.skipped_steps` added; such a task now ends `FAILED` naming the refusal |
| A non-terminal `_finalize_plan` (an `EVIDENCE_GAP` replan) inside the budget-exhaustion branch left the task `IN_PROGRESS` — unclaimable, invisible, occupying an execution slot for ever | HIGH | The branch now falls through to the yield |
| The stall detector was unreachable: it treated "the iteration counter advanced" as progress, which is always true inside a window | MEDIUM | Progress is judged by plan revision, confirmations, completed steps and outstanding requirements |
| `IN_PROGRESS → READY` could overwrite a `CANCELLED` committed during the final iteration | MEDIUM | The yield is a compare-and-set on the committed status |
| `_deferred_tool_calls` was module-global and shared across concurrent chat turns | MEDIUM | Per-instance |
| `_flush_telemetry` peeked then reset non-atomically | LOW | `registry.take()` does both under the lock |
| `last_served_model` was unbounded provider text into a `String(128)` | LOW | Truncated at the boundary |
| `last_blocker` was persisted and served unredacted | LOW | Routed through `redact()` |
| Live resume auto-resolved an intervention with a fabricated outcome and a constant `resolved_by` | LOW | Factual outcome, attributed to the principal |

Controls confirmed unchanged: fixed-model integrity, registry validation, authorization
policy and allowlists, transaction-bound approvals, wallet vault isolation, emergency stop,
API auth and audit logging, per-task context isolation. Task leases were strengthened (the
renewal result is now checked).