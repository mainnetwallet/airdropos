# Wallet & Key Security

## Threat Model

The primary risks are: key exposure to the LLM, prompt-injected signing requests, unbounded
spending, and unverified transaction outcomes. Each is addressed by a deterministic control,
not a prompt instruction.

## Encryption at Rest

Private keys and seed phrases are encrypted with **AES-256-GCM**:

```
master key (WALLET_VAULT_MASTER_KEY, 64 hex chars / 256-bit)
    └── encrypts wallet secrets stored as (ciphertext, nonce, tag)
```

The master key lives in configuration, separate from the encrypted data. A unique nonce (12
bytes, `os.urandom`) is generated per encryption, so identical secrets never produce identical
ciphertext.

Generate a production key with:

```bash
openssl rand -hex 32
```

Never commit it. `KeyVault.fingerprint()` returns a non-reversible identifier safe for
diagnostics.

## Ephemeral Key Scope

`_with_private_key()` is a context manager yielding the private key only inside its scope:

```python
with self._with_private_key(enc, iv, tag, label) as private_key:
    signed = account.sign_message(...)
# private_key reference scrubbed here
```

The key is never stored on a long-lived instance, never logged, never returned to the caller,
and never serialized into an API response.

## The LLM Never Sees Keys

The LLM may formulate a `SigningRequest`:

```python
@dataclass(frozen=True)
class SigningRequest:
    wallet_address: str
    chain_id: int
    message: Optional[str] = None
    transaction: Optional[Dict[str, object]] = None
```

It contains **no secret material**. The vault independently validates:

1. Emergency stop is not active.
2. `chain_id` is in `SUPPORTED_CHAIN_IDS` (no unnamed chains).
3. The wallet is in the authorized address set.
4. Calldata is under 200 KB.
5. The method selector is not forbidden.

## Forbidden Selectors

The vault refuses to sign these regardless of who requests them:

| Selector | Function | Why |
|---|---|---|
| `0x8bda01ab` | `permit(...)` | Unilateral token approval |
| `0xf2fde38b` | `transferOwnership(address)` | Ownership transfer |
| `0x715018a6` | `renounceOwnership()` | Ownership surrender |
| `0xffc9a479` | unknown selector | Unrecognized, refused by default |

This means a fully prompt-injected agent cannot drain a wallet by signing a `permit`.

## Address Mismatch Guard

After decryption, the vault derives the signer address from the key and compares it to
`request.wallet_address`. A mismatch raises `SigningDenied` — so a request naming one wallet
cannot be signed by another.

## Spending Limits

Enforced in `transactions/engine.py`, not in prompts:

| Limit | Variable | Default |
|---|---|---|
| Per transaction | `MAX_TRANSACTION_VALUE_USD` | unset (no ceiling) |
| Rolling 24h | `MAX_DAILY_GAS_SPEND_USD` | unset (no ceiling) |

**There is no fixed application-level transaction amount cap and no daily
transaction-count cap.** Both ceilings above are optional operator settings: leave them
blank (or `none` / `unlimited`) and the platform imposes no dollar ceiling at all, so a
legitimate large autonomous transfer is never refused for being large. Set a number and it
is enforced atomically across processes by the spend ledger. Spend is recorded either way,
so a ceiling can be turned on later without losing history.

What the agent is *allowed* to send is governed by the autonomous policy
(`AUTONOMOUS_POLICY`): networks, source wallets, destinations, tokens and per-rule
amounts. The Emergency Stop overrides all of it.

`check_spend_limits()` rejects before signing when a ceiling is configured.
`record_spend()` accumulates actual cost.

`REQUIRE_CONFIRMATION_ABOVE_USD` is declared in settings but is not read anywhere: there
is no code path that consults it. Approval is transaction-bound rather than amount-based -
see [AUTONOMOUS_EXECUTION.md](AUTONOMOUS_EXECUTION.md).

**Token prices are never invented.** `estimate_gas()` returns native-denominated cost plus a
note that USD requires an external price source.

## Denylists

`DENYLIST_CATEGORIES` blocks destinations before any value is sent. Categories currently
include the zero address and a sanctioned-address set (empty by default, populated per
deployment).

## Emergency Stop

`EmergencyStopController` halts signing, submission, task execution, and scheduled jobs. Layers:

1. Config default (`EMERGENCY_STOP_ENABLED`)
2. Runtime controller state
3. Guard inside `ToolRegistry.invoke()` before any side-effecting tool
4. Guard inside `KeyVault.validate_request()`
5. Guard inside `TransactionEngine.validate_intent()` and `submit_raw()`

Stopping does **not** cancel transactions already broadcast to a network.

## Recovery

Re-importing a wallet re-encrypts the secret under the current master key. If the master key is
lost, encrypted secrets are unrecoverable — there is no escrow and no backdoor by design.
Back up `WALLET_VAULT_MASTER_KEY` separately from database backups.

## Never Do This

- Never paste a seed phrase or private key into chat, Telegram, or a support channel.
- Never store secrets in `.env` inside the repository.
- Never log decrypted material, even at DEBUG level.
- Never let the LLM read `encrypted_secret`, `encryption_iv`, or `encryption_tag` columns.
