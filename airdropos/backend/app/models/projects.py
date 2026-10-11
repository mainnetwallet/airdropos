"""Projects Database Models"""

from typing import Optional, List, Dict, Any
from sqlalchemy import String, Float, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class Project(Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    official_website: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    official_docs: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    twitter_handle: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    discord_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    primary_network: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)  # DeFi, L2, Bridge, NFT, Infra
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE", index=True, nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # 0 to 10
    metadata_info: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="project")
    campaigns: Mapped[List["AirdropCampaign"]] = relationship("AirdropCampaign", back_populates="project")
