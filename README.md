# AirdropOS

Autonomous AI agent platform for crypto airdrop research, execution and monitoring.
One backend, one background worker and a web dashboard, driven by a single LLM that you configure.

## Quick Start

Full step-by-step guides for Windows PowerShell, Windows CMD, Ubuntu and VPS: **[DEVELOPMENT.md](DEVELOPMENT.md)**

```bash
git clone https://github.com/mainnetwallet/airdropos.git
cd airdropos

python run.py --set-password   # first time only: creates .env, sets your login password (Windows: py run.py ...)
python run.py                  # start backend + worker + dashboard

# Next time
git pull origin main
python run.py

# Docker (recommended for a VPS)
docker compose up -d --build
```

- Dashboard: http://localhost:5173 (`run.py`) or http://localhost:3000 (Docker)
- API docs: http://localhost:8000/docs
- Tests: `python -m pytest backend/tests/ -q`

## Features

- **Agent loop**: Observe, Plan, Act, Verify. A task is completed only with verified evidence.
- **Wallets**: import or watch wallets. Keys are AES-256-GCM encrypted and never exposed.
- **EVM transactions**: validation, gas estimation, spend limits, daily cap, nonce management.
  Nothing is signed or sent unless the operator explicitly enables it.
- **Browser automation** (Playwright) with a live broadcast view, plus Telegram, Discord and X integrations.
- **Memory, knowledge graph and learning engine** for airdrop research.
- **24/7 scheduler** for discovery, monitoring, claim checks and daily reports.

## Security

- Login required on every API route, with deny-by-default scopes.
- The LLM cannot sign transactions. Tools run through a governed registry.
- Emergency stop, spend limits, rate limiting, secret screening and audit logging with redaction.
- Single fixed LLM configuration. No fallback model.

Details: [SECURITY.md](SECURITY.md), [docs/WALLET_SECURITY.md](docs/WALLET_SECURITY.md)

## Documentation

- [DEVELOPMENT.md](DEVELOPMENT.md): run and deploy guide
- [docs/](docs): architecture, deployment, troubleshooting, Telegram, Discord and X setup
- [CHANGELOG.md](CHANGELOG.md), [IMPLEMENTATION_CHECKLIST.md](IMPLEMENTATION_CHECKLIST.md), [CONTINUATION.md](CONTINUATION.md)

## Contributing

Fork, create a branch, make your change, run the tests, open a Pull Request.
Changes must keep the authorization and security rules above.
