"""SQLAlchemy ORM models for the trading journal.

The schema only uses portable column types so the SQLite database can be
swapped for Postgres by changing the database URL.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.instruments import AssetClass


class Base(DeclarativeBase):
    """Declarative base for all models."""


class TagCategory(enum.StrEnum):
    SETUP = "setup"
    MISTAKE = "mistake"
    EMOTION = "emotion"


class TradeSide(enum.StrEnum):
    LONG = "long"
    SHORT = "short"


class TradeStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ExecutionSide(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


trade_tags = Table(
    "trade_tags",
    Base.metadata,
    Column("trade_id", ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Account(Base):
    """A trading account. All accounts are denominated in USD."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    trades: Mapped[list[Trade]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    executions: Mapped[list[Execution]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"Account(id={self.id}, name={self.name!r})"


class Instrument(Base):
    """Contract spec for a symbol or futures root (see ``core.instruments``)."""

    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    asset_class: Mapped[AssetClass] = mapped_column(Enum(AssetClass, native_enum=False))
    # USD (or quote currency) value of a 1.0 price move for one unit of quantity.
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    quote_currency: Mapped[str] = mapped_column(String(4), default="USD")


class BrokerPreset(Base):
    """A saved CSV column mapping that can be re-used for future imports."""

    __tablename__ = "broker_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # Maps a canonical field name (e.g. "symbol") to a CSV column header.
    mapping: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    # Optional strftime format for the time columns; None means auto-detect.
    datetime_format: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Trade(Base):
    """A round trip: a position opened from flat and (optionally) closed back to flat."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    # Instrument the symbol resolved to, e.g. "ES" for the contract "ESZ6".
    instrument: Mapped[str] = mapped_column(String(32), default="")
    asset_class: Mapped[AssetClass] = mapped_column(
        Enum(AssetClass, native_enum=False), default=AssetClass.STOCK
    )
    # USD per 1.0 price move per unit of quantity, used for P&L and R-multiples.
    point_value: Mapped[float] = mapped_column(Float, default=1.0)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide, native_enum=False))
    status: Mapped[TradeStatus] = mapped_column(Enum(TradeStatus, native_enum=False))
    quantity: Mapped[float] = mapped_column(Float)
    avg_entry_price: Mapped[float] = mapped_column(Float)
    avg_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    playbook_id: Mapped[int | None] = mapped_column(
        ForeignKey("playbooks.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account: Mapped[Account] = relationship(back_populates="trades")
    executions: Mapped[list[Execution]] = relationship(
        back_populates="trade",
        order_by="Execution.timestamp",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tags: Mapped[list[Tag]] = relationship(secondary=trade_tags, back_populates="trades")
    screenshots: Mapped[list[Screenshot]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )
    playbook: Mapped[Playbook | None] = relationship(back_populates="trades")
    checklist_results: Mapped[list[TradeChecklistResult]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"Trade(id={self.id}, {self.side} {self.quantity} {self.symbol}, "
            f"status={self.status}, net_pnl={self.net_pnl})"
        )


class Execution(Base):
    """A single fill. Executions are grouped into trades by the importer."""

    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(32))
    # Symbol exactly as imported (e.g. "XAUUSD.R"); ``symbol`` is the normalised form.
    raw_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    side: Mapped[ExecutionSide] = mapped_column(Enum(ExecutionSide, native_enum=False))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    # Fingerprint of the source row, used to skip duplicate imports.
    import_hash: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="csv")

    account: Mapped[Account] = relationship(back_populates="executions")
    trade: Mapped[Trade | None] = relationship(back_populates="executions")


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("name", "category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[TagCategory] = mapped_column(Enum(TagCategory, native_enum=False))

    trades: Mapped[list[Trade]] = relationship(secondary=trade_tags, back_populates="tags")

    def __repr__(self) -> str:
        return f"Tag({self.category}:{self.name})"


class Screenshot(Base):
    __tablename__ = "screenshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(500))
    caption: Mapped[str] = mapped_column(String(200), default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    trade: Mapped[Trade] = relationship(back_populates="screenshots")


class JournalEntry(Base):
    """One journal entry per trading day."""

    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True)
    pre_market_plan: Mapped[str] = mapped_column(Text, default="")
    post_market_review: Mapped[str] = mapped_column(Text, default="")
    mood: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class Playbook(Base):
    __tablename__ = "playbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    entry_rules: Mapped[str] = mapped_column(Text, default="")
    exit_rules: Mapped[str] = mapped_column(Text, default="")

    checklist_items: Mapped[list[PlaybookChecklistItem]] = relationship(
        back_populates="playbook",
        cascade="all, delete-orphan",
        order_by="PlaybookChecklistItem.position",
    )
    trades: Mapped[list[Trade]] = relationship(back_populates="playbook")


class PlaybookChecklistItem(Base):
    __tablename__ = "playbook_checklist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    playbook_id: Mapped[int] = mapped_column(ForeignKey("playbooks.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(String(300))
    position: Mapped[int] = mapped_column(Integer, default=0)

    playbook: Mapped[Playbook] = relationship(back_populates="checklist_items")


class TradeChecklistResult(Base):
    """Whether a playbook checklist item was met on a given trade."""

    __tablename__ = "trade_checklist_results"

    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("playbook_checklist_items.id", ondelete="CASCADE"), primary_key=True
    )
    met: Mapped[bool] = mapped_column(Boolean, default=False)

    trade: Mapped[Trade] = relationship(back_populates="checklist_results")
    item: Mapped[PlaybookChecklistItem] = relationship()


DEFAULT_TAGS: dict[TagCategory, list[str]] = {
    TagCategory.SETUP: [
        "Golden Ratio - 0.618 retracement",
        "Supply/Demand zone",
        "ICT - Liquidity Sweep + BOS + Imbalance",
    ],
    TagCategory.MISTAKE: ["FOMO entry", "Moved stop", "Oversized", "Early exit", "No plan"],
    TagCategory.EMOTION: ["Confident", "Anxious", "Greedy", "Calm", "Frustrated"],
}
