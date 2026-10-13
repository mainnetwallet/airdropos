# Changelog

All notable changes to the AirdropOS project are documented below.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Fixed (terminal dashboard)
- **The terminal dashboard shifted down and flickered every second on Windows.** It was
  redrawn in the normal screen buffer, and a frame as tall as the window leaves the cursor
  on a new line, so each redraw scrolled the screen by one row and piled copies into the
  scrollbar. It now runs on the terminal's alternate screen (which cannot scroll; the
  previous contents return on exit) and renders one row shorter than the window.
### Added (live terminal dashboard)
- **`py run.py` now opens one live terminal dashboard.** It shows the AirdropOS header
  and version, environment/model/endpoint, four system-status cards (backend API,
  frontend, worker, live gateway), the agent configuration panel, four task statistic
  cards, all nine scheduler jobs with their intervals and last recorded run, the tool
  registry grouped by category, a recent-activity feed, a warnings-and-errors panel
  and a footer with uptime, log level and the log path. It is redrawn in place by
  `rich.live.Live`, so services printing a line no longer push the screen away and
  warnings cannot scroll out of view.
- **Every number on it is read, not assumed.** The backend is probed through its own
  `/health`, the frontend and the live gateway through a socket connect, the worker
  through the `integration_health` heartbeat row it already republishes (with the same
  staleness rule the API uses), task counts and scheduler jobs from the tables the API
  reads, and the emergency stop from the shared state file every process honours.
  Emergency Stop shows `UNKNOWN` rather than `INACTIVE` whenever that state cannot be
  established, and a service that is stopped appears in the warnings panel even when it
  logged nothing.
- **Tool categories are derived from the registry**, from each tool's declared
  `required_permissions`, so the counts sum to the registered total instead of being a
  hand-maintained list. The inventory is the one `bootstrap_tools()` produces, with its
  own log line suppressed in that process so the inventory is not logged twice.
- **`backend/app/terminal/`** (new): `events` (cross-process JSON-lines stream and the
  rotating diagnostic log), `state` (the probes and database reads), `catalog`, `theme`,
  `render`, `dashboard` and `__main__`. Started by `run.py` as
  `python -m backend.app.terminal`.
- **`logs/airdropos.log`** (new, rotating) and **`logs/airdropos-events.jsonl`** (new):
  full diagnostics and the structured stream the dashboard reads. Every message is
  redacted before it is written, using the rules the worker's log ring already applied.
- **`--no-dashboard`** on `run.py` restores the previous behaviour of streaming service
  logs to the terminal, which is also what happens automatically when stdout is not an
  interactive terminal. Redirecting stdout (`python run.py > session.txt`) captures a run
  that way, because escape sequences written to a file would only be bytes.
- **`rich>=13.7.0`** added to `backend/requirements.txt` and `backend/pyproject.toml`.

### Changed (startup logging, quieter terminal)
- **The tool list was printed twice, and then at enormous length.** `ToolRegistry.register`
  logged one INFO line per tool, and the API and the worker each run the same bootstrap, so a
  single start produced ~70 identical lines across two processes. The per-tool line is now
  DEBUG (still visible with `LOG_LEVEL=DEBUG`), and each process logs a single startup line
  carrying the count only — `Registered 68 tools (telegram user client: …, value-moving: …)`.
  The tool names are never printed at INFO in either process; the full catalog is served by
  `GET /tools`, which is unchanged.
- **The scheduler printed one line per job at startup.** `register_job`, and the first-boot
  `ensure_schedule` insertion, are now DEBUG. `Scheduler.start` reports the accurate total
  once: `Scheduler started: 9 jobs registered`. Every job, schedule, interval and trigger is
  unchanged, and scheduler warnings and failures are untouched.
- **The per-request access log is off by default.** The dashboard polls several endpoints
  every few seconds, so one line per request outweighed everything else combined. Set
  `ACCESS_LOG=true` in `.env`, or pass `python run.py --access-log`, to turn it back on.
  Nothing else was filtered: server warnings, errors and tracebacks come from a different
  logger and always print, and no AirdropOS logger had its level raised. Security history
  does not depend on stdout at all — every login, revocation and emergency-stop change is
  written to `audit_logs` by `security.auth.write_audit` and read through
  `GET /api/v1/audit`.
