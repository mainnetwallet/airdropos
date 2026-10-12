# AirdropOS Security Policy and Architecture

## 1. Security Overview

AirdropOS operates autonomously on financial and web environments. Because it coordinates wallet actions, browser automation, and AI decision-making, security is enforced through **deterministic code boundaries**, not merely LLM instructions.

## 2. Core Security Pillars

### 2.1 Prompt Injection Defenses
- All external inputs (web scrape results, Discord messages, Twitter posts, RPC responses) are strictly treated as **untrusted data**.
- External text is sanitized before ingestion into prompt templates.
- Instructions found in external web pages or social media **cannot** modify system configuration, disable security checks, alter spending limits, or change the active LLM.
- LLM outputs are parsed into strictly-typed Pydantic schemas before tool invocation.

### 2.2 Key Vault & Cryptographic Isolation
- **Private keys and seed phrases are NEVER provided to the LLM, logs, API responses, or database in plaintext.**
- Keys are encrypted at rest using AES-256-GCM with a dedicated master key (`WALLET_VAULT_MASTER_KEY`).
- The LLM can only formulate a structured *signing request* (network, to, value, data).
- The Transaction Engine independently validates the request against:
  1. Recipient address sanity (non-blacklisted, valid checksum).
  2. Maximum transaction value limits (`MAX_TRANSACTION_VALUE_USD`).
  3. Maximum gas limits and daily cumulative spending caps.
  4. Emergency stop state.
- Signing occurs inside the isolated vault service, returning only the raw signed payload or submitting directly to the RPC.

### 2.3 Single LLM Fixed-Model Integrity
- Exactly one fixed model identifier is permitted.
- The system prevents dynamic model routing, model fallbacks, or secondary untrusted LLM endpoints.

### 2.4 Emergency Stop System
- A global hardware/software interlock (`EMERGENCY_STOP_ENABLED`) halts all transaction generation, wallet signing, and automated web task execution immediately.
- Active task checkpoints are preserved in PostgreSQL to prevent data corruption.
- Resumption requires manual administrative override via API or dashboard.

### 2.5 Browser Automation Sandboxing
- Playwright instances run in isolated user data directories with partitioned cookies and storage.
- Anti-bot and CAPTCHA detection triggers a `NEEDS_USER_ACTION` state rather than attempting illicit evasion.
- No arbitrary JavaScript injection from external sites is executed.

### 2.5.1 Live Broadcast & Manual Control
Manual browser control is opt-in per session: view-only by default, an exclusive server-issued lock per session, input bound to (operator, session, lock token, sequence), agent tools refuse to act while an operator holds the lock or the session is paused/stopped/waiting, and every acquire/release is audited. The worker gateway is internal (shared secret, loopback or private network only). Saved browser cookies are encrypted at rest. Browser navigation is guarded against SSRF per request (hostname resolution, redirects, sub-requests); DNS rebinding is narrowed but only an egress firewall closes it. Details: `docs/LIVE_BROADCAST.md`.

### 2.6 Dynamic Tool Isolation

Agent-authored tool code is **never executed inside the API process**.

The previous implementation of `app/tools/dynamic.py` called `exec()` on
LLM-supplied source at *proposal* time — before registration, authorization, or
the emergency-stop guards had run. A single tool proposal was therefore
arbitrary code execution: it could import `os`, read configuration and
credentials, write files, spawn subprocesses, and reach the database. The
module docstring claimed it "cannot import arbitrary modules"; the code did not
enforce that.

The current contract:

- Source is parsed to an AST and inspected. It is **never** passed to `exec`,
  `eval`, `compile`+`exec`, `pickle`, or an agent-named dynamic import.
- `app/tools/dynamic.py::_check_source` rejects imports, `eval`/`exec`/
  `__import__`, dunder attribute traversal (`__globals__`, `__subclasses__`,
  `__class__`, …), and global/nonlocal rebinding.
- A proposal must define exactly one top-level `class Tool` with an
  `async def execute`. Only the arguments passed to `execute()` are available.
- A dynamic tool may **never** declare `WALLET_SIGNING` or
  `TRANSACTION_SUBMISSION`. Value movement stays behind reviewed, statically
  registered tools and the full signing guard chain.
- Registration is gated on the live `AuthorizationPolicy`: a tool declaring an
  ungranted permission is rejected outright rather than registered-and-refused.
- Accepted tools are registered **disabled** and `execute()` returns
  `UNAVAILABLE` / `DYNAMIC_EXECUTION_DISABLED`. Agent capability comes from
  composing existing registered tools, not shipping new code.

**Rejected alternatives.** `RestrictedPython` was not adopted: it transforms
source to strip unsafe operations but still runs in-process, and its guards are
bypassable through object-graph traversal — exactly the class of escape the
static checker denies. A subprocess sandbox is the correct escalation if
operator-reviewed dynamic execution is ever genuinely required; it is not needed
while the only sanctioned path is static validation.

Regression coverage: `backend/tests/test_dynamic_tools.py`.

### 2.7 Authorization Policy Is Enforced, Not Merely Configured

`ToolRegistry.policy` is a declared attribute initialized to the deny-all
`DEFAULT_POLICY`, and `invoke()` falls back to **it** when a caller passes no
explicit policy (the agent loop always does).

This was previously broken. `bootstrap_tools()` assigned `registry.policy`, but
`ToolRegistry` never declared that attribute, so the assignment silently
created an instance field that `invoke()` never read. Every invocation fell
through to `DEFAULT_POLICY` (zero enabled permissions) and was refused with
`NOT_AUTHORIZED` regardless of operator configuration. Authorization was
fail-closed, so this was a functional failure rather than a security hole — but
operator policy was dead configuration.

Regression coverage: `test_invoke_consults_registry_policy_when_caller_passes_none`,
`test_invoke_is_denied_under_default_policy`.

### 2.8 Incident Reporting
If you discover a vulnerability or potential exploit, please email security@airdropos.internal or open a confidential security advisory.
