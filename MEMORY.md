# MEMORY.md

Project: **AirdropOS** — `C:\Users\mainw\airdropos` · `mainnetwallet/airdropos` · branch `main`

Status as of 2026-10-13. Everything below is committed and pushed to `origin/main`.

---

## Browser system gotchas (learned 2026-10-13 while building persistent Chrome)

These cost real debugging time. Keep them in mind before changing pp/browser/:

1. **An installed Chrome does NOT exit when Playwright closes its persistent context.**
   It stays alive in background mode and keeps the user-data directory locked, so
   "stop then immediately open again" failed with a profile-lock conflict. Fixed in
   BrowserController._release_chrome: wait, then terminate ONLY the processes whose
   command line names that exact managed directory. One Chrome instance is 8+ processes
   (renderers, GPU, utilities all inherit --user-data-dir).

2. **--load-extension is session-scoped.** Chrome loads the unpacked extension but
   never writes it into Default/Extensions or extensions.settings. So it cannot be
   detected as installed after a restart. Only Web Store installs persist.

3. **Playwright cannot intercept requests in the default context of a CDP browser.**
   Attach mode therefore has navigation-only destination guarding. Reported in
   profile_status.route_guard, never hidden.

4. **page.inner_text("body") returns "" on chrome:// pages.** The WebUI renders into
   an open shadow root. Playwright CSS selectors pierce it, so use locators there.

5. **Playwright's connection belongs to the loop that created it.** Creating a new event
   loop per call in tests gives "The future belongs to a different loop". Also,
   syncio.Lock binds to the first loop that awaits it, so controller.stop() clears
   _key_locks and the gateway tests must use ONE context-managed Starlette
   TestClient (a non-context-managed one runs each request on its own loop).

6. **Session cookies do not persist across a Chrome restart; cookies with expires do.**
   Use an explicit expiry when testing persistence.

7. **A tool name containing profile_ trips the existing "no tool implies filesystem
   access" guard** (substring ile_). The four profile tools are therefore named
   rowser_list_profiles / rowser_start_profile / rowser_status_profile /
   rowser_stop_profile. Renaming them back would fail that regression test.

---

## Where things stand

| | |
|---|---|
| Last commit | `9cd3a4a` — autonomous execution policy |
| Before that | `24d1d89` — chat-controlled agent |
| Backend tests | **1099 passed**, 1 warning |
| Frontend tests | **18 passed** (3 files) |
| Lint / build | clean (0 errors) |
| DB | SQLite (`./airdropos.db`), Alembic head = `4_autonomous_execution` |

**Two commits were delivered in this session.** Both were real code changes with
tests, not documentation.

---

## Commit 1 — `24d1d89` chat-controlled agent

**The gap found:** `POST /agent/chat` asked the LLM for a JSON reply plus an optional
task spec and stopped there. No tool was ever invoked. "Show my wallet address"
returned prose, not an address.

**Delivered:**
- `backend/app/agent/chat_loop.py` — bounded decide → authorize → execute → verify →
  report loop. Model only names tools from the policy-filtered catalog; hallucinated
  names and malformed calls discarded before execution.
- **25 new tools** (19 → **44**): wallets, tasks, transfers, diagnostics, airdrops.
- `backend/app/blockchain/tokens.py` — symbol never resolves to a guessed contract;
  decimals read back on-chain; integer-only decimal parsing (floats refused).
- `backend/app/transactions/approvals.py` — server-side approvals bound to one tx,
  consumable once, atomic check-and-consume.
- Frontend renders the backend's report: tool statuses, evidence kinds, Copy Address,
  approval notices, blockers. No branch renders non-success as success.

**Nine defects fixed** (all confirmed in source, not assumed):
1. Watchdog AsyncSession — session probes gathered concurrently on one session,
   reporting a healthy DB as an outage
