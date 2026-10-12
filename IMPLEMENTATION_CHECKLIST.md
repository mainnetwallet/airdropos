# AirdropOS Implementation Checklist

**Status as of 2026-10-13:** the backend suite passes at **1051 tests** and the
frontend at **18**, with `npm run lint` and `npm run build` both clean. See
[`CONTINUATION.md`](./CONTINUATION.md) for the authoritative status and honest gap
list, [`docs/AUTONOMOUS_EXECUTION.md`](./docs/AUTONOMOUS_EXECUTION.md) for the
autonomy policy, and [`docs/AGENT_CAPABILITIES.md`](./docs/AGENT_CAPABILITIES.md) for
what the agent can do from chat.

**The AI Agent Console executes tools.** `POST /agent/chat` selects registered tools
and runs them through the same `ToolRegistry` the background worker uses, so
chat-driven operations are schema-validated, permission-checked, network- and
wallet-allowlisted, emergency-stop gated and bounded. The registry carries 44 tools.

Legend: `[x]` created · `[~]` partial/stub · `[ ]` not implemented

## Phase 1 — Foundation
- [x] Dedicated project directory, preserving unrelated work
- [x] `docker-compose.yml` (postgres, redis, backend, worker, frontend)
- [x] Multi-stage `Dockerfile` with Playwright and Web3
- [x] `.env.example` with no real secrets
- [x] `SECURITY.md` threat model
- [x] `app/core/config.py` — settings, single-LLM lock, spend limits
- [x] `app/database/session.py` — async engine and session management
- [x] 30+ database models with FKs, indexes, relationships
- [x] `app/services/init_db.py` table creation and seeding
- [x] Alembic migration chain (`1_initial_schema` → `2_reconcile_schema_with_models` →
      `3_transaction_approvals` -> `4_autonomous_execution`); ORM↔migration agreement
      pinned by `backend/tests/test_migrations.py`. Both later revisions are additive
      (one new table, then three new columns) and touch no existing row.

## Phase 2 — Single LLM & Central Agent
- [x] `FixedLLMClient` — the ONE LLM, model locked, no routing or fallback
- [x] `AgentController` Observe-Plan-Act-Verify loop
- [x] Conservative `_verify()` requiring authoritative evidence
- [x] Loop budgets, timeouts, cancellation, checkpointing
- [x] `app/planning/planner.py` — decomposition, persisted plan revisions, replanning (wired into `AgentController`)
- [ ] `app/agent/intent.py`, `context.py`, `decision.py` — **not implemented** (logic inline in controller)

## Phase 3 — Persistence & Recovery
- [x] Task ID generation (`AIR-YYYY-NNNNNN`) with collision skipping
- [x] 13-state machine with enforced transitions
- [x] `assert_completion_verified()` — COMPLETED requires evidence
- [x] `TaskManager` — CRUD, claiming, status reports, duplicate detection
- [x] Checkpoint persistence and resume
- [x] `FOR UPDATE SKIP LOCKED` concurrency control
- [x] `NonceManager` per-(network, address) locks
- [x] Worker crash recovery (`recover_interrupted_tasks`)
- [x] `app/tasks/recovery.py` — failure diagnosis/classification and retry backoff (used by the controller); interrupted-task recovery lives in `scheduler/worker.py`

## Phase 4 — Wallets, Blockchain & Security
- [x] AES-256-GCM key vault with ephemeral key scope
- [x] Independent `validate_request()` with forbidden selectors
- [x] Address-match guard preventing cross-wallet signing
- [x] `TransactionEngine` — validation, gas, simulation, receipts
- [x] Per-transaction and rolling daily spend limits
- [x] Destination denylist
- [x] `EmergencyStopController` with layered guards
- [x] Memory secret screening
- [x] Wallet Management API — `app/wallets/service.py` + `api/v1/endpoints/wallets.py` (import, watch, list, get, update, lock, activate, delete). `wallet_read` for reads, a separate `wallet_write` for every mutation. Key material is encrypted on import and never returned, logged, or audited. **Fixed `KeyVault._with_private_key`, which was a bare generator missing `@contextmanager` and so had never worked.**
- [x] `app/blockchain/chains.py` EVM chain adapter — the supported-network list had been duplicated in **eight** places; it is now derived from one catalog (`test_chains.py` pins this). Adapter is structurally read-only: no signing, no broadcasting.