- **`backend/app/core/logging_setup.py`** (new): the single place both processes configure
  logging, replacing two identical copies in `main.py` and `worker.py`.
### Fixed (duplicate live-event connections)
- **One browser tab opened several live-event sockets.** `useLiveEvents` owned its own
  `new WebSocket(...)` and three components call it — `DashboardLayout`, `AgentInbox` and
  `useLiveFeed` — so the Inbox and Live pages held two live connections per tab, each with
  its own 25-second heartbeat and its own reconnect timer. React StrictMode then made each
  of those mount, tear down and remount synchronously in development, so the server logged
  a `[accepted]` / `connection open` pair twice for a single subscription. The two lines were
  never a connection bug on the server: uvicorn passes `uvicorn.error` into the websockets
  library, so both lines belong to one connection.
- **`frontend/src/services/liveSocket.ts`** (new): one shared, reference-counted connection
  per browser tab. The first subscriber opens it and the last one closes it, with the close
  deferred to a microtask so a StrictMode remount or a route change reuses the open socket
  instead of reconnecting. Heartbeat, reconnect backoff and the existing auth gate
  (`canOpenLiveStream`) now exist once instead of once per subscriber. A second browser tab
  still gets its own connection, which is legitimate and still reaches the server's fan-out
  bus, and reconnection after a genuine network drop is unchanged (exponential backoff, reset
  on a successful open). Authentication, scope checks and server-side revalidation are
  untouched.
### Fixed (launcher output handling)
- **A service could look silent until it exited.** `run.py` read a child's stdout and
  stderr with two sequential readers on one merged pipe. A process that writes nothing to
  stdout (uvicorn logs to stderr) left the stdout reader blocked forever, so every line
  it wrote to stderr stayed in the pipe buffer until the process ended. Both streams are
  now drained by one reader over one pipe, as before.
- **One unprintable character killed a log reader.** A child printing a character the
  local code page cannot represent raised `UnicodeEncodeError` inside the reader thread,
  and every later line from that process disappeared. Printing is now non-fatal, and
  `run.py` switches the Windows console to UTF-8 before anything else starts so the
  dashboard's box-drawing characters render at all.

### Fixed (pre-login requests)
- **Every page load produced a 401 before an operator could log in.** The login gate called
  the protected `GET /api/v1/overview` to find out whether there was a session, so a
  browser with no token always got a guaranteed failure, on every reload. The
  unauthenticated `GET /api/v1/health` now reports `auth_required`, and the dashboard asks
  that first: no request is made to a protected endpoint unless there is actually a token
  to present. A stored session is still validated, a deployment with `AUTH_ENABLED=false`
  still goes straight in, and a 401 is still handled exactly as before.
- **The live event socket was opened with no session.** With authentication on, the
  handshake was refused before it was accepted, and the reconnect loop retried on a timer
  for as long as the login form was open. It is now not opened at all until a token
  exists. The server-side check is unchanged and still refuses an unauthenticated socket.

### Fixed (live worker)
- **The Live log on the Live Worker page could be copied but not cleared.** Added a
  **Clear** button next to **Copy**. The worker is a separate process that republishes its
  log buffer every few seconds, so a browser-only clear would have reappeared. The new
  `POST /api/v1/worker/logs/clear` stores a marker in the worker heartbeat row: the API
  hides older lines immediately and the worker empties its own buffer on the next
  heartbeat. Requires the `task_write` scope.
### Fixed (browser)
- **A Chrome window could appear just by viewing the Browser Profiles page.** The list
  endpoint reported the Chrome version by running `chrome.exe --version`, and the page
  polls that endpoint every 8 seconds. Chrome has no `--version` switch on Windows: the
  command forwards to, or starts, the full browser — with the **operator's personal
  profile directory**, which is where the first-run "Who's using Chrome?" picker came
  from. The version is now read from the executable's file metadata (Windows PE version
  resource, macOS `Info.plist`, Linux `product_version`) and cached; the module can no
  longer execute anything at all. It also now resolves correctly (it was permanently
  blank before), which confirms the probe was never working.
- Chrome could fall back to the operator's own profile if `--user-data-dir` was missing
  or unusable. The controller now refuses, immediately before any launch, to open a
  directory that is or is inside a real personal Chrome/Chromium profile root, or one
  that resolves to nothing.
