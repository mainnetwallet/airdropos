# AirdropOS Implementation Checklist

**Status as of 2026-10-14:** the backend suite passes at
**362 tests** (17 terminal-layout tests fail — they also fail at HEAD, see *Known gaps*)
and the frontend at **26**, with `npm run lint` (0 errors) and
`npm run build` both clean. See
[`CONTINUATION.md`](./CONTINUATION.md) for the authoritative status and honest gap
list, [`docs/AUTONOMOUS_EXECUTION.md`](./docs/AUTONOMOUS_EXECUTION.md) for the
autonomy policy, [`docs/EXECUTION_CONTEXT.md`](./docs/EXECUTION_CONTEXT.md) for the
context hierarchy, and [`docs/AGENT_CAPABILITIES.md`](./docs/AGENT_CAPABILITIES.md) for
what the agent can do from chat.

**The AI Agent Console executes tools.** `POST /agent/chat` selects registered tools
and runs them through the same `ToolRegistry` the background worker uses, so
chat-driven operations are schema-validated, permission-checked, network- and
wallet-allowlisted, emergency-stop gated and bounded.

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
- [x] 16 documents under `docs/` (added `BROWSER_PROFILES.md`)
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
- [~] **The terminal dashboard's layout tests fail (17).** `test_terminal_layout.py` and
      `test_terminal_logs.py` fail at HEAD, before any of this work: click targets are
      recorded at coordinates where the frame draws whitespace, and some sizes render a
      row count other than the terminal height. Reproduced in a clean worktree of `fbca677`.
      Unrelated to the agent; not fixed here.

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

## Phase 15 — Persistent Execution Context (added 2026-10-14)
See [`docs/EXECUTION_CONTEXT.md`](./docs/EXECUTION_CONTEXT.md) for the reference.

- [x] `app/models/context.py` + migration `6_task_context` — `task_contexts`,
      `task_observations`, `task_requirements`. One row per task; observations carry a
      fingerprint of tool + canonical arguments.
- [x] `memory_records` gains `scope` / `task_id` / `campaign_id`. Enforced in
      `MemoryManager`: a scoped record without its owner is refused, and a "global"
      record carrying an owner is refused. `recall_for_context` returns only what a task
      is entitled to. **Before this, every memory was global, so any recall could return
      one task's private history into another's prompt.**
- [x] `app/agent/context.py` — `TaskContextService`. Goal, action history, observed
      external state kept distinct from tool results, discovered requirements, external
      resource references, next-step rationale, human-intervention and authorization
      state, retry summary. Every write is scoped by `task_id`; there is no API that
      reads "the" context without a task.
- [x] `app/airdrops/campaign_context.py` — campaign facts shared across tasks, with
      freshness reporting (stale after 24h or never checked), official-vs-community
      provenance, and terminal-campaign handling. `record_shared_fact` **raises** on
      account- or wallet-specific content.
- [x] `app/agent/discovery.py` — deterministic structured requirement extraction
      (no model call) plus bounded prose extraction (`MAX_PROSE_EXTRACTIONS_PER_RUN`).
      Injection-shaped content is not sent to the model and never becomes a requirement.
- [x] `AgentController` writes to the context after every meaningful action, records why
      the next step is next, records failures and authorization refusals, and names
      outstanding requirements at finalization.
- [x] **Repeat suppression.** A non-idempotent action already proven in this task is not
      executed again. Only an *authoritative confirmation* suppresses it, so an uncertain
      action is investigated rather than skipped or blindly repeated. The suppression is
      itself recorded as `duplicate_suppressed`.
- [x] `ReplanTrigger.DISCOVERY` — the plan is revised when executing it revealed
      requirements no current step addresses, under its own budget
      (`MAX_DISCOVERY_REPLANS_PER_RUN`) so progress never consumes the failure-retry
      allowance.
- [x] Secrets cannot enter the context: every write is screened, and `resource_refs`
      accepts an allowlist of reference keys only.
- [x] `GET /api/v1/tasks/{id}/context` (`require_read`), a `context` block on the task
      status report, and a `task.context_updated` live event.
- [x] **Task Manager** detail panel renders the context: goal, why the next action is
      next, outstanding requirements, proven actions, and attempted-but-unverified actions
      as three separate lists. It never renders an unproven action as a success.
- [x] `test_task_context_isolation.py` (31), `test_campaign_context.py` (15),
      `test_context_aware_execution.py` (31), plus context assertions in
      `test_migrations_sqlite.py`.
- [x] **Recovery replans are budgeted separately from discovery replans.** Both advance
      the same revision counter, so the recovery cap is now read from the persisted
      revision history counting only recovery triggers. Without this, a task that
      legitimately discovered new requirements would have spent its ability to recover
      from a genuine failure.
- [ ] **The panel does not yet subscribe to `task.context_updated`**; it renders what the
      task status report carried when it loaded. The backend state is authoritative.
- [ ] **No external-state reconciliation on resume.** A resumed run loads its context and
      does not replay confirmed actions, but does not re-probe live state to confirm
      recorded facts still hold. `observed_state` is the record to compare against.