## Phase 5 — Tools & Browser
- [x] `BaseTool` with strict schema validation rejecting unknown params
- [x] `ToolRegistry` with `AuthorizationPolicy` and pre-flight guards
- [x] Web tools with injection sanitization
- [x] Blockchain query tools
- [x] Browser tools with post-action evidence
- [x] Notification and report tools
- [x] Playwright controller with isolated contexts and CAPTCHA/MFA detection
- [x] `app/tools/dynamic.py` — **sandbox hole closed**: agent source is AST-validated, never `exec`'d; accepted tools register disabled and refuse to execute
- [x] `registry.policy` is now a declared attribute that `invoke()` consults — startup policy was dead code and every tool call was being denied

## Phase 6 — Airdrop Intelligence
- [x] `SourceReliability`, `VerificationClass`, `SourceClaimRecord`
- [x] `CampaignDeduplicator` with domain+name fingerprinting
- [x] Risk assessment (seed-phrase requests, guaranteed returns, urgency pressure)
- [x] `SourceVerifier` cross-source reconciliation
- [x] `EligibilityEvaluator` with per-wallet tracking and disallowed-activity refusal
- [x] `ChangeDetector` with impact classification
- [x] `app/airdrops/claims.py` claim lifecycle — 6-state machine with enforced transitions; expiry is checked before chain state, a missing receipt moves a claim to SUBMITTED rather than CONFIRMED, and an RPC failure leaves the claim untouched. Never signs or broadcasts.
- [x] Discovery wired into the scheduler — **the ternary routing that sent 6 of 9 job types to airdrop discovery is replaced by an explicit `JOB_HANDLERS` map** (`test_scheduler_wiring.py`)

## Phase 7 — Memory, Learning & Self-Improvement
- [x] `MemoryManager` with TTL revalidation and consolidation
- [x] `LearningEngine` with minimum sample sizes and policy rejection
- [x] `SelfImprovementEngine` with protected paths and `VALIDATED`-gated deployment
- [x] `app/knowledge/graph.py` — typed, weighted, directed edges with bounded traversal, weight-based staleness, secret screening on free-text properties, and explicit pruning on entity deletion (edges are intentionally not FKs). Exposed at `/api/v1/knowledge/*`.

## Phase 8 — API & Interfaces
- [x] FastAPI app with CORS and security headers
- [x] 20+ REST endpoints (system, agent, tasks, goals, wallets, tools, memory, learning)
- [x] Authenticated WebSocket event bus
- [x] Telegram bot with auth and duplicate-command guard
- [x] **REST authentication/authorization audited and enforced** — every route requires `get_principal`; scope per endpoint; regression suite in `backend/tests/test_api_auth.py` (439 passed)
- [x] WebSocket session binding — token must resolve to a live, unexpired, unrevoked `UserSession`; arbitrary tokens refused (was a bypass)
- [x] Campaign, transaction, audit log, settings endpoints
- [x] `AUTH_ENABLED=False` now actually honored; refuses to serve unauthenticated traffic when `ENVIRONMENT=production`
- [x] Settings secrets are write-only (`[REDACTED]` for every caller, including SYSTEM_ADMIN)
- [~] Discord — command router and REST reply exist, but there is **no gateway client**: `start_polling()` returns False and health reports `NOT_IMPLEMENTED`; nothing starts it
- [~] X — command router exists, but there is **no API listener and replies are only logged**: `start_polling()` returns False and health reports `NOT_IMPLEMENTED`; nothing starts it

