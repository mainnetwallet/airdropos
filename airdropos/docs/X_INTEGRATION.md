# X / Twitter Integration

## Status

The X module is **specified but not yet implemented** in the current source tree. This document
records the intended design and the real constraints of official API access.

## Intended Capabilities

Using official or explicitly authorized API access:

- Monitor selected accounts via the users-by-username and timeline endpoints.
- Read permitted posts.
- Search supported public content where the access tier allows.
- Detect campaign announcements.
- Track project changes and emit change records.
- Record evidence with provenance.
- Publish permitted posts where authorized.

## Configuration

```env
TWITTER_BEARER_TOKEN=...
TWITTER_API_KEY=...
TWITTER_API_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_SECRET=...
```

## Access Tiers Matter

Capabilities differ substantially by tier and this is **not** glossed over:

| Capability | Requirement |
|---|---|
| Read public posts by ID | Basic |
| Search recent posts | Basic |
| Full archive search | Academic / Pro |
| Post, reply, like, follow | User context with OAuth 2.0 |

AirdropOS does not claim to support operations the configured tier cannot perform. When an
endpoint is unavailable for the current tier, the agent reports the limitation rather than
pretending.

## Rate Limits

X enforces strict per-endpoint rate limits. Requests must be spaced, responses cached, and
rate-limit information tracked on the integration account record.

## Prohibited

- Self-bots or reverse-engineered private APIs.
- Automated engagement farming, mass following, or spam.
- Posting that impersonates a project or person.
- Evading platform rate limits or automation rules.

## Implementation Notes

Posts are **untrusted data**. They pass through `sanitize_external_text()` before reaching the
LLM. A post claiming "airdrop ending now, connect your wallet and sign this transaction" is data
to be analyzed, never an instruction to follow — and connecting a wallet to an unknown site is
exactly the flow the security model refuses to execute.
