# What the agent can do, and what it must never do alone

This page is the operator's reference for the AI Agent Console. It describes what the
agent actually does when you type something, and — more importantly — the parts that
will stop and ask you.

The short version: **the agent reads and acts, but it does not decide.** Every action
goes through the same `ToolRegistry` the background worker uses, so it is
schema-validated, permission-checked, network- and wallet-allowlisted, emergency-stop
gated and time-limited before it runs. The model proposes; deterministic code disposes.

---

## Try these

Type any of these into the Agent Console:

| You type | What actually happens |
|---|---|
| `Show my wallet address.` | Reads the wallet record and returns the real public address, with a **Copy** button. If you have several wallets it asks which, rather than guessing. |
| `Create a wallet.` | Generates a key inside the vault and returns the address. The private key is never shown, logged, or exportable. |
| `Show my tasks.` | Reads the database. The status shown is the stored status, not a guess. |
| `Continue my previous task.` | Finds the unfinished task and returns it to `READY` so the worker resumes it from its checkpoint. |
| `Stop this task.` | Cancels it — unless a worker is mid-run, in which case it tells you so rather than stranding the lease. |
| `Why did this fail?` | Runs the real diagnostics sweep and reports measured status for the database, model, browser, Telegram, tools, emergency stop and every RPC endpoint. |
| `Check all configured RPC networks.` | Probes each chain and reports `HEALTHY`, `NOT_CONFIGURED`, or the exact failure category. Endpoint URLs are never printed (they carry your provider key). |
| `Find good airdrops.` | Runs the discovery cycle and reports what it found **and which sources it could not read**. |
| `Start this airdrop.` | Creates a real task (`AIR-YYYY-MM-DD-NNNNNN`) and queues it for the worker. |
| `Show my recent transactions.` | Reads the transaction records. |
| `Transfer 0.01 ETH to 0x…` | **Prepares** a transfer and shows you the exact terms. Signs nothing. See below. |

---

## Transfers: the three-step rule

A transfer is deliberately three separate steps, and the agent only ever takes the
first one unless you have approved that exact transfer.

**1. Prepare.** `Transfer 0.01 ETH to 0x1234…` reads the real balance, estimates the
real fee, simulates the call, and returns:

- source wallet, network and chain id
- destination
- asset, amount, and the amount in exact base units
- estimated fee
- warnings (including a simulation revert, if the dry run reverted)

Nothing is signed and nothing is sent. The amount is parsed from a **decimal string**.
Sending `0.1` as a float is refused, because a float cannot represent `0.1` exactly and
the difference is real money.

**2. Approve.** Once you confirm those exact details, the agent records a server-side
approval naming **you**, bound to that network, wallet, destination, amount and
calldata. One approval covers one transaction and expires (default 15 minutes).
`agent`, `system` and every other non-human identity are refused.

**3. Submit.** The agent signs through the vault, broadcasts through your configured
RPC, then waits for the receipt. It reports success **only** when a receipt confirms
it. "Broadcast" on its own is reported as `SUBMITTED`, not as done.

Before the bytes are broadcast, the signed transaction is decoded and checked against
what you approved: chain id, destination, value and calldata. A mismatch stops the
broadcast and says which field differed.

### Enabling value moving

Off by default. Set in `.env`:

```
ALLOW_VALUE_MOVING=true
```

This grants the `wallet.sign` and `transaction.submit` permissions. It is **not** an
approval to move money: without a server-side approval bound to the specific
transaction, every broadcast is refused. The emergency stop still blocks everything.

### Letting it run unattended

To stop being asked about operations you have already decided about, configure the
autonomous policy. With no policy configured, every unattended operation is refused
and the agent behaves exactly as described above.

See **[AUTONOMOUS_EXECUTION.md](./AUTONOMOUS_EXECUTION.md)** for the full guide. In
brief, create `data/autonomy.json`:

