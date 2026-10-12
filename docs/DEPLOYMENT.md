# AirdropOS Deployment Guide

This document describes the deployment of the AirdropOS autonomous AI agent platform. It assumes familiarity with Docker, Python, PostgreSQL, and basic security practices.

## Prerequisites

- **Docker** (for containerized services) and `docker-compose`.
- **Python 3.10+** (for local development without Docker).
- **PostgreSQL** (external or Docker-managed).
- **Redis** (session store).
- **GitHub token** (if using any GitHub integrations).

## Quick Start (Docker)

Without Docker for the app, a single `python run.py` starts backend, worker and dashboard. See [`RUNNING.md`](../RUNNING.md) for Windows, Ubuntu and VPS steps.

Run all services with a single command:

```bash
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos
docker-compose up -d
```

### Services Launched

| Service | Port | Notes |
|---------|------|-------|
| `postgres` | 5432 | Stores users, wallets, tasks, audit logs, etc. |
| `redis` | 6379 | Session cache and task queues. |
| `backend` | 8000 | FastAPI application, WebSocket endpoint. |
| `worker` | N/A | Scheduler daemon running background jobs. |
| `frontend` | 3000 | React + TypeScript dashboard. |

### Configuration

All service configuration lives in `.env` (see `.env.example`). Key settings:

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_ENABLED` | Enable REST authentication (`true` in production) | `false` |
| `ENVIRONMENT` | Deployment environment (`production`/`development`) | `development` |
| `WALLET_VAULT_MASTER_KEY` | 64‑char hex AES‑256 key | Example in `.env.example` |
| `TELEGRAM_BOT_TOKEN` | Bot token for alerts | `null` |
| `DATABASE_URL` | PostgreSQL DSN | `postgresql://postgres:password@postgres:5432/airdropos` |

Create `.env`:

```env
AUTH_ENABLED=true
ENVIRONMENT=production
WALLET_VAULT_MASTER_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
DATABASE_URL=postgresql://postgres:password@postgres:5432/airdropos
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
```

## Production Considerations

### 1. Authentication Enforcement

The system defaults to **no authentication** (`AUTH_ENABLED=false`). Before deploying to any internet‑exposed environment:

1. Set `AUTH_ENABLED=true`.
2. Ensure an operator user exists (`init_db.py` seeds a default user if missing).
3. Configure the operator’s password (see `.env.example` for format).
4. Verify every REST endpoint requires a bearer token. Use `test_api_auth.py` to confirm no unprotected routes remain.

### 2. Database Initialization

PostgreSQL must contain the schema created by `init_db.py`. Two options:

#### a) Using `init_db.py` (fast, local)

```bash
.venv/Scripts/python.exe -m backend.app.services.init_db
```

This creates tables, indexes, and seeds baseline settings (operator user, default limits).

#### b) Alembic migration (recommended for production)

If you already have a PostgreSQL instance, run:

```bash
alembic upgrade head
```

Note: **Alembic 2** (`2_reconcile_schema_with_models.py`) modifies `wallets`, `knowledge_relationships`, `memory_records` to match the ORM. Running `alembic upgrade head` applies all revisions.

### 2b. Migration directories
`backend/alembic.ini` uses `%(here)s`-relative paths, so `alembic -c backend/alembic.ini ...` works from any directory. The only active chain is `backend/app/migrations/`. `backend/migrations/` is a stale, unused duplicate kept only so no history is deleted (see its `DEPRECATED.md`); never point Alembic at it. Do not run migrations against a production database without a backup.

### 3. Secrets Management

- **WALLET_VAULT_MASTER_KEY** must be a 64‑character hex string (256 bits). Store securely (e.g., secret manager, `.env` with restricted permissions).
- **Database passwords** should be stored in environment variables, not in the repo (`.gitignore` excludes `.env`).
- **Telegram/X/Discord bot tokens** should be kept secret and rotated periodically.

#### Worker egress
The worker's browser blocks internal destinations in code, but only a network-level egress firewall on the worker container can fully close DNS-rebinding and non-HTTP paths. See `SECURITY.md` section 2.7.1.

