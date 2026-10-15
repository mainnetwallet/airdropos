# Telegram Setup

The Telegram bot and the web dashboard share the same database, task engine, and agent
controller. A task created in either interface is immediately visible in the other.

## 1. Create a Bot

1. Open Telegram, message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts.
3. Copy the returned token (format `123456789:AA...`).

## 2. Configure

```env
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_AUTHORIZED_USER_IDS=[111111111, 222222222]
```

To find your user ID, message [@userinfobot](https://t.me/userinfobot) or read
`from.id` from a webhook update.

**Only listed IDs may drive the agent.** Unauthorized users receive "Access denied" and their
messages are logged as rejected.

## 3. Start

The bot starts with the worker daemon:

```bash
python -m backend.app.scheduler.worker
```

Look for `Telegram bot polling started`. With no token or no authorized IDs, the bot logs
`Telegram bot not started` and everything else continues working.

## Commands

| Command | Behaviour |
|---|---|
| `/start` | Capability summary |
| `/help` | Command list |
| `/status` | Platform and agent status |
| `/tasks` | List active tasks |
| `/task AIR-2026-10-15-483920` | Full authoritative task report |
| `/create <title>` | Create a task |
| `/goals` | List long-term goals |
| `/wallet` | Wallet summary |
| `/discover` | Run discovery |
| `/stop` | Engage emergency stop |
| `/resume` | Release emergency stop |

Anything else is routed to the central agent as natural language.

## Duplicate-Execution Guard

Identical commands from the same user within a short window are deduplicated, and the user is
told the command is already being processed. This prevents a double-tap from firing the same
irreversible operation twice.

## Notifications

The agent sends to every authorized user via `NotificationDispatcher`, with a dedupe key so the
same change never alerts twice. Severity levels render as: 📋 info, ✅ success, ⚠️ warning,
❌ error, 🚨 critical.

A task reaching `NEEDS_USER_ACTION` triggers a notification naming the blocker.

## Security

- Never paste wallet secrets into Telegram. The bot has no command that accepts them.
- Tokens live in `.env` only; never commit them.
- The bot never exposes private keys, master keys, or stored API keys.
- Revoke access by removing a user ID from `TELEGRAM_AUTHORIZED_USER_IDS` and restarting.

## Webhook Alternative

For a public VPS, long polling is usually simpler than a webhook. If you need webhooks:

```env
TELEGRAM_WEBHOOK_URL=https://your-domain.example/telegram/webhook
```

Set it with `setWebhook` pointing at your reverse-proxied endpoint. Note that in production
(`ENVIRONMENT=production`) the dashboard WebSocket requires a token; the Telegram webhook
should likewise be fronted by your reverse proxy.
