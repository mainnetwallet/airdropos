# AirdropOS Architecture Overview

## Overview

AirdropOS is an autonomous AI agent platform designed to research, execute, and monitor crypto airdrop campaigns. Built according to Specification Parts 1–42, the system emphasizes **strict security constraints**, **authorization enforcement**, and **verifiable operation**.

All critical operations are gated behind a deterministic authorization policy, with **no real blockchain transactions** unless explicitly enabled by the platform operator. The architecture is modular, with each subsystem implementing its own safety guards and verification mechanisms.

## Key Architectural Principles

### 1. Single LLM Policy
- Exactly one provider endpoint/API key/model.
- No routing, no fallback model.
- Enforced structurally in `FixedLLMClient.assert_model_locked()`.

### 1a. Input Context Budget and Compaction
- The input context budget (`LLM_CONTEXT_*`) is an **input** concern only; it never sets
  an output limit. `LLM_MAX_TOKENS` stays unset by default, so `max_tokens` is omitted
  from every request.
- A request that fits the budget is sent **byte-for-byte as built**. Nothing is rewritten.
- A request that cannot fit is **compacted by the same fixed model**: a bounded,
  multi-round workflow in `app/llm/compaction.py` asks the configured `LLM_MODEL_ID` for
  a strict-JSON, section-labelled summary of the reduceable content, re-estimates the
  request after every round, and runs another round only while it is still making
  progress. There is no second model, router or fallback.
- System messages and the opening of the active objective are **never reduced**. Every
  trust/security label present in the reduced content is re-emitted deterministically in
  a preserved-labels ledger, so a summary can never launder untrusted content into an
  instruction. Secrets are redacted with the platform's existing patterns before any
  text reaches the compaction prompt.
- Compaction terminates explicitly: the request fits, the configured maximum number of
  rounds is reached, the summary is unusable, or a round stopped making progress. If it
  cannot be made safe, the request fails with a non-retryable `CONTEXT_TOO_LARGE` error
  instead of being submitted oversized or quietly trimmed.
- The compaction model call is a dedicated internal path
  (`FixedLLMClient._compaction_summarize`) that skips the budget preflight, so compaction
  can never re-enter itself.
- Chat history has **no application-level limit**. There is no turn count, no character cap
  and no replacement bound of either kind: an oversized history reaches the context
  pipeline above, which can compact it, rather than being thinned at the API boundary
  before anything could see it. A long message can no longer be refused with a 422
  either. Ordering, roles and the tool-call/tool-result relationships are preserved.

### 2. Deny-by-Default Authorization
- **Scopes**: `read`, `tool_read`, `wallet_read`, `wallet_write`, `system_admin`, `emergency_stop`, etc.
- Every REST endpoint requires `get_principal`; WebSocket sessions resolve from a live, unexpired `UserSession`.
- Authorization policy is a static `AuthorizationPolicy` object populated once in `main.py`.

### 3. Observe‑Plan‑Act‑Verify Execution Loop
- **Observe**: Gather tasks, context, constraints.
- **Plan**: Single plan generation; LLM cannot re‑plan.
- **Act**: Sequential execution; verification required before proceeding to next step.
- **Verify**: `assert_completion_verified()` requires authoritative evidence; `UNVERIFIED` actions are recorded, not assumed successful.

### 4. Read‑Only Tool Exposure
- Tools are **read‑only** or **value‑moving** with explicit operator permissions.
- Tool registry validates `required_permissions` against the policy.
- No unrestricted shell, SQL, filesystem, or raw network access.

### 5. Security‑First Subsystem Design
Each subsystem implements its own **deterministic guards**:

#### Wallet Management (`app/wallets/`)
- AES‑256‑GCM vault; private keys never exposed.
- `wallet_read` for inspection, `wallet_write` for mutations (import, watch, lock, deactivate, delete).
- Secrets screened; `can_sign` flag based on key material presence and lock state.

#### Blockchain & Transactions (`app/transactions/`, `app/blockchain/`, `app/wallets/vault.py`)
- **TransactionEngine** validates intents, estimates gas, enforces spend limits (per‑transaction and daily), checks destination denylist, guards with emergency stop.
- **NonceManager** per (network, address) to prevent conflicts.
- **EVM Chain Adapter** (`app/blockchain/chains.py`) centralizes supported network list; adapter is structurally read‑only (no signing).