### 4. Resource Limits

Tune Docker Compose resource limits as needed. Example `docker-compose.yml` excerpt:

```yaml
  backend:
    ulimits:
      nofile:
        soft: 65536
        hard: 131072
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G
```

### 5. Monitoring & Alerting

The **Diagnostics Watchdog** (`app/diagnostics/watchdog.py`) persists subsystem health to `integration_health`.

Dashboard endpoint: `/api/v1/diagnostics` returns live status of database, RPC endpoints, LLM, browser, telegram, emergency stop, and tools.

Configure alerts via `TELEGRAM_BOT_TOKEN` and `TELEGRAM_AUTHORIZED_USER_IDS` in `.env`.

### 6
### 6. Docker Compose Override

For production, you may want to mount application code as a volume to avoid rebuilding images:

```yaml
  backend:
    volumes:
      - ./backend:/app/backend
      ```

Similarly, mount `docs/`, `scripts/`, and any custom startup scripts.

### 7. Worker Process

The `worker` service runs the scheduler daemon (`app/scheduler/worker.py`). It launches background jobs (`handle_airdrop_discovery`, `handle_campaign_monitoring`, etc.). Ensure the worker stays alive (Docker restart policy).

### 8. Rate limiting behind a reverse proxy

Rate limits are keyed on the connecting address. Behind the bundled nginx (or any reverse proxy) every client
would otherwise share the proxy's address and one bucket. The bundled `nginx.conf` forwards `X-Forwarded-For`;
the API only believes that header when the connecting peer is listed in `TRUSTED_PROXIES` (comma-separated
IPs/CIDRs, e.g. your compose network's subnet: `docker network inspect <project>_default`). Empty (the default)
trusts no forwarded header, so a client can never choose its own bucket by sending one.

- List only addresses your own proxies connect from. Never list a range that clients can reach the API from directly.
- The header is read right to left, skipping trusted proxies, so extra proxy hops are handled.
- Audit-log `ip_address` fields still record the connecting address; only rate limiting uses `TRUSTED_PROXIES`.

### 9. Emergency Stop: shared state, locking and limits

The stop is a JSON file (`EMERGENCY_STOP_STATE_FILE`) that the API and worker both read, so both containers must
mount the same `./data` volume (the bundled compose file does). Know its limits:

- If the activation cannot be written, the stop is engaged **only in that process**, and the API returns an error
  instead of success. Treat that as "not engaged elsewhere" and restart or retry. Nothing falls back silently.
- A state file that is corrupt or unreadable fails **closed** (stopped). A marker file
  (`<state file>.initialized`) is written after the first successful save; if the state file is later missing but the
  marker is present, startup fails **closed** until an operator resumes. If the whole `./data` directory is lost
  (marker included), a missing file still looks like a first boot, unless you set `EMERGENCY_STOP_REQUIRE_STATE=true`:
  then a missing file always means "stopped" until an operator resumes (including on the very first boot).
  Durable shared state in PostgreSQL would remove this limit but needs a schema migration; it is not done yet.
- Cross-process locking uses `fcntl` (Linux/macOS) or `msvcrt` (Windows). If no lock can be taken, a release
  (resume) is **refused** and the stop stays engaged; engaging a stop is never blocked by a lock failure. A release
  also re-checks the file just before writing, so it can never overwrite a stop engaged in the meantime.
- While stopped, the live gateway refuses session start, agent resume and manual input to a session. Reads (listing,
  frames, events) and pause stay available.

### 10. WebSocket session revocation window

Open WebSockets re-read their session every `WS_REVALIDATE_SECONDS` (10s) and end at the exact expiry time.
`/auth/logout` closes that session's sockets immediately. Any other revocation (account deactivation, a direct
database update) can keep an already-open socket streaming for up to ~10 seconds. A socket whose session cannot be
verified (e.g. database down) is closed after 3 consecutive failed checks.

## Local Development (without Docker)

