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