"""Position monitoring, strategy exits, and exchange reconciliation."""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Mapping

from .config import RuntimeConfig
from .execution import ExecutionService
from .models import Position, RunMode, Side
from .ports import ExchangePort, MarketDataPort, StrategyPort, TradeStorePort


class PositionManager:
    def __init__(self, config: RuntimeConfig, market: MarketDataPort, exchange: ExchangePort,
                 store: TradeStorePort, execution: ExecutionService,
                 positions: dict[tuple[str, str], Position]) -> None:
        self.config, self.market, self.exchange = config, market, exchange
        self.store, self.execution, self.positions = store, execution, positions
        self.paper_strategy_names = {
            strategy.name for strategy in config.strategies
            if strategy.mode is RunMode.PAPER
        }

    def reconcile(self) -> None:
        if self.config.mode is not RunMode.LIVE:
            return
        for position in list(self.positions.values()):
            try:
                amount = abs(self.exchange.position_amount(position.symbol))
                if amount == 0:
                    mark = self.market.mark_price(position.symbol)
                    settlement = self.exchange.settlement(position, time.time_ns() // 1_000_000)
                    if settlement is not None:
                        mark = settlement.exit_price
                        position.realized_pnl = settlement.net_pnl
                    else:
                        gross = self.execution.gross_pnl(position, mark, position.remaining_quantity)
                        fees = ((position.entry_price + mark) * position.remaining_quantity
                                * self.config.fee_rate)
                        position.realized_pnl += gross - fees
                    reason = self._reconciled_close_reason(position, mark)
                    self.exchange.cancel_symbol_orders(position.symbol)
                    self.store.save_close(position, mark, position.realized_pnl, reason)
                    self.positions.pop((position.strategy, position.symbol), None)
                    self.execution.notifier.send(
                        self.execution.close_message(position, mark, reason)
                    )
                elif amount < position.remaining_quantity:
                    position.remaining_quantity = amount
                    position.stop_order_id = self.exchange.replace_stop(position, position.stop_price)
                    self.store.save_position(position)
                elif position.stop_order_id is None:
                    position.stop_order_id = self.exchange.replace_stop(position, position.stop_price)
                    self.store.save_position(position)
                    logging.info("adopted position protection strategy=%s symbol=%s stop_order_id=%s",
                                 position.strategy, position.symbol, position.stop_order_id)
            except Exception:
                logging.exception("position reconciliation failed symbol=%s", position.symbol)

    def _reconciled_close_reason(self, position: Position, exit_price) -> str:
        slippage = self.config.strategy_risk(position.strategy).exit_slippage
        tolerance = slippage + Decimal("0.001")
        crossed_stop = (
            exit_price <= position.stop_price * (Decimal("1") + tolerance)
            if position.side is Side.LONG
            else exit_price >= position.stop_price * (Decimal("1") - tolerance)
        )
        if not crossed_stop:
            return "exchange_close_reconciled"
        if "trailing" in position.stop_reason:
            return "trailing_stop"
        if position.stop_reason == "breakeven":
            return "break_even"
        profitable_stop = (
            position.stop_price >= position.entry_price
            if position.side is Side.LONG
            else position.stop_price <= position.entry_price
        )
        if profitable_stop or position.stop_reason in {
            "profit_lock", "early_profit_guard", "second_profit_guard",
        }:
            return "protected_stop"
        return "stop_loss"

    def manage_all(self, strategies: Mapping[str, StrategyPort]) -> None:
        for key, position in list(self.positions.items()):
            strategy = strategies[position.strategy]
            try:
                candles = self.market.candles(position.symbol, strategy.interval, strategy.candle_limit)
                mark = self.market.mark_price(position.symbol)
                position.best_price = (max(position.best_price, mark) if position.side is Side.LONG
                                       else min(position.best_price, mark))
                position.worst_price = (
                    min(position.worst_price or position.entry_price, mark)
                    if position.side is Side.LONG
                    else max(position.worst_price or position.entry_price, mark)
                )
                self.store.save_position(position)
                for action in strategy.manage(position, candles, mark):
                    if action.kind == "move_stop" and action.stop_price is not None:
                        self.execution.move_stop(position, action.stop_price, action.reason)
                    elif action.kind == "add" and action.margin is not None:
                        if (self.config.mode is RunMode.LIVE
                                and position.strategy in self.paper_strategy_names):
                            logging.info("skipping live add after paper switch strategy=%s symbol=%s",
                                         position.strategy, position.symbol)
                        else:
                            self.execution.add(position, action.margin, action.reason, mark)
                    elif action.kind == "close":
                        previous_remaining = position.remaining_quantity
                        self.execution.close(position, action.quantity or position.remaining_quantity,
                                             action.reason, mark)
                        if (action.tier is not None and position.remaining_quantity > 0
                                and position.remaining_quantity < previous_remaining):
                            position.partial_tiers_done = tuple(sorted(
                                set(position.partial_tiers_done) | {action.tier}
                            ))
                            self.store.save_position(position)
                            for protection in strategy.manage(position, candles, mark):
                                if protection.kind == "move_stop" and protection.stop_price is not None:
                                    self.execution.move_stop(
                                        position, protection.stop_price, protection.reason,
                                    )
                                    break
                        break
            except Exception:
                logging.exception("position management failed strategy=%s symbol=%s", *key)
