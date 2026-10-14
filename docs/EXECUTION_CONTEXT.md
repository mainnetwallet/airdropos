# Persistent Execution Context — Task, Campaign, Global

**Status:** implemented in the backend (2026-10-14). Frontend rendering is partial — see
*Not yet done* below.

This is the reference for the three levels of context the agent reasons over, and for the
behaviours that depend on them.

---

## The problem this solves

Before this, everything the executor needed between steps lived in the LLM context window:
the goal, what had already been tried, what the last action actually revealed, which
requirements remained, why the next action was next. A restart, a replan, or a switch
between integrations lost all of it, and the only way to recover was to replay work.

Worse, `MemoryManager.recall()` had no scoping at all — every memory was global, so any
recall returned whatever had been written, from any task.

---

## The three levels

| Level | Where it lives | Who may read it |
|---|---|---|
| **Task Context** | `task_contexts`, `task_observations`, `task_requirements` — one set per task | that task only |
| **Campaign Context** | `airdrop_campaigns` + `memory_records` at `campaign` scope | tasks linked to that campaign |
| **Global Memory** | `memory_records` at `global` scope | any task |

They are a strict hierarchy, enforced in code rather than by convention:

* `MemoryManager.remember_scoped` **refuses** a task-scoped record with no task, a
  campaign-scoped record with no campaign, a record naming both, and a "global" record
  that carries an owner. The last is the important one: it is the exact shape of private
  data published as general knowledge.
* Those rules live in `remember`, not only in `remember_scoped`. `remember_scoped` is not
  the only door into the table, so it cannot be the only lock; and `extra=` is restricted
  to `{scope, task_id, campaign_id}` so it cannot be used to write `content` or `source`
  without screening.
* `recall_for_context` returns the union of what a task is entitled to, never anything
  else. The GLOBAL branch additionally requires **both** owner columns to be NULL, so a
  corrupt row written by any other path is not handed out on the strength of its label.
* `CampaignContextService.record_shared_fact` **raises** when asked to store
  account- or wallet-specific state. "Wallet 0xabc is eligible" describes one participant;
  writing it to shared campaign context is how one wallet's state would come to stand in
  for another's.

`TaskContextService` has no method that reads "the" context without a task. There is
deliberately no such call, because it is precisely the call that could return another
task's history.

---

## Task Context

### `task_contexts` — one row per task

The goal and the objective it was normalised to, success criteria, the outstanding
requirements (denormalised for a single-read answer, with `task_requirements` remaining
authoritative), unresolved questions, external resource references, the last observed
external state, the next-step rationale, human-intervention and authorization state,
error/retry summary, and a `revision` that increments on every write.

### `task_observations` — the action history

One row per meaningful action: the tool, a digest of its arguments, a `fingerprint` of
tool + canonical arguments, the outcome, the evidence kind and whether it was
authoritative, the **observed external state**, a bounded result digest, and the
integration reference.

The split between `observed_state` and `result_digest` is the point. "The join tool
returned 200" is a result; "the bot now says the channel join is required" is a state.
Recording only the former is how an attempt gets mistaken for a fact.

### `task_requirements` — what was discovered

Each requirement records where it came from, which observation satisfied it, and whether
it is campaign-scoped (safe to inform another task on the same campaign) or
account-specific (never).

### Outcomes are deliberately fine-grained

`attempted` · `executed` · `confirmed` · `unverified` · `failed` · `blocked` · `skipped` ·
`duplicate_suppressed`

Only `confirmed` **with authoritative evidence** counts as proven. An unknown outcome
string degrades to `unverified` — it can never be written as success by a future caller.

---

## What the executor does with it

In `AgentController`:

| Point | Behaviour |
|---|---|
| `_ensure_plan` | records the goal; marks a resumed run distinctly from a first run |
| before a step | persists *why this step is next*, read against the whole task |
| before invoking | suppresses a **non-idempotent** action already proven in this task |
| after invoking | writes the observation, extracts requirements, records references |
| before the next step | revises the plan if newly discovered requirements are unaddressed |
| on failure | records the error, category and recovery decision |
| on refusal | records the authorization decision and its reason |
| on human blocker | records what is being waited on, and clears it when resolved |
| on finalization | names the outstanding requirements in the outcome |

### Repeats

`confirmed_fingerprint()` reports an action only when it was *proven*. An attempt whose
outcome was never proven does not suppress anything — it is recorded, so the executor can
investigate rather than either skip it or blindly repeat it. When a repeat is suppressed
it is still written to the history as `duplicate_suppressed`.

### Discovery-driven replanning

After each action, requirements are read out of the tool result in two layers:

1. **Structured, deterministic.** `requirements`, `unmet_requirements`, `pending_tasks`,
   nested buttons. No model is consulted.
2. **Bounded prose extraction.** Real requirements arrive as sentences in a bot message.
   Those go to the single fixed LLM under a hard per-run budget
   (`MAX_PROSE_EXTRACTIONS_PER_RUN`, default 6). Any failure is non-fatal.

If the plan has no step addressing an outstanding requirement, the plan is revised under
`ReplanTrigger.DISCOVERY`, budgeted separately (`MAX_DISCOVERY_REPLANS_PER_RUN`, default 3)
so progress never consumes the failure-retry allowance.

### Prompt injection

* External text passes through `sanitize_external_text` before it is stored or sent.
* Content matching injection markers is **not** sent to the model at all, and never
  becomes a requirement. It is recorded as a question for the operator.
* A requirement extracted from prose is a requirement, not a permission: it can only
  cause a replan, and whatever that plan asks for still passes schema validation, the
  `AuthorizationPolicy` and the emergency stop.

### Secrets

