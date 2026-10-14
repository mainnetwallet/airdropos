"""Durable record of a transaction from signing to settlement.

The approval table records *intent*: a transaction was authorized. That is not the same
as having been sent, and it is not the same as having succeeded. Before this module the
broadcast path persisted only a hash string on the approval row, so a transaction that
was signed but never sent, or sent but reverted, was indistinguishable from one that
confirmed - and a restart could not reconcile anything.

This writes to the existing `blockchain_transactions` / `transaction_receipts` tables.
The states it distinguishes are:

    SIGNED     bytes exist locally, nothing has been sent
    SUBMITTED  the node accepted the broadcast; that is not proof of success
    PENDING    submitted, no receipt yet
    CONFIRMED  a receipt exists with status 1
    REVERTED   a receipt exists with status 0 - mined, but the transfer did not happen
    FAILED     the transaction can never be settled as submitted

Two rules this module exists to enforce:

* A pending transaction is never recorded as confirmed, and a reverted one is never
  recorded as successful. The receipt is the only thing that settles a status.
* A transaction that has already been sent is found by *intent*, not by remembering to
  look, so a duplicated task cannot send the same transfer twice.

Note on ordering: `submit_raw` consumes the approval before it sends, so a crash between
those two points loses the ability to broadcast that approval. That is deliberate - it is
the only direction that cannot double-spend. The operator re-approves; the system never
silently re-sends.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import BlockchainTransaction, TransactionReceipt
from backend.app.transactions.engine import canonical_calldata

logger = logging.getLogger(__name__)

STATUS_SIGNED = "SIGNED"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_PENDING = "PENDING"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_REVERTED = "REVERTED"
STATUS_FAILED = "FAILED"

#: States a transaction can never leave without a receipt or an explicit failure.
TERMINAL = frozenset({STATUS_CONFIRMED, STATUS_REVERTED, STATUS_FAILED})

#: States that mean "bytes exist or reached a node"; reconcile may still settle them.
IN_FLIGHT = frozenset({STATUS_SIGNED, STATUS_SUBMITTED, STATUS_PENDING})

# `blockchain_transactions.data` is a 2048-character column. Calldata longer than that is
# truncated for the record only: every integrity check reads the decoded bytes, never this
# column, so nothing is authorized or rejected on the strength of a truncated value.
MAX_STORED_CALLDATA = 2048


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(value: Optional[str], limit: int = MAX_STORED_CALLDATA) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def _intent_fields(
    network: str,
    from_address: str,
    to_address: str,
    value_wei: int,
    data: str,
) -> Dict[str, Any]:
    return {
        "network": str(network).strip().lower(),
        "from_address": str(from_address).strip().lower(),
        "to_address": str(to_address).strip().lower(),
        "value_wei": str(int(value_wei)),
        "data": canonical_calldata(data),
    }


async def record_signed(
    db: AsyncSession,
    *,
    wallet_id: Any,
    network: str,
    chain_id: Optional[int],
    from_address: str,
    to_address: str,
    value_wei: int,
    data: str,
    gas_limit: int,
    task_id: Optional[Any] = None,
) -> BlockchainTransaction:
    """Record that a transaction was signed locally. Nothing has been sent yet."""
    row = BlockchainTransaction(
        wallet_id=wallet_id,
        task_id=task_id,
        network=str(network),
        chain_id=int(chain_id) if chain_id is not None else None,
        from_address=str(from_address).strip().lower(),
        to_address=str(to_address).strip().lower(),
        value_wei=str(int(value_wei)),
        data=_clip(canonical_calldata(data)),
        gas_limit=str(int(gas_limit)),
        status=STATUS_SIGNED,
    )
    db.add(row)
    await db.flush()
    logger.info("Transaction signed but not sent (id=%s, %s)", row.id, row.network)
    return row


async def record_submitted(db: AsyncSession, row: BlockchainTransaction, tx_hash: str) -> None:
    """Record the hash the node returned.

    Written immediately after the node accepts the broadcast and before any receipt
    wait, so a crash during the wait still leaves the hash on disk and a retry reports
    what happened instead of sending a second time.
    """
    existing = await db.scalar(
        select(BlockchainTransaction).where(BlockchainTransaction.tx_hash == tx_hash)
    )
    if existing is not None:
        # The hash column is unique. A concurrent worker recorded it first; that row is
        # the authoritative one and must not be duplicated or overwritten.
        if existing.id != row.id:
            logger.info("Broadcast hash %s already recorded on transaction %s", tx_hash, existing.id)
        return
    row.tx_hash = tx_hash
    row.status = STATUS_SUBMITTED
    row.submitted_at = _now()
    await db.flush()
    logger.info("Transaction submitted (id=%s, hash=%s)", row.id, tx_hash)


async def record_receipt(
    db: AsyncSession, row: BlockchainTransaction, receipt: Optional[Mapping[str, Any]]
) -> str:
    """Settle a submitted transaction from its receipt. Returns the new status.

    ``receipt is None`` means the transaction is still pending, which is a real answer:
    it is recorded as PENDING and never as CONFIRMED.
    """
    if receipt is None:
        if row.status != STATUS_PENDING:
            row.status = STATUS_PENDING
            await db.flush()
        return STATUS_PENDING

    outcome = str(receipt.get("status") or "").upper()
    if outcome not in ("CONFIRMED", "REVERTED"):
        # An unrecognised receipt is not evidence of success.
        logger.warning("Receipt for %s reported unknown status %r; treating as pending.",
                       row.tx_hash, receipt.get("status"))
        if row.status != STATUS_PENDING:
            row.status = STATUS_PENDING
            await db.flush()
        return STATUS_PENDING

    db.add(
        TransactionReceipt(
            transaction_id=row.id,
            block_number=receipt.get("block_number"),
            gas_used=None if receipt.get("gas_used") is None else str(receipt["gas_used"]),
            effective_gas_price=(
                None
                if receipt.get("effective_gas_price") is None
                else str(receipt["effective_gas_price"])
            ),
            status="SUCCESS" if outcome == "CONFIRMED" else "REVERTED",
            logs=receipt.get("logs") or [],
            contract_address=receipt.get("contract_address"),
            timestamp=_now(),
        )
    )
    row.status = STATUS_CONFIRMED if outcome == "CONFIRMED" else STATUS_REVERTED
    row.confirmed_at = _now()
    confirmations = receipt.get("confirmations")
    row.confirmation_count = (
        int(confirmations) if isinstance(confirmations, int) and confirmations >= 0 else 1
    )
    if row.error_message is None and outcome == "REVERTED":
        row.error_message = "The transaction was mined but reverted on-chain."
        row.error_category = "TX_REVERTED"
    await db.flush()
    logger.info("Transaction %s settled as %s", row.tx_hash, row.status)
    return row.status


async def record_failure(
    db: AsyncSession, row: BlockchainTransaction, reason: str, category: str
) -> None:
    """Record a transaction that can never settle as submitted.

    Only valid while nothing was accepted by a node. A transaction that reached a node is
    reconciled from its receipt instead, because it may still confirm.
    """
    if row.status in TERMINAL or row.status == STATUS_SUBMITTED:
        logger.warning(
            "Refusing to mark %s as FAILED: it may still settle on-chain.", row.tx_hash or row.id
        )
        return
    row.status = STATUS_FAILED
    row.error_message = str(reason)[:2000]
    row.error_category = str(category)[:50]
    await db.flush()


async def find_in_flight(
    db: AsyncSession,
    *,
    network: str,
    from_address: str,
    to_address: str,
    value_wei: int,
    data: str,
) -> Optional[BlockchainTransaction]:
    """An earlier transaction of this exact intent, whatever state it reached.

    Used to answer "was this already sent?" from the transaction's own fields, so a
    duplicated task, a retried command or a second worker cannot send it again.
    """
    intent = _intent_fields(network, from_address, to_address, value_wei, data)
    rows = (
        await db.execute(
            select(BlockchainTransaction)
            .where(
                BlockchainTransaction.network == intent["network"],
                BlockchainTransaction.from_address == intent["from_address"],
                BlockchainTransaction.to_address == intent["to_address"],
                BlockchainTransaction.value_wei == intent["value_wei"],
            )
            .order_by(BlockchainTransaction.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    for row in rows:
        if canonical_calldata(row.data or "0x") == intent["data"]:
            return row
    return None


async def reconcile(db: AsyncSession, engine: Any, row: BlockchainTransaction) -> str:
    """Settle one recorded transaction from the chain, and persist what it says."""
    if not row.tx_hash or row.status in TERMINAL:
        return row.status
    try:
        receipt = await engine.get_receipt(row.network, row.tx_hash)
    except Exception as exc:
        # An unreachable node is not evidence that the transaction failed. It stays
        # in flight and is retried later.
        logger.warning("Could not reconcile %s: %s", row.tx_hash, exc)
        return row.status
    try:
        return await record_receipt(db, row, receipt)
    except Exception as exc:
        logger.error("Could not persist receipt for %s: %s", row.tx_hash, exc)
        raise


async def reconcile_in_flight(
    db: AsyncSession, engine: Any, *, limit: int = 50
) -> Dict[str, List[str]]:
    """Settle every transaction this deployment recorded but has not yet settled.

    This is the restart path: whatever was in flight when the process died is checked
    against the chain, so a pending transaction is never left being described as sent.
    """
    rows = (
        await db.execute(
            select(BlockchainTransaction)
            .where(BlockchainTransaction.status.in_(tuple(IN_FLIGHT)))
            .order_by(BlockchainTransaction.created_at.asc())
            .limit(max(1, min(limit, 500)))
        )
    ).scalars().all()
    settled: Dict[str, List[str]] = {STATUS_CONFIRMED: [], STATUS_REVERTED: [], STATUS_PENDING: []}
    for row in rows:
        if not row.tx_hash:
            continue
        try:
            status = await reconcile(db, engine, row)
        except Exception as exc:
            logger.error("Reconciliation of %s failed: %s", row.id, exc)
            continue
        settled.setdefault(status, []).append(str(row.tx_hash))
    return {k: v for k, v in settled.items() if v}