#### Agent Execution (`app/agent/`, `app/main.py`)
- Single LLM, no routing, no fallback.
- `SYSTEM_PRINCIPAL` for scheduler‑initiated work; operator principal for HTTP APIs.

#### Memory & Knowledge (`app/memory/`, `app/knowledge/`)
- **MemoryManager** stores verified facts; secret screening, TTL revalidation, consolidation.
- **KnowledgeGraphService** typed, weighted, directed edges; bounded traversal; explicit entity deletion pruning.

#### Diagnostics & Monitoring (`app/diagnostics/`, `app/scheduler/`, `app/tools/registry.py`)
- **Watchdog** real‑time probes (database, RPC, LLM, browser, emergency stop, tools); persisted to `IntegrationHealth`.
- **Scheduler** workers (`handle_airdrop_discovery`, `handle_campaign_monitoring`, etc.) each bound to a distinct handler.
- **ToolRegistry** enforces policy; `registry.policy` is a declared attribute (was previously a dynamic assignment that left tool calls denied).

### 6. State‑Machine Operations
- **TaskManager**, **ClaimLifecycle**, **GoalManager** enforce all state transitions.
- **InvalidTransition** exceptions raised on illegal moves; `assert_completion_verified()` before marking `COMPLETED`.

## Security Controls

### Authentication & Authorization
- **Bearer token** for REST **and** for the WebSocket, sent as either an
  `Authorization: Bearer` header or the `Sec-WebSocket-Protocol: bearer, <token>`
  sub-protocol. A **query parameter is rejected on purpose**: a token in the URL
  lands in access logs, proxy logs, browser history and `Referer` headers.
- `UserSession` records: token hash, user reference, scopes, expiration.
- **Rate limiting** per‑IP, per‑bucket (`read`, `write`, `chat`, `auth`, `emergency`).
- **Audit logging** (`write_audit`) for security‑relevant actions; secrets redacted via `_redact()`.

### Emergency Stop
- **Global interlock**; if set, all signing/submission is rejected with detailed error.
- Guarded by `emergency_stop.can_submit_transactions()` and `emergency_stop.can_sign()`.
- Operator cannot enable signing without explicit configuration.

### Secret Management
- **AES‑256‑GCM** vault; master key in settings (`WALLET_VAULT_MASTER_KEY`).
- **Ephemeral key scope** (`_with_private_key` decorated with `@contextmanager`).
- **Secret screening** (`MemoryManager.assert_no_secrets`) blocks private keys, mnemonics, API keys, etc.
- **Audit redaction** (`_REDACT_KEYS`) removes sensitive fields.

### Verification & Evidence
- **Authoritative evidence** required for `COMPLETED` status.
- **Verification** calls `assert_completion_verified()`; failures roll back.
- **Irreversible actions** (e.g., wallet delete) are logged as irreversible; `UNVERIFIED` is never assumed successful.

## Technology Stack

| Layer | Technology |
|-------|------------|
| **Runtime** | Python 3.10+, FastAPI, Starlette |
| **Database** | PostgreSQL (SQLAlchemy 2.0 async); in‑memory SQLite for tests |
| **Security** | PBKDF2‑HMAC‑SHA256 password hashing (600k rounds); OAuth2‑style scopes |
| **CLI** | `pytest` for unit/integration; `npm run lint` and `npm run build` for frontend |
| **Containerization** | `docker-compose.yml`, multi‑stage `Dockerfile` |
| **Tools** | Playwright (browser automation), eth‑account (signing), httpx (RPC), alembic (migrations) |

## Subsystem Interfaces

### REST API (`app/api/v1/endpoints/`)
All endpoints require `get_principal`; response bodies **never contain secrets**.

