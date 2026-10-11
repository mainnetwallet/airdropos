# Memory & Learning

## Memory

Memory is database-backed and category-tagged: verified facts, user preferences, project
knowledge, campaign requirements, successful workflows, failure patterns, tool reliability,
source reliability, execution outcomes, and unresolved blockers.

Each record stores content, source, related entities, confidence, timestamps, a verification
timestamp, and a review/expiry policy.

## Secret Screening

`MemoryManager.assert_no_secrets()` refuses to store content matching seed-phrase, private-key,
API-key, or token patterns. Memory is not a secret store.

## Revalidation

Time-sensitive memories carry an `expires_at`. `get_needing_revalidation()` surfaces lapsed
records so the agent re-checks them instead of trusting stale cached information. Revalidation
extends the TTL and nudges confidence upward.

## Consolidation

`consolidate()` merges near-duplicate memories by normalized content, keeping the
highest-confidence instance and slightly lowering duplicates' confidence rather than deleting
data outright.

## Knowledge Graph

`knowledge_relationships` links entities by (type, id) pairs with a relationship type and
weight — projects, campaigns, wallets, tasks, transactions, sources, tools, and outcomes.
Indexed relational tables are sufficient; a separate graph database is not used.

## Learning

`LearningEngine` analyzes task outcomes, repeated errors, and tool reliability to produce
insights with a sample size and confidence.

**One success is never treated as proof a workflow is universally correct** — insights carry a
minimum sample size, and low-sample observations get proportionally lower confidence.

Recommendations that would weaken security (disable, bypass, lower a limit, skip verification)
are rejected by policy and recorded with confidence 0.

Learning records are stored separately from authoritative task records so analysis never
corrupts ground truth.
