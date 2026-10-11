# AirdropOS Architecture Overview

## Overview

AirdropOS is an autonomous AI agent platform designed to research, execute, and monitor crypto airdrop campaigns. Built according to Specification Parts 1–42, the system emphasizes **strict security constraints**, **authorization enforcement**, and **verifiable operation**.

All critical operations are gated behind a deterministic authorization policy, with **no real blockchain transactions** unless explicitly enabled by the platform operator. The architecture is modular, with each subsystem implementing its own safety guards and verification mechanisms.

## Key Architectural Principles

### 1. Single LLM Policy
- Exactly one provider endpoint/API key/model.
- No routing, no fallback model.
- Enforced structurally in `FixedLLMClient.assert_model_locked()`.

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
- **Bearer token** for REST; **query parameter** for WebSocket.
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
| **Security** | PBKDF2‑HMAC‑SHA256 password hashing; Argon2‑equivalent; OAuth2‑style scopes |
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
- Single endpoint `/api/v1/ws?token=...`.
- Session resolved from `UserSession`; `principal` includes scopes.
- Emits `connected` event; responds to `ping` with `pong`.

## Testing & Verification

* **Backend**: `439 passed` (`.venv/Scripts/python.exe -m pytest backend/tests/ -q`).
  - `test_api_auth.py` validates REST + WS auth/authz.
  - `test_wallets.py` ensures key material never leaves.
  - `test_chains.py` enforces catalog integrity.
  - `test_knowledge_graph.py` validates graph semantics.
  - `test_migrations.py` ensures ORM ↔ Alembic agreement.
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
- `backend/tests/` – All regression suites.
- `frontend/` – Dashboard code.
- `docs/` – Additional architecture and deployment details.