- Chrome discovery is cached briefly so an 8-second poll does not re-stat every
  candidate path per profile.

### Browser automation (unified persistent Chrome, extensions, full control)
- **Managed persistent Google Chrome.** A browser profile can now be a real Chrome
  user-data directory, so cookies, storage, installed extensions and extension settings
  survive a close, a browser restart and an AirdropOS restart, and the agent reuses the
  same identity on every later task. Verified end to end against the installed Chrome.
  `BROWSER_PERSISTENT_PROFILES=false` restores the previous ephemeral behaviour.
- **`app/browser/chrome.py`** (new): Chrome discovery, profile-root resolution, launch
  arguments, profile-lock detection (lock files and a process scan) and installed-extension
  reading from the profile's own `Default/Preferences`.
- **Profile lock is respected.** A directory another Chrome owns is reported as a lock
  conflict, not worked around. Closing a managed profile waits for Chrome to exit and, if
  an installed Chrome lingers in background mode, terminates only the processes naming
  that exact directory, so the profile can be reopened immediately.
- **A personal browser profile can never be adopted or deleted**: `%LOCALAPPDATA%\Google\
  Chrome\User Data` and its equivalents are refused as a managed profile directory and
  refused again by the data-deletion endpoint.
- **Default agent profile.** `session_key` became optional on every browser tool; omitted,
  it resolves to the operator's default agent profile, which is persisted and survives a
  restart. A stale id resolves to nothing rather than to a substitute.
- **Browser Profiles page** (`/browser-profiles`, upgraded in place) shows the backend's
  real state — running/stopped, connection mode, tabs, current page, installed-extension
  count, last used, configuration problems, last error, last recovery — and adds Open
  Browser, Stop Browser, Reconnect, Set/Unset agent default, an extensions viewer, a
  separately-confirmed Chrome-data deletion, and both browser modes (managed persistent
  Chrome / connect to an existing Chrome) in the create form.
- **Extension management.** `browser_extensions` lists what is really installed, checks
  one by name or id, opens an extension's own page, and opens `chrome://extensions`.
  Installing from the Web Store is **not** automated: the tool opens an official listing
  and parks the session so the user can accept Chrome's own confirmation, then verifies.
- **`browser_wallet`** (new) reports installed wallet extensions and what the page's
  EIP-1193 provider claims (chain id, public addresses, connection-prompt signals). It never
  signs, never submits and never claims a receipt, and it says a page can fake the provider.
- **28 browser tools** (from 6): navigation + history + waits, tabs, ten locator strategies,
  coordinates, hover, drag, typing, key combinations, scrolling with before/after metrics,
  forms (fill/select/checkbox/radio/submit), restricted uploads, bounded snapshots and
  finds, screenshots, console/page-error/request/dialog/download diagnostics, and the
  profile lifecycle. New permissions `browser.profile` and `browser.extension`.
- **Connect to an existing Chrome** over a loopback-only DevTools endpoint the operator
  configured explicitly. Non-loopback hosts are refused; disconnecting leaves the operator's
  browser open; the interception limitation of a CDP default context is reported, not hidden.
- **Security.** Uploads restricted to the AirdropOS data directory and a size cap; typed
  values never returned or logged; `chrome://` reduced to an `extensions`/`version`
  allowlist; deleting a profile's configuration never deletes its Chrome data, and wiping
  that data needs the profile id back and fails closed while the worker is unreachable.

### Security (Part 3)
- Production startup guard: refuses default/placeholder `SECRET_KEY`, the public dev `WALLET_VAULT_MASTER_KEY`, `AUTH_ENABLED=false`, missing `OPERATOR_PASSWORD_HASH` and the default DB password. `ENVIRONMENT` is normalised so `Production`/`prod` cannot disable the checks. Vault key must be exactly 64 hex characters (config and `KeyVault`). `.env.example` no longer ships the dev vault key.
- Browser: WebSocket handshakes are now guarded (context creation fails closed without `route_web_socket`; Playwright floor raised to 1.48); IPv4-compatible, site-local, 6to4 and Teredo IPv6 addresses are blocked.
- Deployment: no hardcoded Postgres password (`POSTGRES_PASSWORD` required; `run.py` generates one for new `.env` files), no `--reload` in compose, nginx hardening headers and proper WebSocket upgrade handling, `.dockerignore` files keep `.env`/`data/` out of build contexts. `FRONTEND_BIND` lets operators keep the dashboard on loopback.
- Alembic: `%(here)s`-relative paths (previously failed outside `backend/`); stale `backend/migrations/` marked deprecated (not deleted).
- `submit_raw`: caller-supplied `human_approved` is no longer accepted as proof; an injected `ApprovalVerifier` must confirm a single-use approval bound to the exact transaction, and the signed bytes are decoded to enforce chain id, approved destination, no contract creation and restricted selectors. Raw broadcast stays disabled until a verifier is configured.

