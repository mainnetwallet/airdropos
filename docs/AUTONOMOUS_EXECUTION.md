# Autonomous Execution

This page explains how to let the agent act without asking you first, exactly what
that permits, and what it still cannot do.

The short version: **you grant autonomy with configuration, not with prompts.** The
agent reads a policy you wrote; it cannot widen it, and neither can the model.

---

## 1. Enable it

Everything is off by default. With no policy, the agent behaves exactly as before:
it asks you before it signs or sends anything.

Create a policy file (recommended on Windows, where embedding multi-line JSON in
`.env` is a reliable source of escaping mistakes):

```json
{
  "enabled": true,
  "rules": [
    { "category": "web.fetch",           "action": "allow" },
    { "category": "campaign.discover",    "action": "allow" },
    { "category": "campaign.research",    "action": "allow" },
    { "category": "eligibility.evaluate", "action": "allow" },
    { "category": "diagnostics.run",      "action": "allow" },
    { "category": "task.create",          "action": "allow" },
    { "category": "task.resume",          "action": "allow" },
    { "category": "evidence.collect",     "action": "allow" },
    { "category": "browser.navigate",     "action": "allow" },

    {
      "id": "eth-transfers",
      "category": "tx.native_transfer",
      "action": "allow",
      "allowed_networks": ["ethereum", "base"],
      "max_value_native": "0.05",
      "description": "Small ETH transfers on Ethereum and Base."
    }
  ]
}
```

Point at it in `.env`:

```
AUTONOMOUS_POLICY_FILE=data/autonomy.json
ALLOW_VALUE_MOVING=true
```

Or inline, if you prefer:

```
AUTONOMOUS_POLICY={"enabled":true,"rules":[{"category":"web.fetch","action":"allow"}]}
```

Restart. The Agent Console now shows an **Autonomous execution** panel listing every
rule, so you can see what you actually granted rather than what you intended to.

> `data/` is git-ignored. Keep `data/autonomy.json` somewhere backed up.

---

## 2. Categories

Every category is deny-by-default: a category with no `allow` rule is refused, and
the refusal names the missing rule.

| Category | Covers |
|---|---|
| `web.fetch` | Reading public web pages |
| `campaign.discover` | Airdrop discovery cycles |
| `campaign.research` | Reading a campaign's own pages |
| `eligibility.evaluate` | Per-wallet eligibility assessment |
| `browser.navigate` | Opening a page in a browser session |
| `browser.interact` | Clicking, filling forms |
| `task.create` | Creating tasks |
| `task.resume` | Resuming an interrupted task |
| `task.cancel` | Cancelling a task |
| `diagnostics.run` | Health and diagnostic sweeps |
| `evidence.collect` | Screenshots, receipts, records |
| `transaction.prepare` | Quoting a transfer (always read-only) |
| `tx.native_transfer` | **Moving native coin** |
| `tx.token_transfer` | **Moving an ERC-20 token** |

`transaction.submit` and `transaction.approve` are internal: signing and broadcasting
happen through the two categories above, so a rule that allows one cannot silently
allow the other.

---

## 3. Limits

Each rule can bound itself:

| Field | Effect |
|---|---|
| `max_value_native` | Ceiling on the amount, in the native coin. `"0.05"` means 0.05 ETH. |
| `max_value_usd` | Ceiling in USD. Unset defers to the operator's `MAX_TRANSACTION_VALUE_USD`, which is itself unset by default. If neither is set there is no USD ceiling at all. No new limit is ever invented. |
| `allowed_networks` | Only these chains. Omitting the network on a restricted rule is refused, not allowed. |
| `allowed_tokens` | Only these ERC-20 symbols. |
| `allowed_destinations` | Only these addresses. |
| `allowed_source_wallets` | Only these source wallets. |
| `require_receipt` | Wait for and verify a receipt before calling the operation done. |

---

## 4. What actually happens on a transfer

```
prepare  ->  authorize  ->  submit  ->  verify
(reads     (policy or     (signs,     (waits for
 chain)     the operator)  broadcasts)  a receipt)
```

1. **Prepare.** Reads the real balance, estimates the real fee, simulates the call.
   Signs nothing, sends nothing.
