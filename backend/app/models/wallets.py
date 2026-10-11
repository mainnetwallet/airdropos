"""Wallet Management Database Models"""

from typing import TYPE_CHECKING, Optional, List, Dict, Any

from sqlalchemy import String, Boolean, Float, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.app.models.transactions import BlockchainTransaction
    from backend.app.models.airdrops import ClaimRecord, WalletEligibility


class Wallet(Base):
    __tablename__ = "wallets"

    label: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    chain_type: Mapped[str] = mapped_column(String(50), default="EVM", index=True, nullable=False)  # EVM, SOLANA
    derivation_path: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # AES-256-GCM encrypted private key/seed. NEVER returned via API.
    encrypted_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encryption_iv: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    encryption_tag: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    supported_networks: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    total_spent_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships. These mirror the `back_populates` declarations on the
    # referencing models; both sides must agree or mapper configuration fails.
    transactions: Mapped[List["BlockchainTransaction"]] = relationship(
        "BlockchainTransaction", back_populates="wallet"
    )
    claims: Mapped[List["ClaimRecord"]] = relationship(
        "ClaimRecord", back_populates="wallet"
    )
    eligibility: Mapped[List["WalletEligibility"]] = relationship(
        "WalletEligibility", back_populates="wallet"
    )
