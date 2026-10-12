"""Transaction Engine Database Models"""

from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid

from sqlalchemy import String, Float, DateTime, ForeignKey, Text, Boolean, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class BlockchainTransaction(Base):
    """On-chain transaction record with verification and receipt tracking."""

    __tablename__ = "blockchain_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False
    )
    network: Mapped[str] = mapped_column(String(100), nullable=False)
    chain_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    to_address: Mapped[str] = mapped_column(String(128), nullable=False)
    from_address: Mapped[str] = mapped_column(String(128), nullable=False)

    value_wei: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)  # hex
    gas_limit: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)
    gas_price_wei: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)

    data: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)  # hex calldata
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128), unique=True, index=True, nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)  # PENDING, SUBMITTED, CONFIRMED, FAILED, EXPIRED
    confirmation_count: Mapped[int] = mapped_column(default=0, nullable=False)
    required_confirmations: Mapped[int] = mapped_column(default=12, nullable=False)

    max_fee_limit_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    actual_gas_cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    usd_value_transacted: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Relationships
    task: Mapped[Optional["Task"]] = relationship("Task", back_populates="transactions")
    wallet: Mapped["Wallet"] = relationship("Wallet", back_populates="transactions")

    receipts: Mapped[List["TransactionReceipt"]] = relationship(
        "TransactionReceipt", back_populates="transaction", cascade="all, delete-orphan"
    )

    # Evidence chain
    evidence: Mapped[List["TaskEvidence"]] = relationship(
        "TaskEvidence", back_populates="blockchain_transaction"
    )


class TransactionApproval(Base):
    """A human decision to sign and broadcast exactly one specific transaction.

    `TransactionEngine.submit_raw` refuses to broadcast without a server-side proof
    of approval, and deliberately refuses to accept a caller-supplied "yes I approved
    this" flag. This table is that proof: a row is written when a named human
    approves a *specific, fully specified* transfer, it expires, it can be consumed
    exactly once, and `ApprovalVerifier` checks all of that server-side.

    `binding` pins the approval to one transaction: network, chain id, source
    wallet, destination, value and the hash of the exact signed payload. An approval
    for one transfer therefore cannot authorise another, and cannot be replayed.
    """

    __tablename__ = "transaction_approvals"

    # The primary key is declared here rather than inherited: `Base` supplies a
    # `default=uuid.uuid4`, but redeclaring `id` in a subclass replaces that default
    # with a bare column, so every INSERT sent NULL and the insert failed on NOT NULL.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True
    )
    # Short, opaque, operator-facing identifier used as `approval_id` in a tool call.
    approval_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Never a machine identity. `TransactionEngine.NON_HUMAN_APPROVERS` refuses these.
    approved_by: Mapped[str] = mapped_column(String(128), index=True, nullable=False)

    # The transaction this approval is good for.
    network: Mapped[str] = mapped_column(String(50), nullable=False)
    chain_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    wallet_address: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    to_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    value_wei: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    estimated_cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Canonical comparison target. `verify_and_consume` recomputes this from the raw
    # transaction and requires an exact match.
    binding: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    # Provenance of the request being approved.
    proposal_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    # Deterministic digest of the transfer intent. A repeated request for the same
    # intent reuses the existing approval instead of creating a second one, which is
    # what stops a retried command from broadcasting twice.
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    # Which configured autonomous rule authorized this, when the policy did rather
    # than a person. NULL for a human approval. Recorded so "why did this send money
    # unattended" has a precise, checkable answer.
    policy_rule_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    # Recorded the moment the node accepts the broadcast, before any receipt wait, so
    # a crash between the two still leaves the hash on disk.
    broadcast_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # PENDING -> CONSUMED (broadcast) | REJECTED (operator said no) | EXPIRED
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TransactionReceipt(Base):
    """Blockchain transaction receipt with gas usage, logs, and status."""

    __tablename__ = "transaction_receipts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blockchain_transactions.id"), nullable=False
    )
    block_number: Mapped[Optional[int]] = mapped_column(index=True, nullable=True)
    gas_used: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)
    gas_price_wei: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)
    cumulative_gas_used: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)
    effective_gas_price: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)

    status: Mapped[str] = mapped_column(String(50), nullable=False)  # SUCCESS, FAILED, REVERTED
    logs: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    logs_bloom: Mapped[Optional[str]] = mapped_column(String(16384), nullable=True)

    root: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)  # Merkle root
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    contract_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Relationships
    transaction: Mapped["BlockchainTransaction"] = relationship("BlockchainTransaction", back_populates="receipts")