| Resource | GET | POST | PATCH | DELETE |
|----------|-----|------|-------|--------|
| `/api/v1/wallets` | `wallet_read` | – | – | – |
| `/api/v1/wallets/import` | – | `wallet_write` | – | – |
| `/api/v1/wallets/watch` | – | `wallet_write` | – | – |
| `/api/v1/wallets/{id}` | `wallet_read` | – | `wallet_write` | `wallet_write` |
| `/api/v1/wallets/{id}/lock` | – | `wallet_write` | – | – |
| `/api/v1/wallets/{id}/deactivate` | – | `wallet_write` | – | – |
| `/api/v1/tasks` | `task_read` | `task_write` | – | – |
| `/api/v1/agent/chat` | – | `agent_chat` | – | – |
| `/api/v1/emergency-stop` | – | `emergency` | – | – |

### WebSocket (`app/api/v1/endpoints/ws.py`)
- Single endpoint `/api/v1/ws`. The token is sent as `Sec-WebSocket-Protocol: bearer, <token>` (or an `Authorization` header), never in the URL.
- Session resolved by the same `resolve_session` as REST; requires the `read` scope and is revalidated while connected (logout/expiry closes the stream).
- Emits `connected` event; responds to `ping` with `pong`.

### Telegram Tool Surface (`app/tools/telegram_*`, `app/tools/miniapp_tools.py`)

The agent acts through the operator's own Telegram account with Telethon. It cannot do
this through the Bot API bot in `app/integrations/telegram.py`, because **one bot cannot
talk to another bot**. The tool surface went from **12 tools to 90** and is split so that
each module has exactly one job:

| Module | Tools | Owns |
|---|---|---|
| `telegram_user_tools.py` | 8 | The shared per-account session: the lock, pacing, daily cap, FloodWait mapping, button-kind classification, membership probing. **Everything else is built on this.** |
| `telegram_continuation_tools.py` | 2 | `telegram_return_to_bot`, `telegram_wait_for_reply`. |
| `telegram_service.py` | **0** | The shared **service layer**: paginated history, bounded waits, button location + pressability assertion, membership tri-state, task-line extraction, bot-state read. Delegates to `_membership_state`, `_locate_button`, `_button_kind`, `CLICKABLE_BUTTON_KINDS` and `sanitize_external_text` rather than re-deriving them. |
| `telegram_session_tools.py` | 9 | Is the account usable, and why not. Reads **local** process state and never touches the network, so a diagnostic cannot mask the problem it is diagnosing. |
| `telegram_bot_tools.py` | 12 | Bot discovery and commands. Nothing here fabricates a command list, menu button or button set Telegram did not return. |
| `telegram_message_tools.py` | 23 | Reading and navigating a chat. |
| `telegram_group_tools.py` | 7 | Leaving, membership proof, invite inspection. `telegram_get_invite_link_info` never joins to find out. |
| `telegram_campaign_service.py` | **0** | The campaign engine: taxonomy, checklist state machine, persistence mapping, reconciliation, verification. |
| `telegram_campaign_tools.py` | 18 | Thin shells around the engine — schema, permissions, plumbing. |
| `miniapp_tools.py` | 11 | Mini App detection, browser handoff, in-page verification, operator handoff. |

`bootstrap_tools()` registers the five new modules **only when**
`register_telegram_user_tools()` returned `True` (`bootstrap.py:74`), so the catalog is
either fully present or fully absent. Mini App *detection* is read-only and registers
unconditionally; `telegram_open_miniapp` is refused at invoke time if `browser.navigate`
was not granted.

#### The service-layer boundary

Pagination, bounded waiting, entity resolution, button location and the pressability
assertion exist **once**, in `telegram_service.py`. Three concrete reasons:

1. **The old gap was a missing capability, not a wrong one.** `telegram_user_tools.py`
   reads history with `get_messages(entity, limit=…)` and nothing else: `iter_messages`,
   `min_id`, `max_id`, `offset_id` and `search` appeared nowhere in the codebase. A client
   built without history paging can never see an older message, so no amount of prompting
   recovers it — the tool has to have the capability first.
2. **Order-dependence is a real, recurring defect here.** The fake and the high-level
   `get_messages` helper have both been observed returning oldest-first while Telethon
   returns newest-first. `_order_chronological` (`telegram_service.py:417`) sorts by id so
   the behaviour cannot depend on which client is underneath, and `load_older` anchors on
   the **minimum** over a window rather than on one message for exactly that reason.
