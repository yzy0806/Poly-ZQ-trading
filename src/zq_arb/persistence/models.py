from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False, index=True
    )


class ConfigVersion(Base, TimestampMixin):
    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config_version: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MarketMappingRecord(Base, TimestampMixin):
    __tablename__ = "market_mappings"
    __table_args__ = (UniqueConstraint("config_version", "market_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    market_code: Mapped[str] = mapped_column(String(64), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(80), nullable=False)
    yes_token_id: Mapped[str] = mapped_column(String(96), nullable=False)
    no_token_id: Mapped[str] = mapped_column(String(96), nullable=False)
    rule_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class QuoteRecord(Base, TimestampMixin):
    __tablename__ = "quotes"
    __table_args__ = (Index("ix_quotes_instrument_created", "instrument", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    bid_size: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    ask_size: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    last: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class OrderBookRecord(Base, TimestampMixin):
    __tablename__ = "order_books"
    __table_args__ = (Index("ix_order_books_token_created", "token_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)
    condition_id: Mapped[str | None] = mapped_column(String(80))
    book_hash: Mapped[str | None] = mapped_column(String(96))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    levels: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SignalRecord(Base, TimestampMixin):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    minimum_net_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    return_bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    tradeable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class BatchRecord(Base, TimestampMixin):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    zq_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class OrderRecord(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("venue", "venue_order_id"),
        UniqueConstraint("idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    venue_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    permanent_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ExecutionRecord(Base, TimestampMixin):
    __tablename__ = "executions"
    __table_args__ = (UniqueConstraint("venue", "execution_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_id: Mapped[str] = mapped_column(String(160), nullable=False)
    venue_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class HedgeObligationRecord(Base, TimestampMixin):
    __tablename__ = "hedge_obligations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    obligation_id: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    exec_id: Mapped[str] = mapped_column(String(160), nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)
    due_shares: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    confirmed_shares: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PositionRecord(Base, TimestampMixin):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_venue_instrument", "venue", "instrument"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    average_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MarginSnapshotRecord(Base, TimestampMixin):
    __tablename__ = "margin_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    current_values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    projected_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ReconciliationRecord(Base, TimestampMixin):
    __tablename__ = "reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reconciliation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    clean: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observed: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    differences: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class AlertRecord(Base, TimestampMixin):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AuditLogRecord(Base, TimestampMixin):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_actor_action", "actor", "action", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
