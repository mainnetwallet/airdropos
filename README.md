# AirdropOS

Autonomous AI agent platform for crypto airdrop research, execution and monitoring.
One backend, one background worker and a web dashboard, driven by a single LLM that you configure.

## How to run

To run AirdropOS, follow the guide in **[DEVELOPMENT.md](DEVELOPMENT.md)**.
It covers Windows PowerShell, Windows CMD, Ubuntu and VPS (Docker), step by step: install, clone, `.env`, start, and update.

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