```bash
# Install dependencies
.venv/Scripts/python.exe -m pip install -r requirements.txt
npm install

# Start PostgreSQL (example using docker run)
docker run -d -p 5432:5432 --name devdb -e POSTGRES_PASSWORD=password postgres

# Set environment
export AUTH_ENABLED=false
export ENVIRONMENT=development
export DATABASE_URL=postgresql://postgres:password@localhost:5432/airdropos

# Run database init
.venv/Scripts/python.exe -m backend.app.services.init_db

# Start backend (background)
# In a separate terminal:
# .venv/Scripts/python.exe -m backend.app.main

# Start frontend dev
npm run dev
```

## Testing

All automated tests pass:

```bash
# Backend suite
.venv/Scripts/python.exe -m pytest backend/tests/ -q
# Output: 439 passed

# Frontend lint
npm run lint
# Output: (0 errors)

# Frontend build
npm run build
# Output: success (dist/ built)
```

## Security Checklist

- [x] No real blockchain transactions unless explicitly enabled.
- [x] All REST endpoints require authentication.
- [x] Authorization scopes enforced per endpoint.
- [x] Secrets (wallet material, tokens, DB passwords) never logged or exposed.
- [x] Emergency stop blocks signing/submission.
- [x] Rate limiting active per‑IP, per‑bucket.
- [x] Audit logging for all security‑relevant actions.
- [x] Secret screening for memory and edge properties.
- [x] Verified evidence required for `COMPLETED` status.
- [x] No shell/SQL/network backdoor exposed to LLM.

## Troubleshooting

### Common Issues

1. **Authentication failures**
   - Ensure `AUTH_ENABLED=true`.
   - Verify `UserSession` exists and is not expired.
   - Check `scopes` include required permissions.

2. **Wallet import failures**
   - Invalid private key or mnemonic format.
   - Network not in allowed list (check `KNOWN_NETWORKS`).

3. **RPC errors**
   - `TransactionRejected` for unsupported networks; configure `RPC_*` env vars.
   - Missing `ethrpc` tools; ensure `blockchain_tools.py` is registered.

4. **Watchdog not reporting**
   - Ensure `app/diagnostics/watchdog.py` is imported in `main.py`.
   - Check database connectivity and table `integration_health`.

5. **Frontend fails to connect**
   - Verify backend is reachable at `http://localhost:8000`.
   - Check CORS settings (`backend/app/main.py` adds `CORSMiddleware`).

## Updating Dependencies

To upgrade a library safely:

```bash
# Update specific package
cargo add package@new-version

# Run tests to catch regressions
.venv/Scripts/python.exe -m pytest backend/tests/ -x
```

## Changelog

See [`CHANGELOG.md`](../CHANGELOG.md) for version history.

---

**Legal Note:** This software is for authorized security testing, defensive security, and educational purposes. Do not use for malicious activities.

---

For support, refer to the repository’s issues tracker or consult the project maintainers.

---

*Built with security as a first-class design constraint. No real transactions, no secret exposure, no unauthorized state changes.*

---

### References

- [`SECURITY.md`] – Threat model and mitigations.
- [`IMPLEMENTATION_CHECKLIST.md`] – Gap documentation.
- [`CONTINUATION.md`] – Verification status.
- [`backend/tests/`] – All regression suites.
- [`frontend/`] – Dashboard source.
- [`docs/`] – Additional guides (architecture, monitoring, etc.)

---

*Project maintains strict compliance with Specification Parts 1–42.*

---

### Version

- **Schema:** `2_reconcile_schema_with_models.py`
- **Test Suite:** 439 passed
- **Build:** `npm run build` successful
- **Lint:** `npm run lint` successful

---

*Deployment-ready, but remember to configure secrets and enforce authentication before any internet exposure.*

---

### Contacts

- **Security issues:** Follow responsible disclosure.
- **General questions:** Open an issue in the repository.
- **Team:** See `CONTRIBUTING.md` for coordination guidelines.

---

*End of deployment guide.*

---

> “Security is not a product, but a process.” — Bruce Schneier