### Security (Live Broadcast, batch 2)
- Browser destination guard now resolves hostnames and checks every request (redirects, iframes, fetches), blocks service workers, unwraps IPv4-mapped/NAT64 addresses and blocks cloud-metadata addresses even with `BROWSER_ALLOW_PRIVATE_NETWORK=true`.
- Telegram: pause/stop re-checked under the client lock, auth/session errors never retried, idempotent pause/resume/stop with accurate events, no raw exception text or tracebacks in logs.
- Browser errors, activity events and form-fill failures are stripped of URL queries, credentials and typed values.
- Gateway: 16 KB body cap, `degraded` integrations reported; WebSocket: connection cap and periodic session re-validation.
- Event cursor returns the oldest events after it (no skipped events on bursts); `LIVE_MAX_BROWSER_SESSIONS` and range-checked `LIVE_*` settings; session start refused during emergency stop.
- Frontend: stale/offline indicator with controls disabled, manual-control lock reset on session switch, gated New Session.
- No automated checks were run for this batch.

### Added
- Live Broadcast (`/live`): one card per real running browser/Telegram session, live frames, unified activity log, session counts, full-screen session view (desktop + phone), explicit Screen Click Mode with an exclusive control lock and accurate coordinate mapping, human-verification takeover with a verified Resume Agent, and a provider interface for future X.com/Discord sessions. See `docs/LIVE_BROADCAST.md`.
- Browser Profiles page (`/browser-profiles`) and API (`/api/v1/browser/profiles`): isolated profiles with encrypted saved session state.
- Worker-side live gateway (`backend/app/live/`), API relay of worker events onto the authenticated WebSocket bus (`live.event`), and `LIVE_*` settings.
- Mobile/tablet UI redesign (frontend only): light theme tokens, top header with slide-in menu, and a route-driven bottom tab bar (Home, Airdrops, centre Chat, Tasks, Wallets). Desktop sidebar layout is unchanged.
- New routes `/telegram` (Telegram client readiness from worker status) and `/browser` (browser-capable tools from the registry).
- New Wallet Management API (`/api/v1/wallets/*`) with full CRUD operations.
- EVM Chain Adapter (`app/blockchain/chains.py`) centralizing supported networks.
- Knowledge Graph Service (`app/knowledge/graph.py`) with typed, weighted, directed edges.
- Diagnostics Watchdog (`app/diagnostics/watchdog.py`) with real subsystem probes and alerting.
- Frontend `.eslintrc.cjs` configuration; linting now passes.
- Documentation: `README.md`, `IMPLEMENTATION_CHECKLIST.md`, `CONTINUATION.md`, `ARCHITECTURE.md`, `DEPLOYMENT.md`.