Every write path is screened, not just the obvious ones. The earlier version screened
only `resource_refs` and the observation's top-level strings, which left three open
routes — each confirmed by a test that failed before the fix:

* `external_state` was assigned the **raw** payload a few lines after the screened copy
  had been written to the observation, so anything removed from one was present in full
  on the other.
* a secret nested inside a dict or list was never checked — the old code screened only
  `isinstance(value, str)` at the top level and JSON-encoded everything else, and a bot
  reply or page dump is *mostly* nested.
* requirement text, error strings, human-intervention reasons and authorization-refusal
  details were stored verbatim. All four are built from untrusted external content: a
  remote service quoting back what was submitted to it is the ordinary case.

Screening is now recursive and bounded (`MAX_SCREEN_DEPTH`, `MAX_SCREEN_ITEMS`), and the
*same* screened copy is written everywhere. A value matching a secret pattern becomes
`[REDACTED: matched a secret pattern]` rather than being dropped, so the record still
shows something was there.

`resource_refs` accepts an **allowlist** of reference keys only (`url`, `chat_id`,
`session_key`, `tx_hash`, …). A `session_cookie` has no key and is refused, so the
context cannot become a place credentials accumulate.

### Closing a requirement

A requirement is closed only when the confirmed observation actually targets it:

* every **identifier** it names — a `@handle`, an address, a URL, a domain, a quantity —
  must appear in what was observed and submitted, and
* enough of its ordinary words must overlap for it to be the same kind of thing.

The previous rule was "share any 4-character word", which closed *Join Telegram channel
@examplechannel* when the agent joined *@otherchannel* — `join`, `telegram` and `channel`
all matched. A false completion is worse than an unfinished one, so ambiguity resolves to
"not proven" and the requirement stays outstanding.

`unaddressed_requirements()` applies the same rule and deliberately errs the other way:
a requirement is reported as unaddressed unless the plan demonstrably names its target.
A needless replan costs one bounded LLM call; a missed one leaves real work with no step
aimed at it.

---

## Campaign Context

`CampaignContextService` assembles the campaign-wide facts and reports whether they are
still current:

* **Freshness** — `is_stale` is true when `last_checked_at` is older than 24h, or was
  never set. Reusing a stale fact is allowed; doing so *silently* is not — the snapshot
  says so.
* **Provenance** — only sources already judged `OFFICIAL` are listed as official. A
  community link is kept for provenance and explicitly labelled non-official.
* **Terminal campaigns** — a cancelled or expired campaign's requirements are reported as
  history, not as current work.
* **Wallet standing** — read back with an explicit `wallet_id`, and an unknown wallet has
  *no* standing rather than its neighbour's.

A second task on the same campaign gets its own Task Context and points at the same
campaign. It inherits none of the first task's history.

---

## Reading it

* `GET /api/v1/tasks/{id}` — a `context` block (goal, rationale, references, outstanding
  requirements, proven vs unproven actions, campaign facts, global fact count).
* `GET /api/v1/tasks/{id}/context` — the full view: requirements, the observation
  history, and the campaign snapshot including its staleness.
* Live event `agent.inbox` → `task.context_updated` fires when the context materially
  moves. It is a hint only: the state is committed first, so a dropped event costs a
  refresh and never state.
* The **Task Manager** detail panel renders an *Execution context* card: the goal, the
  reason the next action is next, the outstanding requirements, what has been proven, and
  what was attempted but not verified — as three separate lists. The card is hidden
  entirely when the task has no context yet. It never renders an unproven action as a
  success, and an empty "Outstanding requirements" list is labelled as not meaning the
  work is verified.

---

## Tests

* `backend/tests/test_context_secret_exposure.py` — the leak suite. Each test asserts the
  behaviour that should hold and **failed against the previous code**, so it is evidence
  rather than intent: raw `external_state`, nested values, requirement text, error
  strings, human-intervention reasons, and joining the wrong channel closing the
  requirement. Kept in the suite so the leak cannot return.
* `backend/tests/test_task_context_isolation.py` — isolation between unrelated tasks,
  concurrent tasks, cascade on delete, attempted ≠ confirmed, repeat recognition,
  requirement lifecycle, secret refusal, persistence across a fresh session, the resume
  marker, the memory hierarchy, and the three memory-scope bypass paths.
* `backend/tests/test_campaign_context.py` — shared campaign facts, no inherited history,
  account/wallet separation, staleness, official-vs-community provenance, terminal
  campaigns.
* `backend/tests/test_context_aware_execution.py` — deterministic vs prose extraction,
  budget enforcement, injection resistance, the real `AgentController` loop, the recovery
  vs discovery replan budgets, and the API endpoint including its authentication.
* `backend/tests/test_evidence_contract.py` — calls the **real** `_verify_step` rather than
  a replica. The replica is how the suite came to disagree with the controller.
* `backend/tests/test_migrations_sqlite.py` — the tables exist, match the ORM, and memory
  has its scope columns.

---

## Not yet done

* **No live refetch on the context event.** `task.context_updated` is published and the
  endpoint and the Task Manager panel exist, but the panel currently renders whatever the
  task status report carried when it loaded; it does not yet subscribe to the event and
  refetch. The backend state is always authoritative.
* **External-state reconciliation on resume.** A resumed run loads its context and does
  not replay confirmed actions, but it does not yet re-probe live external state to
  confirm those facts still hold. `observed_state` is the record to compare against when
  that is built.
* **Campaign linking is manual.** Nothing automatically attaches a task to a campaign
  during execution; `link_task` must be called with a verified campaign.
* **X and Discord.** Both remain `NOT_IMPLEMENTED` at the integration layer. The context
  layer is integration-agnostic and will carry their observations when they exist, but no
  claim is made about their behaviour today.