## Phase 9 — Scheduler & 24/7 Operation
- [x] Persistent job definitions surviving restart
- [x] Worker daemon with graceful shutdown
- [x] RPC health checks persisting per-subsystem status
- [x] Discovery, campaign monitoring, claim monitoring, receipt monitoring, deadline alerts, memory maintenance, learning analysis — each bound to its own handler in `JOB_HANDLERS`
- [x] `app/diagnostics/watchdog.py` — real probes (database, backlog, LLM, RPC, browser, telegram, emergency stop, tools), persisted to `IntegrationHealth` + `DiagnosticRecord`, with transition-based counters and deduped alerts. `/diagnostics` now reports watchdog-recorded state instead of hardcoded `ok`.

## Phase 10 — Frontend Dashboard
- [x] Vite + React + TypeScript + Tailwind foundation
- [x] Dashboard layout with navigation and live connection indicator
- [x] Overview / Mission Control
- [x] AI Agent Console (chat)
- [x] Task Manager with inspection, cancel, retry
- [x] Tool Registry with health and authorization
- [x] Wallet Manager (secrets never exposed)
- [x] Security & Settings with emergency stop
- [x] Goals & Strategy
- [x] Memory & Learning
- [x] System Diagnostics
- [x] Self-Improvement proposals
- [~] Airdrop Explorer, Transactions, Social, Automation, Evidence — **still placeholder pages**. Read APIs exist for campaigns, transactions and audit; the UIs are not built. No schedule or evidence API.

## Phase 11 — Tests & Documentation
- [x] `test_tasks.py`, `test_security.py`, `test_llm.py`
- [x] `test_api_auth.py` — auth/authz regression suite (REST + WebSocket)
- [x] `test_dynamic_tools.py` — sandbox isolation regression suite
- [x] `test_wallets.py` — key material never leaves; `wallet_read` cannot import, lock, rename, deactivate, or delete
- [x] `test_chains.py` — catalog integrity, adapter read-only invariant, no-hardcoded-network-list guard
- [x] `test_knowledge_graph.py` — edge semantics, bounded traversal, cycle termination, pruning
- [x] `test_migrations.py` — revision chain, reversibility, ORM↔migration column agreement
- [x] `test_claims.py`, `test_watchdog.py`, `test_scheduler_wiring.py`, `test_imports.py`
- [x] **Suite is runnable and passing: 439 passed** (`.venv/Scripts/python.exe -m pytest backend/tests/ -q`)
- [x] `README.md`, `SECURITY.md`
- [x] 15 documents under `docs/`
- [x] `CONTINUATION.md` with honest gap list
- [x] Frontend `npm run build` (tsc + vite) passes
- [x] Frontend `npm run lint` passes (added the missing `frontend/.eslintrc.cjs`; eslint was installed but unconfigured, so the script had never run)

## Phase 12 — Deployment
- [x] `Dockerfile`, `docker-compose.yml`, frontend nginx config
- [x] `scripts/start_backend.sh`, `scripts/start_worker.sh`
- [x] `docs/DEPLOYMENT.md`, `docs/DATABASE_BACKUP.md`

## Known gaps (verified 2026-10-12)
- [~] Airdrop discovery runs on a hard-coded community URL list and **does not persist** discovered campaigns to the database.
- [~] Dynamic tools: proposals are AST-validated and stored inert; execution is intentionally disabled, so there are no working dynamic adapters.
- [ ] Human-approval store/API: `TransactionEngine` requires an `ApprovalVerifier`, but no implementation or approval endpoint exists, so broadcast fails closed.
- [~] Containers (backend, worker, frontend) run as root; the dashboard publishes on `0.0.0.0:3000` over plain HTTP unless `FRONTEND_BIND` is set.
- [~] Egress: destinations are vetted before each request, but there is no network-level egress firewall; DNS rebinding is narrowed, not eliminated.

