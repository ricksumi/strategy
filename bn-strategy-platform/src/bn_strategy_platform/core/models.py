"""Exchange-independent trading domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def entry_order_side(self) -> str:
        return "BUY" if self is Side.LONG else "SELL"

    @property
    def exit_order_side(self) -> str:
        return "SELL" if self is Side.LONG else "BUY"


class RunMode(str, Enum):
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    close_time_ms: int
    quote_volume: Decimal = Decimal("0")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class MarketCandidate:
    symbol: str
    quote_volume_24h: Decimal
    range_24h: Decimal
    spread: Decimal
    return_1h: Decimal
    return_15m: Decimal
    volume_ratio_15m: Decimal
    score: Decimal


@dataclass(frozen=True)
class Signal:
    strategy: str
    strategy_version: str
    symbol: str
    side: Side
    signal_price: Decimal
    stop_price: Decimal
    score: Decimal
    reason: str
    observed_at_ms: int
    metrics: Mapping[str, Any] = field(default_factory=dict)
    requested_margin: Optional[Decimal] = None


@dataclass(frozen=True)
class StrategyEvent:
    event_id: str
    strategy: str
    kind: str
    symbol: str
    side: Side
    observed_at_ms: int
    signal: Optional[Signal] = None


@dataclass(frozen=True)
class TradePlan:
    signal: Signal
    risk_usdt: Decimal
    quantity: Decimal
    estimated_margin: Decimal
    leverage: int


@dataclass
class Position:
    trade_id: str
    strategy: str
    strategy_version: str
    symbol: str
    side: Side
    entry_price: Decimal
    quantity: Decimal
    remaining_quantity: Decimal
    margin: Decimal
    leverage: int
    stop_price: Decimal
    opened_at_ms: int
    best_price: Decimal
    worst_price: Optional[Decimal] = None
    initial_stop_price: Optional[Decimal] = None
    initial_entry_price: Optional[Decimal] = None
    initial_risk_distance: Optional[Decimal] = None
    scale_in_done: bool = False
    scale_in_quantity: Decimal = Decimal("0")
    scale_in_price: Optional[Decimal] = None
    realized_pnl: Decimal = Decimal("0")
    partial_tiers_done: Tuple[int, ...] = ()
    stop_order_id: Optional[str] = None
    stop_reason: str = "initial_stop"
    entry_score: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.worst_price is None:
            self.worst_price = self.entry_price
        if self.initial_entry_price is None:
            self.initial_entry_price = self.entry_price
        if self.initial_risk_distance is None and self.initial_stop_price is not None:
            self.initial_risk_distance = abs(
                self.initial_entry_price - self.initial_stop_price
            )


@dataclass(frozen=True)
class AccountSnapshot:
    equity_usdt: Decimal
    available_usdt: Decimal
    positions: Tuple[Position, ...] = ()


@dataclass(frozen=True)
class OrderFill:
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    average_price: Decimal
    status: str
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeSettlement:
    exit_price: Decimal
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal

    @property
    def net_pnl(self) -> Decimal:
        return self.realized_pnl - self.commission + self.funding


@dataclass(frozen=True)
class ClosedTrade:
    trade_key: str
    strategy: str
    strategy_version: str
    mode: str
    entry_context: str
    symbol: str
    side: Side
    opened_at_ms: int
    closed_at_ms: int
    entry_price: Decimal
    exit_price: Decimal
    initial_stop_price: Decimal
    last_stop_price: Decimal
    net_pnl: Decimal
    duration_seconds: int
    close_reason: str


@dataclass(frozen=True)
class PositionAction:
    kind: str
    reason: str
    quantity: Decimal = Decimal("0")
    stop_price: Optional[Decimal] = None
    tier: Optional[int] = None
    margin: Optional[Decimal] = None