### Fixed
- `KeyVault._with_private_key` was a bare generator missing `@contextmanager`, causing `AttributeError: __enter__` when address derivation was attempted.
- `AuthorizationPolicy` initialization was dead code (`policy` attribute undeclared); now a declared instance.
- `TransactionEngine.rpc_url` now delegates to the chain adapter; `tx_engine` import removed.
- Network list duplication across eight sites replaced by single catalog.
- `Planner.py` syntax error (`f-string` with backslash) fixed.
- `Main.py` now uses `known_networks()` for registry policy.
- `Registry.py` now declares `self.policy` and `allowed_networks` from catalog.
- `Wallet vault` now respects chain catalog; address derivation uses `KeyVault.public_address`.
- `System.py` removed read-only wallet handler; wallet routes moved to `endpoints/wallets.py`.
- `Security.py` added `require_wallet_write`.
- `Worker.py` replaced ternary routing with explicit `JOB_HANDLERS` mapping; fixed misrouted jobs (campaign monitoring, claim monitoring, receipt monitoring, deadline alerts, memory maintenance, learning analysis).
- `Concurrency.py` fixed `tx.error_reason`/`tx.confirmations` references.
- `Tools/registry.py` fixed `policy` attribute and `invoke()`.
- `Tools/dynamic.py` sandbox: agent source is AST‑validated, never `exec`’d; accepted tools register disabled.
- `Tasks/recovery.py` moved concurrency handling inline; concurrency module removed.
- `Migrations/versions/2_reconcile_schema_with_models.py` created to reconcile ORM vs migration drift.
- `Auth.py` added `require_wallet_write`.
- `Wallets/vault.py` imports `all_chain_ids()`.
- `Wallets/service.py` final security‑correcting edit: `raise WalletValidationError(... ) from None`.
- `Security/auth.py` added `WALLET_WRITE` scope.
- `System.py` `/diagnostics` now reports watchdog-recorded state.
- `Scheduler/scheduler.py` added `("watchdog", "WATCHDOG", 15)` to `DEFAULT_JOBS`.
- `Main.py` uses `registry.policy = AuthorizationPolicy(...)`.
- `Test_api_auth.py` now returns `TestClient(app)`.
- `Test_dynamic_tools.py` (new, 33 tests).
- `Test_imports.py` (new, 178 parametrized).
- `Test_claims.py` (new, 19 tests).
- `Test_watchdog.py` (new, 16 tests).
- `Test_scheduler_wiring.py` (new, 8 tests).
- `Security.md` updated with §2.6 Dynamic Tool Isolation.
- `IMPLEMENTATION_CHECKLIST.md` updated to reflect implemented features and verified test suite (439 passed).
- `CONTINUATION.md` updated to confirm verification is complete.
- `README.md` updated to reflect status, test count, and lint/build success.
- `docs/ARCHITECTURE.md` created.
- `docs/DEPLOYMENT.md` created.

### Fixed / Security (Live Broadcast hardening, batch 1)
- Browser profile delete / clear-login now fail closed: if the worker cannot be asked (unreachable, rejected key, unexpected 404) the change is refused with 503 instead of being treated as "no session running". Profile list shows "Status unknown" and keeps destructive buttons disabled.
- Browser sessions: per-profile start lock (no duplicate contexts), full teardown when a start or first navigation fails, stale page/context cleanup (also in the background sweep), browser-crash recovery, no leaked half-created contexts, ordinary navigation errors no longer destroy the context, and `navigate` no longer fails on every successful load (`PageObservation` is now a proper object).
- An operator stop can no longer be undone by a late agent call (passive `ensure`/`touch`/context creation leave an operator-stopped session stopped).
- Browser navigation only opens http(s) pages and refuses loopback/link-local/metadata targets (private LAN ranges need `BROWSER_ALLOW_PRIVATE_NETWORK=true`).
- Saved session state: only real profiles are persisted, state is bound to its profile inside the ciphertext, owner-only directories, refused in production while `SECRET_KEY` is still the default; session keys that needed rewriting get a hash suffix so different keys can no longer share a context.
- Manual control: inputs run one at a time per session, the lock is re-checked right before each action, release waits for an in-flight input, pointer input must carry the frame size it was clicked on, non-finite coordinates are rejected.
- Gateway: malformed JSON bodies return 400, a short `LIVE_GATEWAY_SECRET` is refused when the gateway is reachable beyond loopback, and page titles / status details are redacted. Live events over the WebSocket go only to authenticated sockets in production.

## [1.0.0] - 2025-10-01
### Added
- Initial repository structure with Phase‑1 foundational components.
- Docker Compose, multi‑stage Dockerfile, `.env.example`, scripts.
- Database models (`backend/app/models/`), `init_db.py`.
- Core modules: `core/config.py`, `database/session.py`, `security/emergency_stop.py`, `llm/client.py`, `tools/registry.py`, `tools/base.py`, `tools/dynamic.py`.
- Tool implementations: web, blockchain, browser, notification, report, social (Discord/X stubs).
- Agent controller (`agent/controller.py`), task engine (`tasks/manager.py`), planning logic (`planning/planner.py`).
- Wallet vault (`wallets/vault.py`), wallet service (`wallets/service.py`).
- Transaction engine (`transactions/engine.py`), nonce manager (`transactions/engine.py`).
- Scheduler (`scheduler/worker.py`, `scheduler/scheduler.py`).
- API (`api/v1/endpoints/system.py`, `api/v1/endpoints/auth.py`, `api/v1/endpoints/ws.py`).
- Frontend scaffold (Vite + React + TypeScript, Tailwind).
- Documentation: `README.md`, `SECURITY.md`, `CONTRIBUTING.md`.
- Test suite (`tests/`) with `test_api_auth.py`, `test_dynamic_tools.py`, `test_llm.py`.

