# Troubleshooting

## Startup

**`ModuleNotFoundError: No module named 'backend'`**
Run from the `airdropos/` directory, not from inside `backend/`.

**`WALLET_VAULT_MASTER_KEY must be exactly 64 hex characters`**
Generate one: `openssl rand -hex 32`.

**`Can't locate revision identified by '<hash>'`**
The database is ahead of the local migration history, or vice versa. Run `alembic current` then
`alembic history` to compare.

## Database

**`connection refused` or `could not connect to server`**
Postgres is not running or `DATABASE_URL` is wrong. Check `systemctl status postgresql`, then
verify host, port, and credentials.

**`relation "tasks" does not exist`**
Tables were never created: `python -m backend.app.services.init_db`.

**`too many connections`**
Lower `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` in `.env`, or raise `max_connections` in
`postgresql.conf`.

## LLM

**`LLM authentication failed (HTTP 401)`**
`LLM_API_KEY` is wrong for the configured endpoint. Many providers name the variable differently
from OpenAI; put whatever the provider calls it into `LLM_API_KEY`.

**`LLM endpoint or model not found (HTTP 404)`**
`LLM_BASE_URL` must include the version path (for example `https://api.openai.com/v1`), and
`LLM_MODEL_ID` must be a model that endpoint actually serves.

**`Cannot reach LLM endpoint`**
Check outbound network access and any proxy requirements.

**`LLM did not return valid JSON`**
The model ignored `json_mode`. The error includes a content snippet; the prompt likely needs a
sharper JSON contract. The system never falls back to another model — it reports the failure, as
required.

## Browser Automation

**Playwright browsers missing**
Run `playwright install --with-deps chromium`.

**`Target page, context or browser has been closed`**
The browser crashed. `BrowserController.navigate()` recreates the context and retries once; if
it persists, check `/api/v1/diagnostics` for the browser subsystem.

**`CAPTCHA or anti-bot challenge detected`**
This is correct behaviour, not a bug. The task moves to `NEEDS_USER_ACTION`. The agent will not
attempt to bypass it.

## Transactions

**`Projected daily spend exceeds the daily limit`**
`MAX_DAILY_GAS_SPEND_USD` is reached. Wait for the rolling window or raise it deliberately.

**`Unsupported chain_id`**
Add the chain to `SUPPORTED_CHAIN_IDS` in `wallets/vault.py` and to `rpc_url()` in
`transactions/engine.py`. The allowlist is intentional — never sign on an unnamed chain.

**`Signer address does not match the requested wallet address`**
The stored secret does not correspond to that address. Re-import the wallet. This check exists
to stop a request for one wallet being signed by another.

**`No RPC configured for network`**
Set the `RPC_<NETWORK>` variable in `.env`.

**USD cost is `null`**
Expected. The system never invents a token price. Supply an external price source.

## Emergency Stop

**Everything refuses to run**
Check `/api/v1/emergency-stop`. Resume with `POST /api/v1/emergency-stop/resume`. Note that
stopping cannot cancel already-broadcast transactions.

## Tasks

**Task stuck in `IN_PROGRESS` after a crash**
The worker resets interrupted tasks to `RETRYING` on startup via `recover_interrupted_tasks()`.
If not, set the status manually.

**Task stuck in `UNVERIFIED`**
The action returned success but produced no authoritative evidence. Inspect `error_details` and
`recent_executions`; the action genuinely could not be proven.

**`Invalid task transition`**
The state machine rejected the change. Consult `TASK_LIFECYCLE.md`. `CANCELLED` is terminal.

## Frontend

**Blank page, blank console**
Check that the backend is up and that the Vite proxy target matches. For production builds, set
`VITE_API_URL` and `VITE_WS_URL` before `npm run build`.

**WebSocket shows "Disconnected"**
The hook reconnects with exponential backoff up to 30 seconds. In production the backend rejects
unauthenticated WebSockets — pass a token.

**`Module "..." has no exported member`**
This is a type error. Note that no type checking was run during development, so expect to fix
these before deploying.