3. **Safety rules that live in two places are safety rules that drift.** Button kinds,
   membership tri-state, secret refusal and the FloodWait mapping are defined once and
   delegated to. `assert_pressable` is therefore never looser than what
   `telegram_click_button` already enforces.

#### "Scrolling" is API pagination, and the results say so

A Telethon client is **not a user interface**: there is no viewport, no scrollbar and no
scroll position, so `telegram_scroll_*` cannot move a cursor because there is no cursor.
History is retrieved with `min_id` / `max_id` / `offset_id`. Those tools are honest names
over *page navigation*, and every result carries `navigation: "api_pagination"` plus a
note, so neither the model nor a human can mistake a paginated read for a real scroll. The
only genuine UI scrolling is `telegram_scroll_miniapp` / `telegram_scroll_to_element`
(inside the browser session a Mini App was handed to) and the browser's own
`browser_scroll`.

#### The campaign state machine

A **separate** machine from `TaskStatus`, on purpose. `TaskStatus` answers "what is
AirdropOS doing with this task"; `CampaignTaskState` answers "what has happened to this
one requirement inside the bot's campaign". They move independently — six `COMPLETED`
items and one `WAITING_FOR_USER` can coexist with an `IN_PROGRESS` task — and collapsing
them would force one to lie about the other.

Twelve states: `DISCOVERED`, `ANALYZING`, `READY`, `EXECUTING`, `VERIFYING`, `COMPLETED`,
`WAITING_FOR_USER`, `BLOCKED`, `FAILED`, `UNVERIFIED`, `EXPIRED`, `CANCELLED`. The table is
`VALID_TRANSITIONS` (`telegram_campaign_service.py:199`) and it is enforced, not merely
documented:

- `ChecklistItem.set_state` (`:1071`) → `assert_transition` (`:340`) → `can_transition`
  (`:336`) → the table. An illegal move raises `InvalidCampaignTransition` whose message
  names what *was* allowed, because the caller is usually a tool returning a failure to a
  model, and "invalid transition" alone teaches nothing.
- `advance_through` (`:345`) walks an item to a target along whatever legal path exists,
  rather than assuming where it currently sits. That is what makes "attempted is never
  completed" a property of the table: every completion passes through
  `VERIFYING → COMPLETED`, including one discovered by reconciliation
  (`reconcile_state:1815`) and one proven by another subsystem's tool
  (`telegram_verify_task_completion`, `telegram_campaign_tools.py:1368`).
- `UNVERIFIED` is reachable from almost everywhere and can be re-verified, re-executed,
  re-analysed or escalated — but **never straight to `COMPLETED`**. The only route that
  claims success is a real `VERIFYING → COMPLETED` pass.
- `COMPLETED` may be downgraded to `UNVERIFIED` or `EXPIRED` (a completion later
  contradicted by live state must be reopenable) but deliberately **not** back to `READY`,
  so the agent cannot quietly re-run work it already declared done.
- Several campaign tools move an item — `telegram_execute_campaign_step`,
  `telegram_verify_task_completion`, `telegram_reconcile_task_state`,
  `telegram_retry_failed_step` and `telegram_update_task_status` — and all of them do so
  through `set_state` / `advance_through`. None of them can move an item anywhere the table
  forbids, and `telegram_update_task_status` refuses an illegal transition with the same
  `InvalidCampaignTransition` the engine would raise. It also records its own move as
  `authoritative: False`, so it can report that something happened but can never grant a
  completion by doing so.

#### Persistence: the checklist lives in `task_requirements`

**No new table. No Alembic revision. No second database.** Each checklist item is a
`TaskRequirement` row (`app/models/context.py`), with the structured item in the existing
JSONB `evidence` column:

