# Blockchain & Transactions

## Supported Networks

EVM-compatible chains are prioritized. Configure per-network RPC endpoints:

```env
RPC_ETHEREUM_MAINNET=https://...
RPC_ARBITRUM_ONE=https://...
RPC_OPTIMISM=https://...
RPC_BASE=https://...
RPC_POLYGON=https://...
RPC_BSC=https://...
RPC_SEPOLIA=https://...
```

`chain_id` is allowlisted in `wallets/vault.py`. Unnamed chains are refused.

## Transaction Flow

1. **Construct** a `TransactionIntent` (network, chain, wallet, destination, value, calldata).
2. **Validate** in `TransactionEngine.validate_intent()` — address format, value, calldata size,
   denylist. Deterministic; does not trust the LLM.
3. **Estimate gas** — `eth_estimateGas` and `eth_gasPrice`, with a category-based fallback when
   estimation reverts.
4. **Simulate** — `eth_call` predicts whether the transaction would revert.
5. **Check limits** — per-transaction and rolling daily ceilings.
6. **Sign** in the vault's ephemeral scope after independent re-validation.
7. **Submit** via `eth_sendRawTransaction`, recording the hash.
8. **Track** the receipt and confirmation count.

## Nonce Coordination

`NonceManager` holds per-`(network, address)` locks and reserves in-flight nonces, so two
concurrent sends from the same wallet cannot collide. `get_nonce()` returns
`max(chain_pending, reserved + 1)`.

## Status Semantics

| Status | Meaning |
|---|---|
| `PENDING` | Broadcast, not yet mined. **Not success.** |
| `CONFIRMED` | Mined with status 1. Still not proof a reward arrived. |
| `REVERTED` | Mined with status 0. |
| `FAILED` | Rejected before broadcast. |

Confirmations are tracked against a per-chain target (12 blocks by default). Different chains
have different finality rules; the adapter surface allows per-chain targets.

## Cost Accounting

Gas costs are tracked per transaction and rolled up per task and per wallet. USD cost is only
recorded when an external price is supplied — the system never invents a token price.

## Simulation

`simulate_transaction` runs `eth_call` and returns `will_revert: True` when the call reverts.
A reverting simulation is an informative answer, not a tool failure.

## Not Performed

No real transactions are executed during development. Signing and submission are disabled by
default in `main.py` (`allow_transaction_submission=False`).

## Contract Verification

A verified contract address does not guarantee safety. Verification is not treated as an
authorization to transact.