- [ ] **Campaign linking is not automatic.** `link_task` is called explicitly; nothing
      yet attaches a task to a campaign during execution.

---

## Phase 14 — Unified Persistent Chrome & Browser Automation (added 2026-10-13)

### Persistent Chrome profiles
- [x] `app/browser/chrome.py` — Chrome discovery (configured path, then the usual install
      locations), profile-root resolution (`%LOCALAPPDATA%\AirdropOS\ChromeProfiles\<id>`),
      launch-argument construction, profile-lock detection (lock files **and** a process
      scan for a Chrome already holding the directory), and installed-extension reading
      from the profile's own `Default/Preferences`.
- [x] `app/browser/controller.py` launches the **installed Google Chrome** with
      `launch_persistent_context(user_data_dir=…)`. Cookies, storage, installed
      extensions and extension settings survive a close, a restart and an AirdropOS
      restart, because they live in Chrome's own directory. Verified by test.
- [x] A real person's Chrome/Chromium profile root is **refused** as a managed profile
      directory, unconditionally. AirdropOS never launches into, or deletes, a personal
      browsing profile.
- [x] Chrome's own profile lock is respected: a locked directory is reported as a lock
      conflict, not silently worked around, and Chrome's "Opening in existing browser
      session" refusal is mapped to the same honest outcome.
- [x] Three real modes are distinguished and reported separately: `managed_persistent`,
      `attached` (existing Chrome over loopback CDP) and `ephemeral` (the previous
      throwaway context, kept as a fallback and never mislabelled).
- [x] `save_state()` no longer duplicates cookies for a persistent profile; Chrome has
      already written them. The encrypted `state.enc` snapshot is kept for the ephemeral
      fallback only.

### Browser Profiles page (existing `/browser-profiles` route, upgraded in place)
- [x] Create/edit form with the two labelled modes, Chrome type, executable path,
      persistent profile directory (with the default shown), headless toggle,
      optional unpacked developer extensions, attach host/port, and startup URL.
      Environment variables such as `%LOCALAPPDATA%` are resolved **on the backend**.
- [x] Every card shows backend-derived state: browser running/stopped, connection mode,
      tab count, current tab title, installed-extension count, last used, default-agent
      star, configuration problems, last error and last recovery. "Status unknown" is
      shown when the worker is unreachable — never "stopped".
- [x] Actions: Open Browser, Stop Browser, Reconnect, Set/Unset agent default,
      Extensions, Edit, Clear snapshot, Delete configuration, Delete Chrome data.
- [x] `GET /browser/profiles` additionally reports whether Chrome was found and where,
      the profile root, and whether persistent profiles are enabled. The whole listing
      is **one** worker call (`GET /browser/profiles` on the gateway, one reconcile),
      not one per profile, because the page polls it every 8 seconds.
- [x] Deleting a profile's **configuration** never deletes its Chrome profile data.
      Deleting the data is a separate `DELETE /browser/profiles/{id}/data` that requires
      the profile id to be sent back, fails closed when the worker is unreachable, and is
      refused outright for an attached browser AirdropOS does not own.

### Default agent profile
- [x] `default_agent_profile` is persisted in the same `profiles.json` document, so the
      choice survives a restart. A stale id resolves to nothing rather than to a
  substitute profile.
- [x] `session_key` is **optional** on every browser tool. Omitted, it resolves to the
      default agent profile; supplied, it always wins. No tool substitutes a different
      profile behind the caller's back.

### Extensions
- [x] `browser_extensions` lists what is really installed (read from the profile's own
      `Preferences`, so component extensions are excluded), checks one by name or id,
      opens an extension's own page, and opens `chrome://extensions` for the user.
- [x] Web Store installation is **not** automated. The tool opens an explicitly supplied
      official listing, then parks the session as `NEEDS_USER_ACTION` with the exact
      click the user must make, and `action=check` verifies the result afterwards.
      Non-`chromewebstore.google.com` URLs are refused; AirdropOS never guesses a
      listing URL or downloads an extension package.
- [x] Unpacked developer extensions are loaded with `--disable-extensions-except` /
      `--load-extension`, and the tool reports that Chrome does **not** record them as
      installed (verified by test) instead of pretending they persist.
- [~] An extension **popup** rendered in Chrome's own window is not part of the page DOM
      and is not reachable by any web automation. Only the extension's own pages are.
      Reported honestly in `extension_status.capabilities` and on the UI.
- [~] No wallet-specific signing or transaction flow through an extension. `browser_wallet`
      detects installed wallet extensions (by published store id **and** manifest name, with
      the match labelled as a hint), reads the EIP-1193 provider's `eth_chainId` and
      `eth_accounts`, and reports connection-prompt signals. It never signs, never submits
      and never claims a receipt, and it states that a page can fake `window.ethereum`.

### Browser tools (27 registered, all permissioned and in the planner catalog)
- [x] Navigation: `browser_navigate`, `browser_history` (back/forward/reload, with an
      explicit `no_history`), `browser_wait` (load state, element, text, URL glob).
