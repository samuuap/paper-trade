"""Database schema.

Tables marked *hypertable* are converted by the Timescale migration; their
primary key includes the time column because Timescale requires the
partitioning column to be part of every unique index.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Money, Quantity

#: Native uuid/jsonb on Postgres, portable fallbacks elsewhere. The fallback
#: exists so the full stack (engine, orchestrator, replay) can be exercised
#: against SQLite in tests without standing up a database.
UUIDType = Uuid(as_uuid=True)
JSONType = JSON().with_variant(JSONB, "postgresql")


def _uuid_col(primary_key: bool = False, **kw: Any) -> Mapped[uuid.UUID]:
    return mapped_column(UUIDType, primary_key=primary_key, default=uuid.uuid4, **kw)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- enums


class Phase(str, enum.Enum):
    PRE_MARKET = "PRE_MARKET"
    OPEN = "OPEN"
    AFTER_HOURS = "AFTER_HOURS"
    CLOSED = "CLOSED"


class AgentKind(str, enum.Enum):
    LLM = "LLM"
    BASELINE = "BASELINE"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class DecisionKind(str, enum.Enum):
    TRADE = "TRADE"          # OPEN: the 15-minute trading cycle
    PLAN = "PLAN"            # PRE_MARKET: watchlist and day plan
    REVIEW = "REVIEW"        # AFTER_HOURS: attribution and journal
    RESEARCH = "RESEARCH"    # CLOSED: deep research / strategy reflection


class DecisionStatus(str, enum.Enum):
    OK = "OK"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


def _enum(py_enum: type[enum.Enum], name: str) -> Enum:
    # native_enum=False keeps these as VARCHAR + CHECK, which makes adding a
    # value a plain migration instead of an ALTER TYPE dance.
    return Enum(py_enum, name=name, native_enum=False, length=24, validate_strings=True)


# -------------------------------------------------------------------- agents


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[AgentKind] = mapped_column(_enum(AgentKind, "agent_kind"))
    #: Which runtime drives this agent (`llm`, `buy_and_hold`, `quant_sma`).
    runtime: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text, default="")
    #: The strategy prompt is the *only* variable between LLM agents.
    strategy_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    config: Mapped[dict] = mapped_column(JSONType, default=dict)
    color: Mapped[str] = mapped_column(String(16), default="#7c8ff5")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    portfolio: Mapped[Optional["Portfolio"]] = relationship(
        back_populates="agent", uselist=False, cascade="all, delete-orphan"
    )


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), unique=True, index=True
    )
    cash: Mapped[Decimal] = mapped_column(Money)
    initial_capital: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    fees_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    agent: Mapped[Agent] = relationship(back_populates="portfolio")
    positions: Mapped[list["Position"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint("initial_capital > 0", name="initial_capital_positive"),)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    avg_entry_price: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="positions")

    __table_args__ = (UniqueConstraint("portfolio_id", "symbol", name="uq_positions_portfolio_symbol"),)


# ----------------------------------------------------------------- decisions


class Decision(Base):
    """One agent turn. The instrumentation record of the whole system."""

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUIDType, index=True)
    phase: Mapped[Phase] = mapped_column(_enum(Phase, "phase"))
    kind: Mapped[DecisionKind] = mapped_column(_enum(DecisionKind, "decision_kind"))
    status: Mapped[DecisionStatus] = mapped_column(
        _enum(DecisionStatus, "decision_status"), default=DecisionStatus.OK
    )

    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    inference_ms: Mapped[float] = mapped_column(Float, default=0.0)
    queue_wait_ms: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_per_second: Mapped[float] = mapped_column(Float, default=0.0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    #: What the same call would have cost on the frontier API.
    shadow_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    #: Local inference is not free either — just paid in watt-hours.
    energy_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    reasoning: Mapped[str] = mapped_column(Text, default="")
    market_view: Mapped[str] = mapped_column(Text, default="")
    raw_response: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list] = mapped_column(JSONType, default=list)
    #: Parsed structured output, kept verbatim for auditing.
    output: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="decision")

    __table_args__ = (
        Index("ix_decisions_agent_created", "agent_id", "created_at"),
    )


# -------------------------------------------------------------------- orders


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "order_side"))
    order_type: Mapped[OrderType] = mapped_column(
        _enum(OrderType, "order_type"), default=OrderType.MARKET
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    limit_price: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus, "order_status"), default=OrderStatus.PENDING
    )
    filled_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"))
    avg_fill_price: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    #: Why the agent wanted this trade, in its own words.
    rationale: Mapped[str] = mapped_column(Text, default="")
    conviction: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Snapshot of the deterministic sizing/risk verdict for this order.
    risk_notes: Mapped[dict] = mapped_column(JSONType, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    decision: Mapped[Optional[Decision]] = relationship(back_populates="orders")
    fills: Mapped[list["Fill"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_orders_agent_created", "agent_id", "created_at"),
    )


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "fill_side"))
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    price: Mapped[Decimal] = mapped_column(Money)
    #: The quote that justified this fill — every fill is traceable to one.
    reference_price: Mapped[Decimal] = mapped_column(Money)
    reference_quote_ts: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    slippage_bps: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    commission: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    gross_amount: Mapped[Decimal] = mapped_column(Money)
    #: Cash delta including fees: negative on buys, positive on sells.
    cash_delta: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    order: Mapped[Order] = relationship(back_populates="fills")


# -------------------------------------------------------- market data (TSDB)


class MarketBar(Base):
    """OHLCV for the universe. *hypertable* on `ts`."""

    __tablename__ = "market_bars"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True, default="1D")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(24), default="finnhub")

    __table_args__ = (Index("ix_market_bars_symbol_ts", "symbol", "ts"),)


class MarketQuote(Base):
    """Real-time quotes for the active set. *hypertable* on `ts`."""

    __tablename__ = "market_quotes"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    price: Mapped[Decimal] = mapped_column(Money)
    prev_close: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    day_open: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    day_high: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    day_low: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    volume: Mapped[Optional[Decimal]] = mapped_column(Quantity, nullable=True)
    source: Mapped[str] = mapped_column(String(24), default="finnhub")

    __table_args__ = (Index("ix_market_quotes_symbol_ts", "symbol", "ts"),)


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("symbol", "external_id", name="uq_news_items_symbol_external_id"),
    )


class UniverseMember(Base):
    """The tradable universe plus the cached screening signals for each name."""

    __tablename__ = "universe_members"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    sector: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Indicators recomputed after every daily-bar ingest.
    signals: Mapped[dict] = mapped_column(JSONType, default=dict)
    signals_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ------------------------------------------------------------------ metrics


class EquitySnapshot(Base):
    """Per-agent equity curve. *hypertable* on `ts`."""

    __tablename__ = "equity_snapshots"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    equity: Mapped[Decimal] = mapped_column(Money)
    cash: Mapped[Decimal] = mapped_column(Money)
    positions_value: Mapped[Decimal] = mapped_column(Money)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    gross_exposure: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    net_exposure: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    position_count: Mapped[int] = mapped_column(Integer, default=0)
    phase: Mapped[Optional[Phase]] = mapped_column(_enum(Phase, "snapshot_phase"), nullable=True)

    __table_args__ = (Index("ix_equity_snapshots_agent_ts", "agent_id", "ts"),)


class MetricsDaily(Base):
    __tablename__ = "metrics_daily"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    equity_open: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    equity_close: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    daily_return: Mapped[float] = mapped_column(Float, default=0.0)
    cumulative_return: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    sharpe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sortino: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    calmar: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volatility: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_days: Mapped[int] = mapped_column(Integer, default=0)
    current_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    var_95: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expectancy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    turnover: Mapped[float] = mapped_column(Float, default=0.0)
    gross_exposure_pct: Mapped[float] = mapped_column(Float, default=0.0)
    net_exposure_pct: Mapped[float] = mapped_column(Float, default=0.0)
    concentration_hhi: Mapped[float] = mapped_column(Float, default=0.0)

    alpha: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    beta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    benchmark_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    decisions: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    inference_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    shadow_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    energy_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    extras: Mapped[dict] = mapped_column(JSONType, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True, server_default=func.now()
    )
    level: Mapped[str] = mapped_column(String(16), default="info")
    category: Mapped[str] = mapped_column(String(48), index=True)
    event: Mapped[str] = mapped_column(String(96))
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)


class JobRun(Base):
    """Every scheduled job records a row; powers the 'job success' health metric."""

    __tablename__ = "job_runs"

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    job: Mapped[str] = mapped_column(String(64), index=True)
    phase: Mapped[Optional[Phase]] = mapped_column(_enum(Phase, "job_phase"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)


HYPERTABLES = {
    "equity_snapshots": "ts",
    "market_bars": "ts",
    "market_quotes": "ts",
}
