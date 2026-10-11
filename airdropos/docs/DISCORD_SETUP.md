# Discord Integration

## Status

The Discord module is **specified but not yet implemented** in the current source tree. This
document records the intended design, required permissions, and unavailable capabilities so the
gap is explicit rather than implied.

## Intended Capabilities

- Read permitted channels via the official Discord Bot API.
- Monitor channels for campaign announcements.
- Extract structured campaign information with provenance.
- Track official updates and emit change records.
- Send permitted messages.
- Record evidence for every extracted claim.

## Required Permissions

| Permission | Purpose |
|---|---|
| `View Channels` | Read announcement channels |
| `Read Message History` | Read past announcements |
| `Message Content Intent` | **Required** — privileged intent, must be enabled in the Developer Portal |
| `Send Messages` | Only if posting is authorized |

## Configuration

```env
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_MONITORED_CHANNELS=["channel_id_1", "channel_id_2"]
```

## Prohibited

- **Self-bots.** Using a user account token is against Discord's Terms of Service. Only
  official bot accounts are supported.
- Spamming, fake engagement, or impersonation.
- Reading channels the bot was not invited to.

## Unavailable Capabilities

Some things are simply not possible through supported APIs and will not be attempted: reading
DMs, bypassing permission boundaries, bulk historical scraping outside the bot's guilds, or
evading Discord's own rate limits.

## Implementation Notes

Discord content is **untrusted data**. Extracted messages must pass through
`sanitize_external_text()` before reaching the LLM, and Discord's rate limits must be respected.