| Column | Carries |
|---|---|
| `text` | the requirement description (the bot's own line) |
| `origin` | `"tool_result"` — it came from a bot message |
| `source_ref` | `telegram:<chat_ref>#<message_id>` |
| `status` | `discovered` / `satisfied` / `waived` / `unverifiable` |
| `evidence` | the whole structured item: `task_key`, `order`, `task_type`, `dependencies`, `verification_method`, collected evidence refs, `retry_count`, `last_error`, `requires_user`, `state`, … |
| `is_account_specific` | True for a membership *this* account holds |

Why reuse rather than add a table: the rows, the columns and the guarantees already exist,
and `TaskContextService` already does three things this engine must not get wrong —
secret-screening every string on the way in, refreshing the denormalised outstanding list,
and notifying the context watcher. Writing the rows directly would silently lose all
three. So `persist_checklist` (`telegram_campaign_service.py:1536`) goes through
`record_requirement` / `satisfy_requirement` / `mark_requirement`, and the only direct
column writes are the idempotent evidence-JSONB updates on a row this engine already owns,
identified by `evidence["task_key"]`. `load_checklist` (`:1594`) reads back **only** rows
carrying a `task_key`, so a `TaskRequirement` written by the claim interpreter or the
airdrop tools is left strictly alone.

The schema/migrations story is therefore unchanged: `app/migrations/versions` still ends
at `8_wallet_default`, and `task_requirements` is unchanged since `6_task_context`. The
trade is real and worth naming — the structured state lives in a JSONB blob rather than in
typed columns, so the database cannot enforce its shape, and
`ChecklistItem.from_evidence` (`:1109`) must tolerate older and unknown shapes. It
downgrades an unrecognised `state` to `UNVERIFIED`, the state that claims nothing
(`:1114`–`:1120`).

#### Evidence → `verification_method`

A checklist item declares one `verification_method`. `TASK_TAXONOMY`
(`telegram_campaign_service.py:385`) is the single source of truth mapping
`task_type → {execution_tool, verification_method, evidence_kind, supported}` — including
the types that are **not** implemented (`quiz`, `social_task`, `unsupported`) and the
reason each is not. `VERIFICATION_EVIDENCE_KINDS` (`:546`) maps each method to the evidence
kinds that satisfy it. `verify_item` (`:2570`) requires **both** that the evidence is
`authoritative` and that its `kind` is in that method's accepted set; anything short of
that stays `UNVERIFIED` and is recorded as an `OUTCOME_UNVERIFIED` observation.

Three deliberate exclusions, each closing a real false positive:

- **`button_clicked` is not accepted for `message_observed`.** A press proves the press;
  it says nothing about whether the bot answered. Accepting it let a bare click close a
  `verify_with_bot` step — exactly the "joins the group, presses Verify, claims done"
  failure. `_exec_verify_with_bot` (`:2449`) instead waits for a **new** bot message after
  the press, and `ACCEPTED_TOOLS` (`telegram_campaign_tools.py:130`) applies the same
  exclusion at the tool layer.
- **`miniapp_launch_detected` proves a launch, never a completion.** It is the single most
  tempting false positive in the engine: the URL opened, the tool succeeded, and a careless
  verifier would call the task done while the required interaction never happened. The
  taxonomy says so with a `detection_only` flag.
- **`task_checklist_built` and `campaign_identified` may not satisfy a completion-shaped
  method.** They prove a checklist was *derived* and a chat was *matched* — that this was
  understood, not that this was done.

This is why the new evidence kinds had to be added to the controller at all.
`EVIDENCE_KINDS` in `app/agent/controller.py:106` is a closed set and it is what gates a
plan step (`:1893`, and the "satisfies it. Add it to EVIDENCE_KINDS if it is a real
method." message at `:1907` is what a step gets when it is not): a tool producing a kind
the controller does not know records the step as `UNVERIFIED` and it never closes.
`campaign_identified`, `checklist_built`, `task_checklist_built`, `task_verified`,
`campaign_resumed` and `miniapp_task_verified` were added at `:180`–`:189` for exactly
that reason — without them a genuinely-proven multi-stage Telegram step is
indistinguishable from one that did nothing, and the campaign stalls on the first step
that actually worked. The controller's table and the engine's table deliberately mirror
each other's shape; the controller's gates plan steps, the engine's gates checklist items,
and both agree about what proves what.

The trust chain has no weak link at the model: `telegram_record_task_evidence` only ever
records a **non-authoritative** observation (a model that could assert its own proof would
prove nothing), and `telegram_verify_task_completion` closes a step only from an
observation a real tool already recorded, matched against that tool's name in
`ACCEPTED_TOOLS`.

#### Reconciliation, not resumption

`reconcile_state` (`telegram_campaign_service.py:1672`) is what makes a restart safe, and
it is why "resume" is not simply "load the checklist again". It answers four questions,
and only with live evidence:

1. **Which persisted items are already satisfied?** Only for the types where live state is
   the proof — a membership is re-checked against Telegram's participant record. A click
   cannot be re-proved after the fact, and assuming it happened is exactly the bug this
   engine exists to avoid.
2. **Did the bot edit its instruction message?** If it did, items derived from that message
   go back to `ANALYZING` rather than being executed on stale wording.
3. **Did a button an item depends on disappear?** An item whose button is gone is blocked,
   not "ready" — this is what stops a stale `Verify` being pressed a second time.
4. **Did new tasks appear?** Normal, not an edge case: bots reveal the next task only after
   a verification. They are reported as new and are **persisted before they can be
   executed**, so a discovered task can never run without being on the record.

`next_action` (`:1949`) reconciles first and only then chooses; the order number is never
the answer. `resume_campaign` (`:2715`) is the restart path — load, reconcile, persist the
reconciled list, then choose — so the record never lags the actions.

#### What Telegram alone cannot verify

Telegram cannot observe another host. `visit_website`, `browser_task`, `miniapp_task`,
`check_eligibility`, `check_claim_status` and `onchain_check` are `handoff` types: the
engine names the tool and the parameters and leaves the item `READY`, because performing a
Telegram action and calling it verified would be a lie. `quiz` and `social_task` have no
authorised integration at all and route to the operator with the reason attached. Mini Apps
that need the authenticated Telegram WebView cannot be automated: `initData` and its
signature are never fabricated, and the result is a `NEEDS_USER_ACTION` handoff. See
[`TELEGRAM_SETUP.md`](./TELEGRAM_SETUP.md) for the operator-facing capability list.

## Testing & Verification

* **Backend**: `1 failed, 544 passed` (545 collected), measured 2026-10-16 with
  `.venv\Scripts\python.exe -m pytest backend/tests -q` on Windows / Python 3.11. Run the
  command from the **repository root**: from `backend/` it fails with
  `ModuleNotFoundError: No module named 'backend'`.
  - `test_api_auth.py` validates REST + WS auth/authz.
  - `test_wallets.py` ensures key material never leaves.
  - `test_chains.py` enforces catalog integrity.
  - `test_knowledge_graph.py` validates graph semantics.
  - `test_migrations.py` ensures ORM ↔ Alembic agreement.
  - `test_telegram_airdrop_workflow.py`, `test_telegram_campaign_engine.py`,
    `test_telegram_tool_surface.py` — **178 tests** covering the Telegram campaign
    journey, the checklist state machine / persistence / resume, and the tool surface
    (registration, permissions, schemas, secret redaction, injection handling, refusal of
    non-clickable buttons).
  - **The one failure is pre-existing and is not a regression.**
    `test_inbox_llm_interpretation.py::TestClaimsAreVerifiedForReal::test_a_login_claim_is_checked_by_the_real_telegram_readiness_check`
    fails with `assert 'telegram_worker_session' == 'telegram_session_readiness'`. It
    fails identically at `7ffcb4a`, before any Telegram work, and was deliberately left
    failing rather than "fixed" as a side effect.
* **Frontend**: `npm run lint` passes; `npm run build` successful.
* **All tools**: deterministic, sandbox‑isolated, audit‑recorded.

## Deployment

See [`docs/DEPLOYMENT.md`] for detailed instructions on running `docker-compose`, launching the worker, and configuring external integrations.

---

**Model compliance:**
- Single LLM enforced.
- No authentication bypass on REST.
- Every scope validated via `get_principal`.
- No real blockchain transactions unless explicitly enabled.
- All verification failures are captured and rolled back.

AirdropOS is built to be **secure by default**, with clear operator controls and comprehensive audit trails.

---

### References

- `SECURITY.md` – Threat model.
- `IMPLEMENTATION_CHECKLIST.md` – Ongoing gap documentation.
- `CONTINUATION.md` – Verification status.
- `docs/TELEGRAM_SETUP.md` – Operator-facing Telegram configuration and the honest
  capability list for the agent's user client.
- `backend/tests/` – All regression suites.
- `frontend/` – Dashboard code.
- `docs/` – Additional architecture and deployment details.