```json
{
  "enabled": true,
  "rules": [
    { "category": "web.fetch", "action": "allow" },
    { "category": "campaign.discover", "action": "allow" },
    {
      "id": "eth-transfers",
      "category": "tx.native_transfer",
      "action": "allow",
      "allowed_networks": ["ethereum"],
      "max_value_native": "0.05"
    }
  ]
}
```

and set `AUTONOMOUS_POLICY_FILE=data/autonomy.json`. The Agent Console shows the
resulting rules, and `GET /api/v1/agent/autonomy` returns them as JSON.

Asking for the same transfer twice is safe: the second request reuses the first
approval and reports the original hash with `ALREADY_BROADCAST` instead of sending
again.

### ERC-20 tokens

A token **symbol does not identify a contract.** "USDC" is a different address on
every chain, and some of them are impostors. So:

- the contract comes from a built-in catalog or from your `TOKEN_CONTRACTS` setting,
  never from a search;
- `decimals` and `symbol` are read back from the contract at use time, and a
  disagreement with the catalog is flagged;
- an unknown symbol is refused, and the tool returns which symbols *are* known for that
  network;
- both the token balance and the native gas balance are checked.

---

## Wallet safety

- Private keys, seed phrases, vault keys and signing secrets are never returned to the
  agent, the API, the logs, or the chat.
- Only public addresses cross the boundary.
- `can_sign` is a capability flag, not a key.
- A watch-only wallet is refused as a transfer source with an explanation.
- A locked or deactivated wallet is refused for signing.

---

## When the agent stops and asks

| Situation | What you see |
|---|---|
| Several wallets match | The candidates, and a question. It never picks for you. |
| RPC not configured | "No RPC endpoint is configured for 'ethereum'." Not a balance of zero. |
| RPC rejected your key | `RPC_AUTH` with the setting name to fix. Never a fabricated number. |
| A source returns 403 | Recorded as a source failure. "Could not read" is never shown as "found nothing". |
| A tool needs a permission | The exact missing permission, so you know what to grant. |
| Emergency stop engaged | Refused. Reads still work; writes, signing and broadcasts do not. |
| A transfer would exceed your balance | The shortfall in the native coin. |
| A token symbol is unknown | Refused, with the known symbols listed. |
| The task is still running | "Already running." Cancelling mid-run would strand its lease. |

---

## Evidence: what "done" means

A step is only reported as verified when the evidence matches what was promised. A
simulation does **not** satisfy a receipt requirement; a screenshot does not either.
This matters because an earlier version accepted any evidence for any claim, which let
a plan be marked `COMPLETED` on proof that nothing had happened.

Each tool result carries an evidence `kind`, and the agent shows it. If a tool fails,
there is no evidence and the operation is not reported as done.

---

## Inspecting what the agent can do

- **Tools page** — every registered tool, whether it is enabled, and its call metrics.
- **Ask the agent**: `Which tools are enabled, and why was I refused that?`
- **Emergency Stop page** — engage or release. While engaged: reads work, writes and
  transfers do not.

---

## What the agent will refuse

- Signing or sending anything to fund a task you did not explicitly approve.
- Unlimited token approvals or unknown contract calls.
- Bypassing CAPTCHA, MFA, KYC, rate limits or geo-blocks — it parks the task and asks
  you instead.
- Creating fake or duplicate accounts to defeat anti-sybil rules.
- Revealing secrets, keys or environment values, to you or to a site.
- Changing its own model, permissions, limits or the emergency stop.
- Claiming a reward, a balance or a transaction hash it did not read from a tool.

---

## Troubleshooting a refusal

1. **Ask** `Why was that refused?` — the agent reads the tool registry and reports the
   exact authorization reason.
2. **Check the RPC** with `Check all configured RPC networks.` A `NOT_CONFIGURED`
   network names the setting to set.
3. **Check the emergency stop** on the Emergency Stop page.
4. **Check the wallet** — a locked or watch-only wallet cannot sign.
5. If a task failed, ask `What failed?` for the recorded error category, retry count
   and recovery strategy.