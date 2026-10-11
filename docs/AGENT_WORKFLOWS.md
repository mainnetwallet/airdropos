# Agent Workflows

## The Central Loop

Every complex task runs `Observe → Plan → Act → Verify`, bounded by
`MAX_EXECUTION_LOOP_ITERATIONS` and per-task deadlines. See
[`ARCHITECTURE.md`](./ARCHITECTURE.md) for the diagram and
[`TASK_LIFECYCLE.md`](./TASK_LIFECYCLE.md) for state semantics.

## Airdrop Campaign Workflow

A representative end-to-end flow:

1. **Discover** — `web_fetch` / `site_search` over configured sources.
2. **Verify official identity** — confirm the project's own site and docs.
3. **Deduplicate** — `CampaignDeduplicator` collapses by domain + name fingerprint.
4. **Research requirements** — `read_documentation` extracts eligibility, claims, deadlines.
5. **Assess risk** — `assess_risk()` flags seed-phrase requests, guaranteed returns, urgency pressure.
6. **Evaluate eligibility per wallet** — `EligibilityEvaluator` checks balance, networks, tx counts.
7. **Refuse disallowed activities** — fake identities, sybil schemes, anti-abuse evasion → ineligible.
8. **Plan actions** — decompose into costed, verifiable steps.
9. **Execute permitted actions** — tools only, each permission-checked.
10. **Record evidence** — screenshots, receipts, extracted content.
11. **Monitor changes** — `ChangeDetector` diffs observations and emits `ChangeRecord`s.
12. **Track claims** — `ClaimRecord` lifecycle from pending to verified receipt.
13. **Verify rewards** — on-chain balance observation, explicitly marked verified or not.

## Replanning

When circumstances change, the planner classifies the failure, inspects external state, decides
whether a retry is appropriate, identifies alternative tools, updates the remaining plan, saves
a plan revision, and continues independent work.

Completed and irreversible actions are never blindly repeated. Permanent failures are not
retried indefinitely.

## Human Intervention

The agent parks a task in `NEEDS_USER_ACTION` only for CAPTCHA, MFA, session expiry, missing
credentials, or hardware-wallet interaction. It then checkpoints, notifies you, continues
unrelated work, and resumes when you clear the condition.

It does not attempt to solve CAPTCHAs, bypass MFA, or evade anti-bot controls.

## Reporting

Reports are evidence-based. Verified items, unverified claims, failures, and blockers are
listed separately. The agent does not present an unverified action as complete.