## Phase 12 — Chat-Controlled Agent (added 2026-10-13)
- [x] `app/agent/chat_loop.py` — bounded decide -> authorize -> execute -> verify -> report
      loop invoked from `POST /agent/chat`. The model may only name tools from the
      policy-filtered catalog; hallucinated names and malformed calls are discarded
      before execution.
- [x] Wallet tools — `list_wallets`, `get_wallet_address`, `create_wallet`,
      `get_wallet_balances`. Ambiguous lookups ask rather than guess; no tool can see
      key material.
- [x] Task tools — `list_tasks`, `get_task`, `create_agent_task`, `resume_task`,
      `cancel_task`, all routed through the real `TaskManager` and state machine.
- [x] Transfer tools — `prepare_native_transfer`, `prepare_token_transfer` (read-only),
      `approve_transfer`, `submit_prepared_transfer`, `list_transfer_approvals`.
- [x] `app/blockchain/tokens.py` — a symbol never resolves to a guessed contract;
      addresses come from a curated catalog or `TOKEN_CONTRACTS`, and decimals are
      read back from the contract at use time. Amounts parse from decimal strings
      with integer arithmetic only; floats and over-precise amounts are refused.
- [x] `app/transactions/approvals.py` — server-side approvals bound to one
      transaction, consumable exactly once, expiring, with an atomic check-and-consume
      so two concurrent broadcasts cannot both succeed.
- [x] Diagnostics tools — `system_diagnostics`, `check_rpc_networks`,
      `list_tool_health`, `list_task_failures`, `worker_status`.
- [x] Airdrop tools — `discover_airdrops`, `list_campaigns`, `research_campaign`,
      `check_wallet_eligibility`.
- [x] Frontend renders the backend's report: real tool statuses, evidence kinds,
      copyable addresses and transaction hashes, approval notices, blockers. No branch
      renders a non-success as a success.
- [x] Fixed: `eth_account` rejects a lowercase `to`, so signing failed for every real
      transfer. Destinations are now EIP-55 checksummed before signing.

## Phase 13 — Autonomous Execution (added 2026-10-13)
- [x] `app/security/autonomous_policy.py` — declarative, deny-by-default policy that
      an operator configures without touching source. 17 operation categories.
- [x] `AUTONOMOUS_POLICY` / `AUTONOMOUS_POLICY_FILE` configuration. Malformed
      configuration raises at startup rather than silently defaulting.
- [x] Policy decisions are computed in code from operation facts. The model never
      supplies a decision, an identity, or a limit.
- [x] `ALLOW_VALUE_MOVING` (default **off**) gates the `wallet.sign` /
      `transaction.submit` permissions. Independent of the per-transaction policy.
- [x] Autonomous approvals recorded as `policy:autonomous:<rule>` with the rule id
      stored on the row, so an unattended transfer is traceable to a written decision.
- [x] An autonomous approval is re-authorized against the **live** policy immediately
      before signing and again at broadcast. Narrowing or deleting a rule between
      approval and broadcast stops the transfer.
- [x] The agent cannot invent an identity: `agent`/`system`/`auto`/`llm` are refused,
      and claiming the policy identity directly authorizes nothing on its own.
- [x] Idempotency: each intent is fingerprinted, so a repeat reuses the approval and
      returns the original hash with `ALREADY_BROADCAST`. The hash is persisted before
      any receipt wait, so a crash cannot cause a resend.
- [x] Emergency Stop, vault isolation, spend ledger, transaction validation and audit
      logging all untouched and still enforced over the autonomous path.
- [x] `GET /agent/autonomy` and `POST /agent/autonomy/check` expose the policy and
      answer "would this run?" with the exact constraint that refused it.
- [x] Agent Console shows the policy, its rules and its ceilings.
- [x] Test isolation fixed: every test now runs against its own in-memory database.
      Previously `session_scope` reached the operator's real `airdropos.db`, so test
      runs wrote wallets, tasks and approvals into production data.