2. **Authorize.** Either you called `approve_transfer` with your own identity, or the
   agent called it without an identity and the **policy** decided. If no rule covers
   this exact transfer, the call returns `needs_user_action` with the precise reason
   — for example `Amount 0.01 exceeds the autonomous limit of 0.001 set by rule eth`.
3. **Submit.** The policy is re-evaluated against the live transaction immediately
   before signing. The signed bytes are decoded and compared to the approved terms
   (chain, destination, amount, calldata); a mismatch stops the broadcast.
4. **Verify.** A receipt is required by default. Success is reported only when a
   receipt confirms it. Broadcast-without-receipt is reported as `SUBMITTED`, never
   as done.

### Repeats are safe

Each transfer intent is fingerprinted (network + chain + wallet + destination +
amount + calldata) and stored with the approval. Asking for the same transfer twice:

- reuses the same approval rather than issuing a second one;
- if it already went out, returns the original hash with `ALREADY_BROADCAST` and
  `resent: false`.

The hash is written to disk the moment the node accepts the transaction, before any
receipt wait, so a crash in between still leaves evidence and a retry reports rather
than resends.

---

## 5. What autonomy does NOT grant

Autonomy is narrow on purpose. It does not grant:

- **A different operation class.** A rule for `tx.native_transfer` cannot authorize
  an ERC-20 transfer; the category is derived from the calldata, not chosen by the
  caller.
- **A tool permission.** The policy and the tool registry are separate gates. Both
  must pass.
- **The Emergency Stop.** It overrides every rule and cannot be re-enabled by one.
- **The vault, spend ledger, transaction validation, or audit logging.** Untouched.
- **Any claim of success.** "Broadcast" is not "confirmed". A missing RPC key is
  reported as a missing RPC key.

### The agent cannot invent an identity

`agent`, `system`, `auto`, `llm` and `assistant` are refused as approvers. The policy
identity (`policy:autonomous:<rule>`) is not a name you can type into a tool call and
have it work: spending such an approval re-checks the live policy against the actual
transaction, and an approval naming a rule that no longer exists is dead. Narrowing a
rule between approval and broadcast stops the transfer.

---

## 6. Inspecting it

**In the console.** The *Autonomous execution* panel shows every rule, its action and
its ceilings.

**By asking the agent.** `What can you do without asking me?`

**Over HTTP** (requires the `read` scope):

```bash
GET  /api/v1/agent/autonomy          # the whole policy
POST /api/v1/agent/autonomy/check    # would this specific operation run?
```

```bash
curl -X POST http://localhost:8000/api/v1/agent/autonomy/check \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"category":"tx.native_transfer","network":"ethereum","value_native":"0.01"}'
```

```json
{"allowed": true, "category": "tx.native_transfer",
 "reason": "Authorized by autonomous rule 'eth-transfers'.",
 "rule_id": "eth-transfers", "max_value_usd": null}
```

A refusal names the constraint:

```json
{"allowed": false, "category": "tx.native_transfer",
 "reason": "Network 'polygon' is not in the autonomous rule's allowed networks (ethereum, base).",
 "rule_id": "eth-transfers"}
```

---

## 7. Turning it off

Set `"enabled": false`, remove the rules, or unset `AUTONOMOUS_POLICY_FILE`, then
restart. Use the Emergency Stop for immediate effect — it does not need a restart and
it overrides everything.

---

## 8. Mistakes the policy will not prevent

Autonomy removes the human from the loop. It does not remove consequences:

- A rule that is too broad is acted on immediately and unattended. Start narrow
  (`web.fetch`, `campaign.research`), confirm behaviour, then widen deliberately.
- `max_value_native` is per transfer, not per day. If you configure the rolling 24-hour
  `MAX_DAILY_GAS_SPEND_USD` it applies on top; it is unset by default, and there is no
  daily transaction-count cap anywhere in the platform.
- An autonomous approval is bound to the transaction it was issued for. It is not a
  standing permission.
- Watch `GET /api/v1/agent/autonomy` and the audit log after enabling. Approvals
  record which rule authorized each one, so an unattended transfer is always traceable
  to a decision you wrote.