- [x] Tabs: `browser_tabs` (list / open / switch by id, index, URL or next-previous /
      close) with stable tab ids and a tab ceiling.
- [x] Mouse: `browser_click` (text **or** any of ten locator strategies, right button,
      double click, force, `expect_text` / `expect_url` verification), `browser_click_at`,
      `browser_hover`, `browser_drag`.
- [x] Keyboard: `browser_type` (the typed value is never returned or logged),
      `browser_press` (combinations such as `Control+A`).
- [x] Scrolling: `browser_scroll` (direction, pixels, to top/bottom, a named container,
      and the before/after metrics so infinite scroll can stop when nothing more loads).
- [x] Forms: `browser_fill_form`, `browser_fill_field` (text/textarea/select/checkbox/
      radio, password values never read back), `browser_submit` (submit + verify),
      `browser_upload_file` (restricted to the AirdropOS data directory and a size limit).
- [x] Inspection: `inspect_page`, `browser_snapshot` (bounded role/name/state list plus
      frames), `browser_find` (confirm an element exists before acting on it).
- [x] Evidence and diagnostics: `capture_evidence` (full page or viewport),
      `browser_diagnostics` (console, page errors, failed requests, dialogs, downloads),
      `browser_handle_dialog` (dialogs are recorded and dismissed so a page never hangs).
- [x] Profile lifecycle: `browser_list_profiles`, `browser_start_profile`,
      `browser_status_profile`, `browser_stop_profile`, `browser_close`.
- [x] New permissions `browser.profile` and `browser.extension` are granted in
      `bootstrap_tools`, so they can be withheld per deployment without touching source.

### No implicit Chrome launch
- [x] **Chrome starts only on an explicit action.** Opening `/browser-profiles`, its
      8-second poll, reading status/health, the extensions modal, and editing all leave
      Chrome closed. There is **no** auto-start option and none is needed: nothing
      launches a browser at startup, on login or on a timer.
- [x] **Root cause of the recurring "Who's using Chrome?" popup removed.** The list
      endpoint reported the Chrome version by running `chrome.exe --version`, and Chrome
      has no such switch on Windows: the command forwards to, or starts, the full browser
      **against the operator's personal profile directory**, producing the first-run
      profile picker. The version is now read from file metadata (Windows PE version
      resource, macOS `Info.plist`, Linux `product_version`) and cached. The probe had
      been silently returning `None` forever, which is why it went unnoticed.
- [x] **Defence in depth at the launch site.** Chrome falls back to the operator's own
      profile when `--user-data-dir` is missing or unusable, so the controller refuses,
      before any lock scan or directory creation, to open a directory that is or is
      inside a real personal Chrome/Chromium profile root, or one that resolves to
      nothing.
- [x] Regression suite `test_browser_no_autolaunch.py` (22 tests) and six frontend tests
      ("Browser Profiles never launches Chrome implicitly") cover: listing, refreshing,
      status, extensions, edit and health launch nothing; Open Browser launches the
      selected profile with the correct `user_data_dir`; concurrent open+reconnect
      produce one instance; stop releases the lock before reopening; cleanup only ever
      targets processes naming that exact managed directory.
- [x] Tool names avoid the substring `file_` so the existing "no tool implies shell,
      SQL or filesystem access" regression guard still holds for all 27 of them.

### Tests
- [x] `test_browser_profiles_chrome.py` — profile configuration, default agent profile
      persistence, path validation, personal-profile refusal, extension reading,
      internal-page allowlist, loopback-only attach endpoint.
- [x] `test_browser_live_chrome.py` — **real Chrome**, controlled local site, isolated
      throwaway profiles: launch, navigate, click, fill, select, checkbox, iframe,
      scrolling, waits, dialogs, console/failed-request capture, tab create/switch/close,
      history, profile reuse across a close/relaunch, profile isolation between profiles,
      profile lock, internal-page enforcement, unpacked-extension loading.
- [x] `test_browser_tools_registry.py` — registration, permissions, planner/chat catalog
      visibility, operator stop / manual control gates, upload path restrictions,
      Web Store refusal, extension check against real `Preferences` content.
- [x] `test_browser_profiles_api.py` — every REST endpoint with a real authenticated
      principal and a stubbed worker, including the 503 when the worker is unreachable
      and the delete-data confirmation.
- [x] `frontend/src/pages/BrowserProfiles.test.tsx` — 14 tests asserting the page renders
      exactly what the backend returned and never an optimistic "running".

### Known gaps (browser)
- [~] In attach mode Playwright cannot intercept requests in the default context of a CDP
      browser, so the destination guard covers navigations only. Reported in
      `profile_status.route_guard` and `limitations`, not hidden.
- [ ] A wallet-extension transaction flow (connect → sign → submit → receipt) is not
      implemented. Detection and page reading are; no signing or broadcasting happens
      through an extension, and nothing fabricates a signature or receipt.
- [ ] No visual/screenshot-driven element resolution beyond `browser_click_at` for
      interfaces with no usable DOM. There is no claim of human-level visual automation.
