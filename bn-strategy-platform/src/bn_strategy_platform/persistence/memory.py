"""In-memory store used by tests and local evaluation."""

from __future__ import annotations

import time
from decimal import Decimal

from ..core.models import ClosedTrade, Position, Signal, StrategyEvent


class MemoryTradeStore:
    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}
        self.position_modes: dict[str, str] = {}
        self.position_contexts: dict[str, str] = {}
        self.closed_position_modes: dict[str, str] = {}
        self.signals: list[tuple[Signal, str]] = []
        self.closed: list[tuple[Position, Decimal, Decimal, str]] = []
        self.events: dict[tuple[str, str, str], str] = {}
        self.fast_stop_candidates: list[ClosedTrade] = []
        self.closed_trades: list[ClosedTrade] = []
        self.open_metrics: dict[str, dict] = {}

    def load_open_positions(self, strategy: str, mode: str) -> list[Position]:
        return [item for item in self.positions.values()
                if item.strategy == strategy and self.position_modes.get(item.trade_id) == mode]

    def load_last_open_times(self, strategy: str, mode: str) -> dict[str, int]:
        result: dict[str, int] = {}
        positions = [item for item in self.positions.values()
                     if item.strategy == strategy and self.position_modes.get(item.trade_id) == mode]
        positions.extend(
            item for item, _, _, _ in self.closed
            if item.strategy == strategy and self.closed_position_modes.get(item.trade_id) == mode
        )
        for position in positions:
            result[position.symbol] = max(
                result.get(position.symbol, 0), position.opened_at_ms
            )
        return result

    def save_signal(self, signal: Signal, mode: str) -> None:
        self.signals.append((signal, mode))

    def save_open(self, position: Position, mode: str, score: Decimal, reason: str,
                  entry_context: str = "normal", metrics=None) -> None:
        self.positions[position.trade_id] = position
        self.position_modes[position.trade_id] = mode
        self.position_contexts[position.trade_id] = entry_context
        self.open_metrics[position.trade_id] = dict(metrics or {})

    def save_position(self, position: Position) -> None:
        self.positions[position.trade_id] = position

    def save_close(self, position: Position, exit_price: Decimal, net_pnl: Decimal, reason: str) -> None:
        mode = self.position_modes.get(position.trade_id, "")
        context = self.position_contexts.get(position.trade_id, "normal")
        closed_at_ms = int(time.time() * 1000)
        self.closed_position_modes[position.trade_id] = self.position_modes.get(
            position.trade_id, ""
        )
        self.positions.pop(position.trade_id, None)
        self.position_modes.pop(position.trade_id, None)
        self.position_contexts.pop(position.trade_id, None)
        self.closed.append((position, exit_price, net_pnl, reason))
        self.closed_trades.append(ClosedTrade(
            position.trade_id, position.strategy, position.strategy_version, mode, context,
            position.symbol, position.side, position.opened_at_ms, closed_at_ms,
            position.entry_price, exit_price,
            position.initial_stop_price or position.stop_price, position.stop_price,
            net_pnl, max(0, (closed_at_ms - position.opened_at_ms) // 1000), reason,
        ))

    def daily_net_pnl(self, strategy: str, mode: str, start_ms: int, end_ms: int) -> Decimal:
        return sum((pnl for pos, _, pnl, _ in self.closed
                    if (strategy == "*" or pos.strategy == strategy)
                    and self.closed_position_modes.get(pos.trade_id) == mode), Decimal("0"))

    def recent_fast_stop_candidates(
        self, source_strategies, start_ms: int, end_ms: int, max_duration_seconds: int,
    ) -> list[ClosedTrade]:
        return [item for item in self.fast_stop_candidates
                if item.strategy in source_strategies
                and start_ms <= item.closed_at_ms < end_ms
                and item.duration_seconds <= max_duration_seconds
                and item.net_pnl < 0
                and item.close_reason in {"stop_loss", "exchange_stop_reconciled"}
                and item.last_stop_price == item.initial_stop_price
                and ((item.side.value == "long"
                      and item.last_stop_price < item.entry_price
                      and item.exit_price <= item.last_stop_price)
                     or (item.side.value == "short"
                         and item.last_stop_price > item.entry_price
                         and item.exit_price >= item.last_stop_price))]

    def last_closed_trade(self, strategy: str, mode: str, symbol: str) -> ClosedTrade | None:
        matches = [item for item in self.closed_trades
                   if item.strategy == strategy and item.mode == mode
                   and item.symbol == symbol and item.entry_context == "normal"]
        return max(matches, key=lambda item: item.closed_at_ms) if matches else None

    def count_stop_losses(
        self, strategy: str, mode: str, symbol: str, side, start_ms: int, end_ms: int,
    ) -> int:
        return sum(
            1 for item in self.closed_trades
            if item.strategy == strategy and item.mode == mode and item.symbol == symbol
            and item.side is side and item.close_reason == "stop_loss"
            and start_ms <= item.closed_at_ms < end_ms
        )

    def has_losing_trade(
        self, strategy: str, mode: str, symbol: str, start_ms: int, end_ms: int,
    ) -> bool:
        return any(
            pos.strategy == strategy and pos.symbol == symbol and pnl < 0
            and self.closed_position_modes.get(pos.trade_id) == mode
            and start_ms <= pos.opened_at_ms < end_ms
            for pos, _, pnl, _ in self.closed
        )

    def event_seen(self, event_id: str, strategy: str, mode: str) -> bool:
        return self.events.get((strategy, mode, event_id)) == "processed"

    def save_event(self, event: StrategyEvent, status: str, mode: str, version: str) -> None:
        self.events[(event.strategy, mode, event.event_id)] = status

    def close(self) -> None:
        pass