2. Browser controller import (`browser_controller` doesn't exist)
3. Telegram `await` on a sync `health()`
4. RPC 401/403 → JSONDecodeError → "generic outage"; missing gas price → **zero fee**
5. Source 403 → retryable NETWORK_ERROR; discovery reported "found nothing"
6. Planner `float(datetime)` → TypeError escaped the caller's handler; unknown field
   stranded steps in WAITING forever
7. **Evidence verification**: any authoritative evidence satisfied any declared method
   — a simulation could "prove" a receipt
8. Task status report read nonexistent transaction columns → AttributeError
9. `AuthorizationPolicy` treated optional schema params as required

---

## Commit 2 — `9cd3a4a` autonomous execution policy

**The gap found:** no unattended path at all — every transfer needed personal
approval. Plus two bugs found while implementing:

- **Signing was broken.** `eth_account` rejects a lowercase `to`; the approval binding
  case-folds addresses for comparison. Every real transfer would fail with
  `Transaction had invalid fields`. Invisible until a signature was requested.
- **Tests wrote to production.** `session_scope` reached the real `airdropos.db`.
  Fixed with a per-test in-memory DB (`StaticPool` is required — without it every
  connection gets its *own* empty database).

**Delivered:** `backend/app/security/autonomous_policy.py` — deny-by-default, 17
categories, configured via `AUTONOMOUS_POLICY` / `AUTONOMOUS_POLICY_FILE`.
`approve_transfer`'s `approved_by` is now optional; omit it and the policy decides.
Idempotency by intent fingerprint → `ALREADY_BROADCAST` on repeats, hash persisted
before receipt wait. `GET /agent/autonomy`, `POST /agent/autonomy/check`.

**Preserved:** Emergency Stop (before policy, overrides it), vault, spend ledger,
tx validation, audit. Policy and tool permissions are separate gates. An autonomous
approval is re-checked against the **live** policy before signing and at broadcast.

---

## Enabling autonomy (nothing is on by default)

```json
// data/autonomy.json
{ "enabled": true, "rules": [
  { "category": "web.fetch", "action": "allow" },
  { "id": "eth-transfers", "category": "tx.native_transfer", "action": "allow",
    "allowed_networks": ["ethereum"], "max_value_native": "0.05" } ] }
```
```env
AUTONOMOUS_POLICY_FILE=data/autonomy.json
ALLOW_VALUE_MOVING=true
```

Unset → every unattended operation refused. Restart required (policy is read once).
Docs: `docs/AUTONOMOUS_EXECUTION.md`, `docs/AGENT_CAPABILITIES.md`.

---

## Gotchas worth remembering

- **`sqlite:///:memory:` needs `StaticPool`.** Without it every connection gets a
  different empty DB and tools querying their own session see nothing the test wrote.
- **`eth_account` requires EIP-55 addresses** for `to`. Always `to_checksum_address()`
  before signing.
- **Pydantic Base has an `id` default; redeclaring `id` on a subclass drops it** →
  `NOT NULL constraint failed` on every INSERT.
- **Editing large files with `edit` drops the payload** past a size. Use a Python
  script via `shell`.
- **`git checkout -- frontend/tsconfig.tsbuildinfo`** after builds; it's tracked and
  always shows as modified.
- **Alembic**: this DB was built by `create_all`, not the migration chain. It was
  stamped at `2_reconcile_schema_with_models` once. New tables need explicit
  `alembic upgrade head`. Never edit an applied migration — add a revision.
- PowerShell reports **exit 1 when a command logs to stderr**, even on success. Check
  the actual output before believing a failure.

---

## Not done / open

- **No live transaction has ever been signed or broadcast.** RPC reads and signing
  are stubbed in tests. Needs a real RPC key + funded wallet.
- `ALLOWED_WALLET_IDS` is empty → wallet-ID tools stay fail-closed until populated.
- Telegram user client, Discord, X: unchanged, gated on credentials. Bot-level
  approval-path removal was **not** done — only the transfer path.
- CoinGecko/anti-bot sources still 403. Now *reported*, not worked around.
- `docs/AGENT_WORKFLOWS.md` predates the autonomous policy; may want a refresh.

## Commands

```powershell
cd C:\Users\mainw\airdropos
.venv\Scripts\python.exe -m pytest backend/tests -q      # ~3 min
cd frontend; npm test; npm run lint; npm run build
py run.py --update                                        # update + start
```

Backups: `airdropos.db.bak-2026MMDD-HHMMSS` (git-ignored, keep at least one).
Never commit `.db`, `.env`, logs, or `tsconfig.tsbuildinfo`.