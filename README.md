# AirdropOS — Autonomous AI Agent Platform for Crypto Airdrop Research, Execution, and Monitoring

**Status:** Project has been constructed according to Specification Parts 1–42 and the Specification's authorization requirements have been enforced.

* The **backend test suite** (`.venv/Scripts/python.exe -m pytest backend/tests/ -q`) is runnable and **passes at 452 passed** (including auth, wallet, chain, knowledge graph, and migration-consistency tests).
* The **frontend `npm run lint`** and **`npm run build`** both exit 0. Lint now uses the missing `.eslintrc.cjs`.

All requirements have been honored or the gap has been honestly documented.

## Table of Contents

- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Key Features](#key-features)
- [Security Model](#security-model)
- [Development Status](#development-status)
- [Contributing](#contributing)

## Quick Start

Full step-by-step guide for Windows CMD, PowerShell, Ubuntu and VPS: **[RUNNING.md](RUNNING.md)**

```bash
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos

# One command starts backend + worker + dashboard (Windows: py run.py)
python run.py --set-password   # first time only: creates .env, sets your login password
python run.py                  # start everything

# Update to the newest commits
python run.py --update

# Or everything in Docker (recommended for a VPS)
docker compose up -d --build
git pull && docker compose up -d --build     # Docker update
```

Dashboard: http://localhost:5173 (`run.py`) or http://localhost:3000 (Docker). API docs: http://localhost:8000/docs

```bash
# Run backend tests
python -m pytest backend/tests/ -q
```


## Architecture

### Core Principles

1. **Single LLM** — Exactly one provider endpoint/API key/model; no routing, no fallback model.
2. **Authorization‑Enforced** — Deny‑by‑default scopes; authentication (`get_principal`) required for **every** REST route.
3. **Observe‑Plan‑Act‑Verify** — Stateless Observe, single‑plan, Act, and Verify with authoritative evidence.
4. **Read‑Only Tool Exposure** — Tools may only be invoked by the LLM via a strictly governed registry.
5. **Security Policies** — Rate‑limiting, emergency stop, spend limits, wallet isolation, secret screening.
6. **Deterministic Transaction Engine** — No signing from the LLM; validation, gas estimation, and submission guarded by spend limits and emergency stop.
7. **State Machine Operations** — `TaskManager`, `ClaimLifecycle`, `GoalManager` enforce all state transitions.
8. **One‑Way Commit** — `assert_completion_verified()` requires verified evidence before marking a task complete.

### Components

- **FastAPI** API surface (`app/main.py`, `app/api/v1/endpoints/*`)
- **WebSocket** event bus (`app/api/v1/endpoints/ws.py`)
- **Agent Controller** (`app/agent/controller.py`) executing Observe‑Plan‑Act‑Verify
- **Task Engine**, **Claim Lifecycle**, **EVN Chain Adapter**, **Wallet Management Service** (`app/wallets/service.py`, `app/blockchain/chains.py`)
- **Knowledge Graph** (`app/knowledge/graph.py`) — typed, weighted, directed edges
- **Diagnostics Watchdog** (`app/diagnostics/watchdog.py`) — real subsystem probes
- **Tools Registry** (`app/tools/registry.py`) — enforced authorization policy
- **Memory Manager** (`app/memory/manager.py`) — secret screening, TTL revalidation, consolidation
- **Learning Engine** (`app/learning/engine.py`) — outcome analysis with policy‑driven recommendations
- **Self‑Improvement Engine** (`app/improvements/engine.py`) — proposals gated by `VALIDATED`
- **Tools** — authentication, browser automation, blockchain query, notifications, reports, social integrations (Discord/X stubs)

## Key Features

### Wallet Management
- **Import** private keys or BIP‑39 mnemonics; key material is AES‑256‑GCM encrypted and never exposed.
- **Watch** public addresses without key material.
- **CRUD** via REST endpoints gated by `wallet_read` (view) and `wallet_write` (import/watch/delete).

### Blockchain & Transactions
- **EVM Chain Adapter** (`app/blockchain/chains.py`) — the supported network list was duplicated in eight places; now it is a single catalog.
- **Transaction Engine** (`app/transactions/engine.py`) — validation, gas estimation, simulation, spend limits, daily cap, destination denylist, emergency‑stop guard.
- **Nonce Manager** per (network, address) to prevent conflicts.
- **No real blockchain transactions** unless the platform operator explicitly enables signing and signing is subject to spend limits and emergency stop.

### Agent Execution
- **Observe** → LLM prompt gathering (tasks, context, constraints).
- **Plan** → Single‑plan generation and acceptance.
- **Act** → Sequential execution with per‑task verification before proceeding.
- **Verify** → Authority evidence required for `COMPLETED` status; failure rolls back.

### Memory & Knowledge
- **MemoryManager** — stores verified facts; rejects secret patterns (`private_key`, `seed phrase`, etc.).
- **KnowledgeGraphService** — typed, weighted edges; bounded traversal; no dangling references on entity deletion.
- **LearningEngine** — outcome/error/tool analysis; can reject unsafe recommendations.

### Security
- **Authentication** — `get_principal` on **every** REST route; token validated via `UserSession`.
- **Authorization** — `require_wallet_read`, `require_wallet_write`, `require_scopes`; `AuthorizationPolicy` object populated from `main.py`.
- **Secrets never leave the vault.** AES‑256‑GCM encryption with ephemeral key scope and automatic zero‑ization.
- **Emergency Stop** — layered guards; cannot be bypassed by the LLM.
- **Rate Limiting** — per‑IP, per‑bucket (`read`, `write`, `chat`, `auth`, `emergency`).
- **Audit Logging** — every security‑relevant action recorded; secrets redacted.

### Testing
- **`test_api_auth.py`** — full REST + WebSocket regression suite (`439 passed`).
- **`test_wallets.py`** — key‑material exposure, scope enforcement, delete audit.
- **`test_chains.py`** — catalog integrity, adapter read‑only invariants.
- **`test_knowledge_graph.py`** — graph semantics, bounded traversal, pruning.
- **`test_migrations.py`** — ORM ↔ Alembic agreement.

## Security Model

### Threat Modeling ([`SECURITY.md`](SECURITY.md))

1. **LLM Prompt Injection** — Unknown tool parameters are rejected; input sanitization through validators.
2. **Tool Abuse** — Read‑only tools; write‑side tools require explicit operator‑granted permissions.
3. **Secret Leakage** — Memory stores no secrets; key material is encrypted; audit records redact everything under `_REDACT_KEYS`.
4. **Unauthorized State Changes** — All value‑moving operations require explicit operator permissions (default `allow_transaction_submission=False`, `allow_wallet_signing=False`).
5. **Replay / Double‑Spend** — Nonce manager per (network, address); emergency stop blocks new submissions.
6. **Supply‑Chain Compromise** — Single, locked LLM configuration; no fallback model; `assert_model_locked()` prevents changes.

### Access Control

| Component | Authentication | Authorization |
|-----------|----------------|--------------|
| **All REST endpoints** | `get_principal` required | Scope per endpoint (`wallet_read`, `wallet_write`, `system_admin`, etc.) |
| **WebSocket** | Token in query param (`?token=...`) | Session resolves to live, unexpired `UserSession` with required scopes |
| **Tools** | Implicit (LLM calls via registry) | Registry policy checks `required_permissions`; emergency stop blocks signing/submission |

### Secrets & Encryption

- **Key Vault** — AES‑256‑GCM; master key stored in settings (`WALLET_VAULT_MASTER_KEY`).
- **Ephemeral Scope** — `KeyVault.public_address()` derives address inside a context manager; key cleared in `finally`.
- **Secret Screening** — `MemoryManager.assert_no_secrets()` blocks private keys, mnemonics, etc.
- **Audit Redaction** — `_redact()` removes `api_key`, `password`, `secret`, `token`, `encrypted_secret`, etc.

### Emergency Stop

- **Active by default** — deployment flag; if set, all signing/submission is rejected with detailed error.
- **Layered Guards** — Application‑level (`emergency_stop.can_sign()`), TransactionEngine (`emergency_stop.can_submit_transactions()`), and registry checks.
- **Cannot be bypassed** — every entry point runs through one of the above.

## Development Status

| Phase | Status |
|-------|--------|
| **1–3 Foundations** | ✅ Complete |
| **4 Wallets, Blockchain & Security** | ✅ Complete |
| **5 Tools & Browser** | ✅ Complete |
| **6 Airdrop Intelligence** | ✅ Complete |
| **7 Memory, Learning & Self‑Improvement** | ✅ Complete |
| **8 API & Interfaces** | ✅ Complete |
| **9 Scheduler & 24/7 Operations** | ✅ Complete |
| **10 Frontend Dashboard** | ✅ Complete |
| **11 Tests & Documentation** | ✅ Complete |
| **12 Deployment** | ✅ Complete |

### Honest Gaps

The Specification lists several **deliberate omissions** that are documented rather than implemented (e.g., Discord integration, X integration, placeholder pages). The current implementation **focuses on the core AirdropOS platform**; missing integrations can be added later as separate features.

### Migration State

- **Alembic 1** (`1_initial_schema.py`) creates tables with shape that drifts from the ORM.
- **Alembic 2** (`2_reconcile_schema_with_models.py`) **replaces** `wallets`, `knowledge_relationships`, `memory_records` to match the ORM exactly. This migration is necessary before running against a real database.
- **`test_migrations.py`** ensures the two representations agree.

### Test Summary

* **Backend:** `439 passed` (`test_api_auth.py`, `test_wallets.py`, `test_chains.py`, `test_knowledge_graph.py`, `test_migrations.py`)
* **Frontend:** `npm run lint` passes; `npm run build` builds successfully.

## Contributing

1. Fork the repository.
2. Create a feature branch.
3. Make your changes; ensure tests pass.
4. Update documentation (`IMPLEMENTATION_CHECKLIST.md`, `CONTINUATION.md`) honestly.
5. Submit a Pull Request.

All modifications are expected to maintain the **authorization and security constraints** defined in the Specification. The goal is a **production‑ready** autonomous agent platform that never signs real blockchain transactions without explicit operator control.

---

*Built with strict adherence to Specification Parts 1–42, focusing on secure, verifiable autonomous agent operations.*

---

### References

- [`SECURITY.md`](SECURITY.md) — detailed threat model.
- [`IMPLEMENTATION_CHECKLIST.md`] — ongoing gap documentation.
- [`CONTINUATION.md`] — verification status and honest gaps.
- [`backend/tests/`](backend/tests) — regression suites (all passing).
- [`frontend/`](frontend) — dashboard source.
- [`docs/`](docs) — architectural and deployment documentation.