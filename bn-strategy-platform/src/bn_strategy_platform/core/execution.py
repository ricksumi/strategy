"""Centralized order execution and protective-stop guarantees."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import RuntimeConfig, StrategyRiskConfig
from .models import AccountSnapshot, OrderFill, Position, RunMode, Side, Signal, TradePlan
from .ports import ExchangePort, MarketDataPort, NotifierPort, TradeStorePort
from .risk import RiskPolicy


class ExecutionService:
    def __init__(self, config: RuntimeConfig, market: MarketDataPort, exchange: ExchangePort,
                 store: TradeStorePort, notifier: NotifierPort,
                 positions: dict[tuple[str, str], Position], *,
                 enforce_portfolio_limits: bool = True,
                 enforce_global_limits: bool = True,
                 notify_trades: bool = True,
                 entry_context: str = "normal") -> None:
        self.config, self.market, self.exchange = config, market, exchange
        self.store, self.notifier, self.positions = store, notifier, positions
        self.enforce_portfolio_limits = enforce_portfolio_limits
        self.enforce_global_limits = enforce_global_limits
        self.notify_trades = notify_trades
        self.entry_context = entry_context

    def open(self, signal: Any) -> Position:
        account = self.account_snapshot()
        if self.config.mode is RunMode.LIVE:
            exchange_symbols = set(self.exchange.open_position_symbols())
            if signal.symbol in exchange_symbols:
                raise ValueError("symbol already has Binance position")
            managed_symbols = {position.symbol for position in self.positions.values()}
            external_count = len(exchange_symbols - managed_symbols)
            if len(account.positions) + external_count >= self.config.hard_max_positions:
                raise ValueError("global hard maximum concurrent positions reached")
        plan, strategy_risk = self._prepare_plan(signal, account)
        if self.config.mode is RunMode.LIVE:
            self.exchange.set_leverage(signal.symbol, strategy_risk.leverage)
            fill = self.exchange.enter(plan, strategy_risk.entry_slippage)
        else:
            paper_entry = signal.signal_price * (
                Decimal("1") + strategy_risk.entry_slippage
                if signal.side is Side.LONG
                else Decimal("1") - strategy_risk.entry_slippage
            )
            fill = OrderFill(f"paper-{uuid4().hex}", signal.symbol, signal.side.entry_order_side,
                             plan.quantity, paper_entry, "FILLED")
        stop_ratio = abs(signal.signal_price - signal.stop_price) / signal.signal_price
        stop = fill.average_price * (Decimal("1") - stop_ratio if signal.side is Side.LONG
                                     else Decimal("1") + stop_ratio)
        position = Position(
            trade_id=uuid4().hex, strategy=signal.strategy, strategy_version=signal.strategy_version,
            symbol=signal.symbol, side=signal.side, entry_price=fill.average_price, quantity=fill.quantity,
            remaining_quantity=fill.quantity,
            margin=fill.average_price * fill.quantity / Decimal(strategy_risk.leverage),
            leverage=strategy_risk.leverage, stop_price=stop, opened_at_ms=int(time.time() * 1000),
            best_price=fill.average_price, worst_price=fill.average_price,
            initial_stop_price=stop,
            initial_entry_price=fill.average_price,
            initial_risk_distance=abs(fill.average_price - stop),
            entry_score=signal.score,
        )
        if self.config.mode is RunMode.LIVE:
            try:
                position.stop_order_id = self.exchange.replace_stop(position, stop)
                self.store.save_open(
                    position, self.config.mode.value, signal.score, signal.reason,
                    self.entry_context, signal.metrics,
                )
                self.positions[(position.strategy, position.symbol)] = position
                self.store.save_position(position)
                logging.info(
                    "position opened and protected strategy=%s symbol=%s side=%s entry=%s "
                    "quantity=%s stop=%s stop_order_id=%s",
                    position.strategy, position.symbol, position.side.value, position.entry_price,
                    position.quantity, position.stop_price, position.stop_order_id,
                )
            except Exception:
                logging.exception(
                    "protected entry setup failed; immediately closing strategy=%s symbol=%s",
                    signal.strategy, signal.symbol,
                )
                self.emergency_close(position)
                raise
        else:
            self.store.save_open(
                position, self.config.mode.value, signal.score, signal.reason,
                self.entry_context, signal.metrics,
            )
            self.positions[(position.strategy, position.symbol)] = position
        if self.notify_trades:
            self.notifier.send(self.open_message(position, signal.score, signal.reason))
        return position

    def add(self, position: Position, margin: Decimal, reason: str, mark: Decimal) -> None:
        if margin <= 0 or position.scale_in_done:
            return
        if position.remaining_quantity != position.quantity or position.partial_tiers_done:
            raise ValueError("scale-in is only allowed before partial exits")
        strategy_risk = self.config.strategy_risk(position.strategy)
        account = self.account_snapshot()
        if margin > account.available_usdt:
            raise ValueError("insufficient available margin for scale-in")
        instrument = self.market.instruments()[position.symbol]
        raw_quantity = margin * Decimal(strategy_risk.leverage) / mark
        quantity = (
            (raw_quantity / instrument.step_size).to_integral_value(rounding=ROUND_DOWN)
            * instrument.step_size
        )
        if quantity < instrument.min_quantity or quantity * mark < instrument.min_notional:
            raise ValueError("scale-in quantity is below exchange minimum")
        estimated_fill = mark * (
            Decimal("1") + strategy_risk.entry_slippage
            if position.side is Side.LONG
            else Decimal("1") - strategy_risk.entry_slippage
        )
        projected_quantity = position.quantity + quantity
        projected_entry = (
            position.entry_price * position.quantity + estimated_fill * quantity
        ) / projected_quantity
        projected_risk = self._price_stop_risk(
            position.side, projected_entry, position.stop_price, projected_quantity,
        )
        if projected_risk > strategy_risk.risk_per_trade_usdt:
            raise ValueError(
                f"scale-in planned loss {projected_risk} exceeds per-trade risk limit"
            )
        current_risk = self.stop_risk(position)
        other_risk = sum(
            (self.stop_risk(item) for item in account.positions), Decimal("0")
        ) - current_risk
        directional_other = sum(
            (self.stop_risk(item) for item in account.positions
             if item.side is position.side), Decimal("0")
        ) - current_risk
        if self.enforce_global_limits:
            if other_risk + projected_risk > self.config.max_open_risk_usdt:
                raise ValueError("global open risk limit reached by scale-in")
            if (directional_other + projected_risk
                    > self.config.max_directional_open_risk_usdt):
                raise ValueError("directional open risk limit reached by scale-in")
        signal = Signal(
            position.strategy, position.strategy_version, position.symbol,
            position.side, mark, position.stop_price, position.entry_score,
            reason, int(time.time() * 1000), {"scale_in": True}, margin,
        )
        plan = TradePlan(signal, projected_risk, quantity, margin, strategy_risk.leverage)
        if self.config.mode is RunMode.LIVE:
            fill = self.exchange.enter(plan, strategy_risk.entry_slippage)
        else:
            paper_entry = mark * (
                Decimal("1") + strategy_risk.entry_slippage
                if position.side is Side.LONG
                else Decimal("1") - strategy_risk.entry_slippage
            )
            fill = OrderFill(
                f"paper-{uuid4().hex}", position.symbol,
                position.side.entry_order_side, quantity, paper_entry, "FILLED",
            )
        old_quantity = position.quantity
        total_quantity = old_quantity + fill.quantity
        position.entry_price = (
            position.entry_price * old_quantity + fill.average_price * fill.quantity
        ) / total_quantity
        position.quantity = total_quantity
        position.remaining_quantity += fill.quantity
        added_margin = fill.average_price * fill.quantity / Decimal(strategy_risk.leverage)
        position.margin += added_margin
        position.scale_in_done = True
        position.scale_in_quantity = fill.quantity
        position.scale_in_price = fill.average_price
        if self.config.mode is RunMode.LIVE:
            try:
                position.stop_order_id = self.exchange.replace_stop(
                    position, position.stop_price,
                )
            except Exception:
                logging.exception(
                    "scale-in stop replacement failed; emergency closing strategy=%s symbol=%s",
                    position.strategy, position.symbol,
                )
                self.notifier.send(
                    f"[CRITICAL] SCALE-IN PROTECTION FAILED {position.symbol}; "
                    "emergency closing the full position"
                )
                self.emergency_close(position)
                raise
        self.store.save_position(position)
        logging.info(
            "position scaled in strategy=%s symbol=%s added_quantity=%s add_price=%s "
            "total_quantity=%s average_entry=%s margin=%s stop=%s",
            position.strategy, position.symbol, fill.quantity, fill.average_price,
            position.quantity, position.entry_price, position.margin, position.stop_price,
        )
        if self.notify_trades:
            event = "ADD" if self.config.mode is RunMode.LIVE else "PAPER ADD"
            self.notifier.send(
                f"[{event}] {position.symbol} {position.side.value.upper()}\n"
                f"Strategy: {self.strategy_label(position.strategy, position.strategy_version)}\n"
                f"Added margin: {added_margin:.4f} USDT\n"
                f"Add price: {fill.average_price}\nAverage entry: {position.entry_price}\n"
                f"Total quantity: {position.quantity}\nStop: {position.stop_price}\nReason: {reason}"
            )

    def validate_open_after_release(self, signal: Any, released: Position) -> None:
        """Validate an entry against a conservative post-rotation account snapshot."""
        account = self.account_snapshot()
        positions = tuple(
            position for position in account.positions if position.trade_id != released.trade_id
        )
        projected = replace(
            account,
            available_usdt=account.available_usdt + released.margin,
            positions=positions,
        )
        self._prepare_plan(signal, projected)

    def _prepare_plan(
        self, signal: Any, account: AccountSnapshot,
    ) -> tuple[TradePlan, StrategyRiskConfig]:
        instruments = self.market.instruments()
        if signal.symbol not in instruments:
            raise ValueError(f"unsupported instrument: {signal.symbol}")
        start_ms, end_ms = self._today_bounds()
        if self.enforce_portfolio_limits and self.enforce_global_limits:
            global_daily_pnl = self.store.daily_net_pnl(
                "*", self.config.mode.value, start_ms, end_ms
            )
            if len(account.positions) >= self.config.hard_max_positions:
                raise ValueError("global hard maximum concurrent positions reached")
            risk_positions = sum(1 for item in account.positions if self.stop_risk(item) > 0)
            if risk_positions >= self.config.max_positions:
                raise ValueError("global maximum risk positions reached")
            if global_daily_pnl <= -self.config.max_daily_loss_usdt:
                raise ValueError("global daily net loss limit reached")
        strategy_risk = self.config.strategy_risk(signal.strategy)
        policy = RiskPolicy(
            strategy_risk.leverage,
            strategy_risk.margin_per_trade_usdt,
            strategy_risk.risk_per_trade_usdt,
            strategy_risk.max_daily_loss_usdt,
            strategy_risk.max_positions,
        )
        if self.enforce_portfolio_limits:
            strategy_positions = tuple(
                item for item in account.positions if item.strategy == signal.strategy
            )
            if self.config.mode is RunMode.PAPER:
                used = sum((item.margin for item in strategy_positions), Decimal("0"))
                available = max(Decimal("0"), self.config.paper_equity_usdt - used)
            else:
                available = account.available_usdt
            strategy_account = replace(
                account, available_usdt=available, positions=strategy_positions,
            )
            strategy_daily_pnl = self.store.daily_net_pnl(
                signal.strategy, self.config.mode.value, start_ms, end_ms
            )
        else:
            strategy_account = AccountSnapshot(
                self.config.paper_equity_usdt, Decimal("1e30"), ()
            )
            strategy_daily_pnl = Decimal("0")
        plan = policy.build_plan(
            signal, instruments[signal.symbol], strategy_account, strategy_daily_pnl,
            enforce_planned_risk=self.enforce_portfolio_limits,
        )
        if self.enforce_portfolio_limits and self.enforce_global_limits:
            stop_ratio = abs(signal.signal_price - signal.stop_price) / signal.signal_price
            planned_risk = (
                plan.estimated_margin * Decimal(plan.leverage) * stop_ratio
                * (Decimal("1") + strategy_risk.entry_slippage)
            )
            open_risk = sum((self.stop_risk(item) for item in account.positions), Decimal("0"))
            directional_risk = sum(
                (self.stop_risk(item) for item in account.positions if item.side is signal.side),
                Decimal("0"),
            )
            if open_risk + planned_risk > self.config.max_open_risk_usdt:
                raise ValueError("global open risk limit reached")
            if (directional_risk + planned_risk
                    > self.config.max_directional_open_risk_usdt):
                raise ValueError("directional open risk limit reached")
        return plan, strategy_risk

    def move_stop(self, position: Position, stop_price: Decimal, reason: str) -> None:
        improved = stop_price > position.stop_price if position.side is Side.LONG else stop_price < position.stop_price
        if not improved:
            return
        if self.config.mode is RunMode.LIVE:
            position.stop_order_id = self.exchange.replace_stop(position, stop_price)
        position.stop_price = stop_price
        position.stop_reason = reason
        self.store.save_position(position)
        logging.info("stop moved strategy=%s symbol=%s stop=%s reason=%s",
                     position.strategy, position.symbol, stop_price, reason)

    def close(self, position: Position, quantity: Decimal, reason: str, mark: Decimal) -> None:
        quantity = min(quantity, position.remaining_quantity)
        if quantity < position.remaining_quantity:
            instrument = self.market.instruments()[position.symbol]
            quantity = ((quantity / instrument.step_size).to_integral_value(rounding=ROUND_DOWN)
                        * instrument.step_size)
            if quantity < instrument.min_quantity:
                return
        strategy_risk = self.config.strategy_risk(position.strategy)
        if self.config.mode is RunMode.LIVE:
            fill = self.exchange.reduce(position, quantity, strategy_risk.exit_slippage)
        else:
            paper_exit = mark * (
                Decimal("1") - strategy_risk.exit_slippage
                if position.side is Side.LONG
                else Decimal("1") + strategy_risk.exit_slippage
            )
            fill = OrderFill(
                f"paper-{uuid4().hex}", position.symbol, position.side.exit_order_side,
                quantity, paper_exit, "FILLED",
            )
        self._record_fill(position, fill)
        settlement = self._settlement(position)
        if settlement is not None:
            position.realized_pnl = settlement.net_pnl
        if position.remaining_quantity > 0:
            if self.config.mode is RunMode.LIVE:
                try:
                    position.stop_order_id = self.exchange.replace_stop(
                        position, position.stop_price,
                    )
                except Exception:
                    logging.exception(
                        "remaining-position stop replacement failed; emergency closing "
                        "strategy=%s symbol=%s remaining=%s",
                        position.strategy, position.symbol, position.remaining_quantity,
                    )
                    self.notifier.send(
                        f"[CRITICAL] PARTIAL EXIT PROTECTION FAILED {position.symbol}; "
                        "emergency closing remaining position"
                    )
                    self.emergency_close(position)
                    raise
            self.store.save_position(position)
            logging.info(
                "partial exit completed strategy=%s symbol=%s closed=%s remaining=%s "
                "stop=%s stop_order_id=%s",
                position.strategy, position.symbol, fill.quantity,
                position.remaining_quantity, position.stop_price,
                position.stop_order_id or "paper",
            )
            return
        if self.config.mode is RunMode.LIVE:
            self.exchange.cancel_symbol_orders(position.symbol)
        exit_price = settlement.exit_price if settlement is not None else fill.average_price
        self.store.save_close(position, exit_price, position.realized_pnl, reason)
        self.positions.pop((position.strategy, position.symbol), None)
        if self.notify_trades:
            self.notifier.send(self.close_message(position, exit_price, reason))

    def emergency_close(self, position: Position) -> None:
        try:
            fill = self.exchange.emergency_close(position)
            self._record_fill(position, fill)
            settlement = self._settlement(position)
            if settlement is not None:
                position.realized_pnl = settlement.net_pnl
            exit_price = settlement.exit_price if settlement is not None else fill.average_price
            if self.config.mode is RunMode.LIVE:
                self.exchange.cancel_symbol_orders(position.symbol)
            self.store.save_close(position, exit_price, position.realized_pnl, "protective_stop_failed")
            self.positions.pop((position.strategy, position.symbol), None)
            self.notifier.send(self.close_message(position, exit_price, "protective_stop_failed"))
        except Exception:
            logging.critical("UNPROTECTED POSITION strategy=%s symbol=%s", position.strategy, position.symbol,
                             exc_info=True)
            self.notifier.send(f"[CRITICAL] UNPROTECTED POSITION {position.symbol}; manual action required")

    def account_snapshot(self) -> AccountSnapshot:
        if self.config.mode is RunMode.LIVE:
            return replace(self.exchange.account(), positions=tuple(self.positions.values()))
        used = sum((item.margin for item in self.positions.values()), Decimal("0"))
        return AccountSnapshot(self.config.paper_equity_usdt,
                               max(Decimal("0"), self.config.paper_equity_usdt - used),
                               tuple(self.positions.values()))

    @staticmethod
    def stop_risk(position: Position) -> Decimal:
        return ExecutionService._price_stop_risk(
            position.side, position.entry_price, position.stop_price,
            position.remaining_quantity,
        )

    @staticmethod
    def _price_stop_risk(
        side: Side, entry: Decimal, stop: Decimal, quantity: Decimal,
    ) -> Decimal:
        distance = entry - stop if side is Side.LONG else stop - entry
        return max(Decimal("0"), distance) * quantity

    def _record_fill(self, position: Position, fill: OrderFill) -> None:
        gross = self.gross_pnl(position, fill.average_price, fill.quantity)
        fees = (position.entry_price + fill.average_price) * fill.quantity * self.config.fee_rate
        position.realized_pnl += gross - fees
        position.remaining_quantity -= fill.quantity

    def _settlement(self, position: Position):
        if self.config.mode is not RunMode.LIVE:
            return None
        for _ in range(3):
            result = self.exchange.settlement(position, int(time.time() * 1000))
            if result is not None:
                return result
            time.sleep(0.2)
        return None

    @staticmethod
    def gross_pnl(position: Position, exit_price: Decimal, quantity: Decimal) -> Decimal:
        move = exit_price - position.entry_price
        return move * quantity if position.side is Side.LONG else -move * quantity

    def open_message(self, position: Position, score: Decimal, reason: str) -> str:
        label = ExecutionService.strategy_label(position.strategy, position.strategy_version)
        event = "OPEN" if self.config.mode is RunMode.LIVE else "PAPER OPEN"
        return (f"[{event}] {position.symbol} {position.side.value.upper()} {position.leverage}x\n"
                f"Strategy: {label}\nStrategy ID: {position.strategy} v{position.strategy_version}\n"
                f"Entry: {position.entry_price}\n"
                f"Quantity: {position.quantity}\nMargin: {position.margin:.4f} USDT\n"
                f"Stop: {position.stop_price}\nScore: {score:.2f}\nReason: {reason}")

    def close_message(self, position: Position, exit_price: Decimal, reason: str) -> str:
        roi = position.realized_pnl / position.margin * Decimal("100") if position.margin else Decimal("0")
        label = ExecutionService.strategy_label(position.strategy, position.strategy_version)
        event = "CLOSED" if self.config.mode is RunMode.LIVE else "PAPER CLOSED"
        return (f"[{event}] {position.symbol} {position.side.value.upper()}\n"
                f"Strategy: {label}\nStrategy ID: {position.strategy} v{position.strategy_version}\n"
                f"Reason: {reason}\nEntry: {position.entry_price}\n"
                f"Exit: {exit_price}\nNet PnL: {position.realized_pnl:.4f} USDT\nMargin ROI: {roi:.2f}%")

    @staticmethod
    def strategy_label(strategy: str, version: str) -> str:
        if strategy == "bn-stra-top-gainers-exhaustion-short-1":
            return ("Top Gainers Pullback Long" if version.startswith("4.")
                    else "Top Gainers Exhaustion Short")
        return {
            "bn-stra-top-gainers-1": "Momentum Reversal Short",
            "bn-stra-copy-lead-1": "Copy Lead",
        }.get(strategy, strategy)

    @staticmethod
    def _today_bounds() -> tuple[int, int]:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)