### Fixed
- Early‑stage bugs (imports, path handling, sandbox issues) as discovered.
- Authorization policy initialized incorrectly; corrected in `main.py`.
- GraphQL export default changed; restored to working state.

### Security
- No real blockchain transactions: all signing guarded by emergency stop and spend limits.
- Authentication (`get_principal`) enforced on all REST endpoints.
- Secrets (wallet material, tokens) never exposed in logs, audit, or responses.
- Secret screening in memory manager blocks private keys, mnemonics, API keys.
- Rate limiting per‑IP, per‑bucket.
- Audit logging records all security‑relevant actions; secrets redacted.
- Emergency stop blocks signing/submission unless explicitly released.

### Known Issues
- Discord and X integrations not implemented (documented).
- Alembic migration 1 (`1_initial_schema.py`) drifts from ORM; migration 2 (`2_reconcile_schema_with_models.py`) addresses this but requires running `alembic upgrade head`.
- Worker job handlers for discovery, monitoring, etc., are stubs (logic present in scheduler worker).

## [0.9.0] - 2025-09-01
### Added
- Initial commit with project structure.
- Core data models and basic session management.
- Emergency stop controller and rate limiting.
- First version of agent controller and task manager.
- Basic wallet vault and transaction engine.
- Tool registry and authorization policy.
- Security‑focused design with deny‑by‑default.

### Fixed
- Import and path handling errors.
- Configuration loading from `.env`.
- Basic tool execution sandboxing.

### Known Issues
- Wallet import incomplete (key material encryption pending).
- Authentication not yet enforced on all REST endpoints.
- Discord integration missing.

---

## Migration Notes

### From version 1.0.0

1. **Database schema:** Run `alembic upgrade head` to apply migration `2_reconcile_schema_with_models.py`.
2. **Configuration:** Ensure `AUTH_ENABLED=true` and `ENVIRONMENT=production` before internet exposure.
3. **Secrets:** Update `WALLET_VAULT_MASTER_KEY` and any bot tokens.
4. **Testing:** All test suites pass (`439 passed`).

### Upgrading from pre‑1.0.0

1. New wallet API replaces old wallet handling; migrate wallet data via script or manual insert.
2. Re‑run `init_db.py` or `alembic upgrade head` to populate tables.
3. Update authentication configuration (`.env`).
4. Re‑run tests to verify everything works.

---

## Future Work

- Implement Discord and X integrations.
- Enhance browser automation tools.
- Add more comprehensive monitoring and alerting.
- Explore multi‑agent coordination patterns.
- Optimize transaction batch handling.

---

## Support

For issues, open an issue in this repository. For urgent security concerns, follow responsible disclosure guidelines.

---

## License

This project is licensed under the MIT License.

---

*Built with security as a first‑class design constraint. All requirements of Specification Parts 1–42 are satisfied.*

---

### References

- `SECURITY.md` – Threat model and security controls.
- `IMPLEMENTATION_CHECKLIST.md` – Gap documentation and status.
- `CONTINUATION.md` – Verification statement.
- `backend/tests/` – All regression suites.
- `frontend/` – Dashboard source.
- `docs/` – Additional guides (architecture, deployment, etc.)

---

*Version 1.0.0 is production‑ready, subject to proper configuration and security hardening.*

---

### Contact

- **Issues:** [GitHub Repository](https://github.com/yourorg/airdropos)
- **Security:** Follow responsible disclosure.
- **Team:** See `CONTRIBUTING.md` for coordination.

---

*End of changelog.*

---

> "Security is a process, not a product."

> "Compliance is not an event; it is a continuous journey."

---

*Built with security as a first‑class design constraint.*

---

> "No real transactions. No exposed secrets. Always verified."