"""Thin orchestration layer for market and event-driven strategies."""

from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .config import RuntimeConfig
from .execution import ExecutionService
from .models import Position, RunMode
from .ports import EventStrategyPort, ExchangePort, MarketDataPort, NotifierPort, StrategyPort, TradeStorePort
from .position_manager import PositionManager


@dataclass
class StrategyRuntime:
    strategy: StrategyPort
    mode: RunMode
    universe: tuple[str, ...] = ()
    refresh_at: float = 0
    refresh_future: Future[Sequence[str]] | None = None


@dataclass(frozen=True)
class PendingEntry:
    runtime: StrategyRuntime
    signal: Any


class TradingEngine:
    def __init__(self, config: RuntimeConfig, market: MarketDataPort, exchange: ExchangePort,
                 store: TradeStorePort, notifier: NotifierPort, strategies: Sequence[StrategyPort],
                 event_strategies: Sequence[EventStrategyPort] = ()) -> None:
        self.config, self.market, self.exchange = config, market, exchange
        self.store, self.notifier = store, notifier
        self.runtimes = [
            StrategyRuntime(item, config.strategy_mode(item.name)) for item in strategies
        ]
        self.event_strategies = list(event_strategies)
        self.universe_executor = ThreadPoolExecutor(
            max_workers=max(1, len(self.runtimes)), thread_name_prefix="universe"
        )
        self.strategy_map = {item.name: item for item in strategies}
        self.strategy_map.update({item.name: item for item in event_strategies})
        self.positions: dict[tuple[str, str], Position] = {}
        self.paper_positions: dict[tuple[str, str], Position] = {}
        self.position_limit_paper_positions: dict[tuple[str, str], Position] = {}
        self.last_signal_candle: dict[tuple[str, str], int] = {}
        self.reported_unmanaged: set[str] = set()
        self.unmanaged_live_symbols: set[str] = set()
        self.last_rotation_ms = 0
        for strategy in self.strategy_map.values():
            mode = config.strategy_mode(strategy.name)
            target = self.positions if mode is RunMode.LIVE else self.paper_positions
            if mode is not RunMode.SHADOW:
                for position in store.load_open_positions(strategy.name, mode.value):
                    target[(position.strategy, position.symbol)] = position
            if config.mode is RunMode.LIVE and mode is RunMode.PAPER:
                for position in store.load_open_positions(strategy.name, RunMode.LIVE.value):
                    self.positions[(position.strategy, position.symbol)] = position
                    logging.info(
                        "continuing live position after paper switch strategy=%s symbol=%s",
                        position.strategy, position.symbol,
                    )
            restore_open_times = getattr(strategy, "restore_open_times", None)
            if restore_open_times:
                restore_open_times(store.load_last_open_times(strategy.name, mode.value))
        self.execution = ExecutionService(config, market, exchange, store, notifier, self.positions)
        self.position_manager = PositionManager(config, market, exchange, store,
                                                self.execution, self.positions)
        self.paper_execution: ExecutionService | None = None
        self.paper_manager: PositionManager | None = None
        if any(config.strategy_mode(item.name) is RunMode.PAPER
               for item in self.strategy_map.values()):
            paper_config = replace(config, mode=RunMode.PAPER)
            self.paper_execution = ExecutionService(
                paper_config, market, exchange, store, notifier, self.paper_positions,
                enforce_global_limits=False,
            )
            self.paper_manager = PositionManager(
                paper_config, market, exchange, store,
                self.paper_execution, self.paper_positions,
            )
        self.position_limit_paper_execution: ExecutionService | None = None
        self.position_limit_paper_manager: PositionManager | None = None
        if config.mode is RunMode.LIVE and config.paper_on_position_limit:
            paper_config = replace(config, mode=RunMode.PAPER)
            for strategy in self.strategy_map.values():
                if config.strategy_mode(strategy.name) is not RunMode.LIVE:
                    continue
                for position in store.load_open_positions(strategy.name, RunMode.PAPER.value):
                    self.position_limit_paper_positions[(position.strategy, position.symbol)] = position
            self.position_limit_paper_execution = ExecutionService(
                paper_config, market, exchange, store, notifier,
                self.position_limit_paper_positions,
                enforce_portfolio_limits=False,
                notify_trades=True,
                entry_context="position_limit",
            )
            self.position_limit_paper_manager = PositionManager(
                paper_config, market, exchange, store,
                self.position_limit_paper_execution, self.position_limit_paper_positions,
            )

    def run_forever(self) -> None:
        logging.info(
            "platform started mode=%s strategies=%s", self.config.mode.value,
            {name: self.config.strategy_mode(name).value for name in self.strategy_map},
        )
        while True:
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                logging.exception("platform scan failed")
            time.sleep(max(0, self.config.poll_seconds - (time.monotonic() - started)))

    def run_once(self) -> None:
        self.position_manager.reconcile()
        self.position_manager.manage_all(self.strategy_map)
        if self.paper_manager is not None:
            self.paper_manager.manage_all(self.strategy_map)
        if self.position_limit_paper_manager is not None:
            self.position_limit_paper_manager.manage_all(self.strategy_map)
        self._refresh_unmanaged_live_positions()
        for strategy in self.event_strategies:
            self._scan_events(strategy)
        live_entries: list[PendingEntry] = []
        for runtime in self.runtimes:
            if getattr(runtime.strategy, "background_universe_refresh", False):
                if not self._refresh_background_universe(runtime):
                    continue
            elif time.monotonic() >= runtime.refresh_at:
                runtime.universe = tuple(runtime.strategy.select_universe(self.market))
                runtime.refresh_at = time.monotonic() + self._universe_refresh_seconds(runtime)
                logging.info("universe refreshed strategy=%s symbols=%s", runtime.strategy.name, runtime.universe)
            live_entries.extend(self._scan_entries(runtime, defer_live=True))
        self._allocate_live_entries(live_entries)

    def _refresh_background_universe(self, runtime: StrategyRuntime) -> bool:
        if runtime.refresh_future is not None:
            if not runtime.refresh_future.done():
                return False
            try:
                runtime.universe = tuple(runtime.refresh_future.result())
                logging.info(
                    "universe refreshed strategy=%s symbols=%s",
                    runtime.strategy.name, runtime.universe,
                )
            except Exception:
                logging.exception(
                    "background universe refresh failed strategy=%s",
                    runtime.strategy.name,
                )
                runtime.universe = ()
            runtime.refresh_future = None
            runtime.refresh_at = time.monotonic() + self._universe_refresh_seconds(runtime)
        if time.monotonic() >= runtime.refresh_at:
            runtime.refresh_future = self.universe_executor.submit(
                runtime.strategy.select_universe, self.market
            )
            return False
        return True

    def _universe_refresh_seconds(self, runtime: StrategyRuntime) -> int:
        return max(1, int(getattr(
            runtime.strategy, "universe_refresh_seconds",
            self.config.universe_refresh_seconds,
        )))

    def _scan_entries(
        self, runtime: StrategyRuntime, *, defer_live: bool = False,
    ) -> list[PendingEntry]:
        pending: list[PendingEntry] = []
        for symbol in runtime.universe:
            if runtime.mode is RunMode.PAPER and (runtime.strategy.name, symbol) in self.positions:
                continue
            if runtime.mode is RunMode.LIVE and symbol in self.unmanaged_live_symbols:
                discard = getattr(runtime.strategy, "discard_entry", None)
                if discard is not None:
                    discard(symbol, "unmanaged_binance_position")
                continue
            key = (runtime.strategy.name, symbol)
            active_positions = (self.positions if runtime.mode is RunMode.LIVE
                                else self.paper_positions)
            symbol_conflict = (
                runtime.mode is RunMode.LIVE
                and any(position.symbol == symbol for position in active_positions.values())
            )
            if (runtime.mode is not RunMode.SHADOW
                    and (key in active_positions or symbol_conflict)):
                discard = getattr(runtime.strategy, "discard_entry", None)
                if discard is not None:
                    discard(symbol, "position_conflict")
                continue
            signal = None
            try:
                candles = self.market.candles(symbol, runtime.strategy.interval, runtime.strategy.candle_limit)
                if not candles:
                    continue
                token_method = getattr(runtime.strategy, "entry_scan_token", None)
                scan_token = (token_method(symbol, candles) if token_method
                              else candles[-1].close_time_ms)
                if self.last_signal_candle.get(key) == scan_token:
                    continue
                signal = runtime.strategy.evaluate(symbol, candles, self.market.mark_price(symbol))
                if signal is None:
                    continue
                self.last_signal_candle[key] = scan_token
                self.store.save_signal(signal, runtime.mode.value)
                block_reason = self._entry_block_reason(runtime, signal)
                if block_reason is not None:
                    logging.info(
                        "entry blocked strategy=%s symbol=%s reason=%s",
                        runtime.strategy.name, signal.symbol, block_reason,
                    )
                    continue
                if runtime.mode is RunMode.SHADOW:
                    self.notifier.send(self.signal_message(signal))
                elif runtime.mode is RunMode.LIVE and defer_live:
                    pending.append(PendingEntry(runtime, signal))
                else:
                    self._execute_entry(runtime, signal)
            except ValueError as exc:
                logging.warning("entry blocked strategy=%s symbol=%s reason=%s",
                                runtime.strategy.name, symbol, exc)
            except Exception:
                logging.exception("entry scan failed strategy=%s symbol=%s", runtime.strategy.name, symbol)
        return pending

    def _entry_block_reason(
        self, runtime: StrategyRuntime, signal: Any,
    ) -> str | None:
        if runtime.mode is not RunMode.LIVE:
            return None
        now_ms = int(time.time() * 1000)
        for event_strategy in self.event_strategies:
            gate = getattr(event_strategy, "entry_block_reason", None)
            if gate is None:
                continue
            reason = gate(runtime.strategy.name, signal.symbol, now_ms)
            if reason is not None:
                return str(reason)
        settings = getattr(runtime.strategy, "settings", None)
        cooldown_minutes = int(getattr(settings, "loss_cooldown_minutes", 0))
        if cooldown_minutes <= 0:
            return None
        max_daily_stops = int(getattr(settings, "max_symbol_stop_losses_per_day", 0))
        if max_daily_stops > 0:
            now = datetime.fromtimestamp(now_ms / 1000, ZoneInfo("Asia/Shanghai"))
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ms = int(start.timestamp() * 1000)
            end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
            stops = self.store.count_stop_losses(
                runtime.strategy.name, runtime.mode.value, signal.symbol, signal.side,
                start_ms, end_ms,
            )
            if stops >= max_daily_stops:
                return f"daily symbol stop-loss limit reached ({stops}/{max_daily_stops})"
        previous = self.store.last_closed_trade(
            runtime.strategy.name, runtime.mode.value, signal.symbol,
        )
        if previous is None or previous.net_pnl >= 0 or previous.side is not signal.side:
            return None
        eligible_at = previous.closed_at_ms + cooldown_minutes * 60_000
        if now_ms < eligible_at:
            remaining_seconds = max(1, (eligible_at - now_ms + 999) // 1000)
            return f"same-direction loss cooldown ({remaining_seconds}s remaining)"
        lookback = int(getattr(settings, "reentry_breakout_lookback", 3))
        candles = self.market.candles(
            signal.symbol, "5m", max(lookback + 8, 12),
        )
        eligible_indices = [
            index for index, candle in enumerate(candles)
            if eligible_at <= candle.close_time_ms <= now_ms
        ]
        if not eligible_indices:
            return "awaiting a complete 5m candle after loss cooldown"
        latest_index = eligible_indices[-1]
        if latest_index < lookback:
            return "insufficient 5m structure for loss re-entry"
        latest = candles[latest_index]
        reference = candles[latest_index - lookback:latest_index]
        breakout = (
            latest.close > max(candle.high for candle in reference)
            if signal.side.value == "long"
            else latest.close < min(candle.low for candle in reference)
        )
        if not breakout:
            return "loss re-entry requires a fresh 5m structure breakout"
        return None

    def _allocate_live_entries(self, pending: list[PendingEntry]) -> None:
        remaining = list(pending)
        while remaining:
            counts = Counter(position.strategy for position in self.positions.values())
            reserved = [
                item for item in remaining
                if counts[item.runtime.strategy.name]
                < self.config.strategy_risk(item.runtime.strategy.name).base_positions
            ]
            if not reserved:
                break
            selected = max(reserved, key=lambda item: item.signal.score)
            remaining.remove(selected)
            self._execute_entry(selected.runtime, selected.signal)
        for item in sorted(remaining, key=lambda entry: entry.signal.score, reverse=True):
            self._execute_entry(item.runtime, item.signal)

    def _execute_entry(self, runtime: StrategyRuntime, signal: Any) -> bool:
        if runtime.mode is RunMode.LIVE and signal.symbol in self.unmanaged_live_symbols:
            logging.warning(
                "entry blocked strategy=%s symbol=%s reason=unmanaged Binance position",
                runtime.strategy.name, signal.symbol,
            )
            return False
        if (runtime.mode is RunMode.LIVE
                and any(position.symbol == signal.symbol for position in self.positions.values())):
            logging.warning(
                "entry blocked strategy=%s symbol=%s reason=symbol already has live position",
                runtime.strategy.name, signal.symbol,
            )
            self._open_position_limit_paper(signal)
            return False
        try:
            execution = (self.execution if runtime.mode is RunMode.LIVE
                         else self.paper_execution)
            if execution is None:
                raise RuntimeError(f"no execution service for {runtime.mode.value}")
            execution.open(signal)
            record_open = getattr(runtime.strategy, "record_open", None)
            if record_open:
                record_open(signal.symbol, signal.observed_at_ms)
            return True
        except ValueError as exc:
            if (runtime.mode is RunMode.LIVE and self._is_rotation_limit(exc)
                    and self._try_rotate_entry(runtime, signal, str(exc))):
                return True
            if runtime.mode is RunMode.LIVE and self._is_allocation_limit(exc):
                self._open_position_limit_paper(signal)
            message = (f"[ENTRY BLOCKED] {runtime.strategy.name} {signal.symbol}\n"
                       f"Reason: {exc}")
            logging.warning(
                "entry blocked strategy=%s symbol=%s reason=%s",
                runtime.strategy.name, signal.symbol, exc,
            )
            if str(exc) == "insufficient available margin":
                self.notifier.send(message)
            return False
        except Exception:
            logging.exception(
                "entry execution failed strategy=%s symbol=%s",
                runtime.strategy.name, signal.symbol,
            )
            return False

    def _try_rotate_entry(
        self, runtime: StrategyRuntime, signal: Any, capacity_reason: str,
    ) -> bool:
        if not self.config.position_rotation_enabled:
            return False
        now_ms = int(time.time() * 1000)
        cooldown_ms = self.config.rotation_cooldown_minutes * 60_000
        if now_ms - self.last_rotation_ms < cooldown_ms:
            return False
        candidate = self._rotation_candidate(signal, capacity_reason, now_ms)
        if candidate is None:
            logging.info(
                "position rotation found no stagnant unprotected candidate "
                "strategy=%s symbol=%s reason=%s",
                signal.strategy, signal.symbol, capacity_reason,
            )
            return False
        mark: Decimal
        try:
            self.execution.validate_open_after_release(signal, candidate)
            mark = self.market.mark_price(candidate.symbol)
            projected = candidate.realized_pnl + ExecutionService.gross_pnl(
                candidate, mark, candidate.remaining_quantity,
            ) - ((candidate.entry_price + mark) * candidate.remaining_quantity
                 * self.config.fee_rate)
            start_ms, end_ms = self.execution._today_bounds()
            global_after = self.store.daily_net_pnl(
                "*", self.config.mode.value, start_ms, end_ms,
            ) + projected
            strategy_after = self.store.daily_net_pnl(
                candidate.strategy, self.config.mode.value, start_ms, end_ms,
            ) + projected
            if (global_after <= -self.config.max_daily_loss_usdt
                    or strategy_after <= -self.config.strategy_risk(
                        candidate.strategy
                    ).max_daily_loss_usdt):
                logging.warning(
                    "position rotation blocked by projected daily loss new_strategy=%s "
                    "new_symbol=%s old_strategy=%s old_symbol=%s projected_pnl=%s",
                    signal.strategy, signal.symbol, candidate.strategy,
                    candidate.symbol, projected,
                )
                return False
        except ValueError as exc:
            logging.info(
                "position rotation preflight rejected strategy=%s symbol=%s reason=%s",
                signal.strategy, signal.symbol, exc,
            )
            return False
        except Exception:
            logging.exception(
                "position rotation failed strategy=%s symbol=%s",
                signal.strategy, signal.symbol,
            )
            return False
        logging.warning(
            "rotating weak position new_strategy=%s new_symbol=%s new_score=%s "
            "old_strategy=%s old_symbol=%s old_score=%s reason=%s",
            signal.strategy, signal.symbol, signal.score, candidate.strategy,
            candidate.symbol, candidate.entry_score, capacity_reason,
        )
        self.last_rotation_ms = now_ms
        try:
            self.execution.close(
                candidate, candidate.remaining_quantity, "capital_rotation", mark,
            )
            position = self.execution.open(signal)
            record_open = getattr(runtime.strategy, "record_open", None)
            if record_open:
                record_open(signal.symbol, signal.observed_at_ms)
            self.notifier.send(
                f"[CAPITAL ROTATION] Closed {candidate.symbol} for "
                f"{signal.strategy} {position.symbol}; scores "
                f"{candidate.entry_score:.2f} -> {Decimal(str(signal.score)):.2f}"
            )
            return True
        except Exception:
            logging.exception(
                "position rotation execution failed strategy=%s symbol=%s",
                signal.strategy, signal.symbol,
            )
            self.notifier.send(
                f"[CRITICAL] CAPITAL ROTATION FAILED {signal.strategy} {signal.symbol}; "
                "the old position may already be closed, check live positions"
            )
            return False

    def _rotation_candidate(
        self, signal: Any, capacity_reason: str, now_ms: int,
    ) -> Position | None:
        candidates: list[tuple[Decimal, Decimal, int, Position]] = []
        same_strategy_only = capacity_reason == "maximum concurrent positions reached"
        same_side_only = capacity_reason == "directional open risk limit reached"
        strategy_counts = Counter(
            position.strategy for position in self.positions.values()
        )
        for position in self.positions.values():
            if same_strategy_only and position.strategy != signal.strategy:
                continue
            if same_side_only and position.side is not signal.side:
                continue
            if (not same_strategy_only
                    and strategy_counts[position.strategy]
                    <= self.config.strategy_risk(position.strategy).base_positions):
                continue
            if ExecutionService.stop_risk(position) <= 0:
                continue
            held_ms = now_ms - position.opened_at_ms
            if held_ms < self.config.rotation_min_hold_minutes * 60_000:
                continue
            initial_entry = position.initial_entry_price or position.entry_price
            initial_stop = position.initial_stop_price or position.stop_price
            initial_risk = (
                position.initial_risk_distance
                or abs(initial_entry - initial_stop)
            )
            if initial_risk <= 0:
                continue
            favorable = (
                position.best_price - initial_entry
                if position.side.value == "long"
                else initial_entry - position.best_price
            )
            best_r = favorable / initial_risk
            if best_r >= self.config.rotation_max_best_r:
                continue
            try:
                mark = self.market.mark_price(position.symbol)
            except Exception:
                logging.exception("rotation mark lookup failed symbol=%s", position.symbol)
                continue
            move = (
                mark - initial_entry
                if position.side.value == "long"
                else initial_entry - mark
            )
            current_r = move / initial_risk
            if (current_r > self.config.rotation_max_current_r
                    or current_r < -self.config.rotation_max_loss_r):
                continue
            candidates.append((current_r, best_r, -held_ms, position))
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1], item[2]))[3]

    def _open_position_limit_paper(self, signal: Any) -> None:
        execution = self.position_limit_paper_execution
        if execution is None:
            return
        key = (signal.strategy, signal.symbol)
        if key in self.position_limit_paper_positions:
            return
        try:
            position = execution.open(signal)
            logging.info(
                "position-limit paper opened strategy=%s version=%s symbol=%s side=%s "
                "entry=%s quantity=%s stop=%s score=%s",
                position.strategy, position.strategy_version, position.symbol,
                position.side.value, position.entry_price, position.quantity,
                position.stop_price, signal.score,
            )
        except Exception:
            logging.exception(
                "position-limit paper open failed strategy=%s symbol=%s",
                signal.strategy, signal.symbol,
            )

    @staticmethod
    def _is_allocation_limit(exc: ValueError) -> bool:
        message = str(exc)
        return message.startswith("planned loss ") or message in {
            "global maximum concurrent positions reached",
            "global hard maximum concurrent positions reached",
            "global maximum risk positions reached",
            "maximum concurrent positions reached",
            "global open risk limit reached",
            "directional open risk limit reached",
            "global daily net loss limit reached",
            "daily net loss limit reached",
            "insufficient available margin",
        }

    @staticmethod
    def _is_rotation_limit(exc: ValueError) -> bool:
        return str(exc) in {
            "global maximum concurrent positions reached",
            "global hard maximum concurrent positions reached",
            "global maximum risk positions reached",
            "maximum concurrent positions reached",
            "global open risk limit reached",
            "directional open risk limit reached",
        }

    def _scan_events(self, strategy: EventStrategyPort) -> None:
        mode = self.config.strategy_mode(strategy.name)
        active_positions = self.positions if mode is RunMode.LIVE else self.paper_positions
        execution = self.execution if mode is RunMode.LIVE else self.paper_execution
        for event in strategy.poll_events(self.market):
            if self.store.event_seen(event.event_id, event.strategy, mode.value):
                continue
            try:
                if event.kind == "open" and event.signal is not None:
                    if mode is RunMode.LIVE and event.symbol in self.unmanaged_live_symbols:
                        logging.warning(
                            "event entry blocked strategy=%s symbol=%s "
                            "reason=unmanaged Binance position",
                            event.strategy, event.symbol,
                        )
                        self.store.save_event(
                            event, "processed", mode.value, strategy.version,
                        )
                        continue
                    gate = getattr(strategy, "event_entry_block_reason", None)
                    block_reason = gate(event, mode.value) if gate is not None else None
                    if block_reason is not None:
                        logging.info(
                            "event entry blocked strategy=%s symbol=%s reason=%s",
                            event.strategy, event.symbol, block_reason,
                        )
                        self.store.save_event(
                            event, "processed", mode.value, strategy.version,
                        )
                        continue
                    self.store.save_signal(event.signal, mode.value)
                    if mode is RunMode.SHADOW:
                        self.notifier.send(self.signal_message(event.signal))
                    elif ((event.strategy, event.symbol) not in active_positions
                          and (mode is not RunMode.LIVE
                               or not any(position.symbol == event.symbol
                                          for position in active_positions.values()))):
                        if execution is None:
                            raise RuntimeError(f"no execution service for {mode.value}")
                        execution.open(event.signal)
                elif event.kind == "close":
                    for position in list(active_positions.values()):
                        if (position.strategy == event.strategy and position.symbol == event.symbol
                                and position.side is event.side):
                            if execution is None:
                                raise RuntimeError(f"no execution service for {mode.value}")
                            execution.close(position, position.remaining_quantity, "lead_close",
                                            self.market.mark_price(position.symbol))
                elif event.kind.startswith("skip_"):
                    logging.info(
                        "event skipped strategy=%s symbol=%s reason=%s event_id=%s",
                        event.strategy, event.symbol, event.kind, event.event_id,
                    )
                self.store.save_event(event, "processed", mode.value, strategy.version)
            except ValueError as exc:
                if (mode is RunMode.LIVE and event.signal is not None
                        and self._is_allocation_limit(exc)):
                    self._open_position_limit_paper(event.signal)
                    self.store.save_event(
                        event, "processed", mode.value, strategy.version
                    )
                    logging.warning(
                        "event entry blocked strategy=%s symbol=%s reason=%s",
                        event.strategy, event.symbol, exc,
                    )
                else:
                    logging.exception(
                        "external event failed strategy=%s symbol=%s",
                        event.strategy, event.symbol,
                    )
                    self.store.save_event(
                        event, "error", mode.value, strategy.version
                    )
            except Exception:
                logging.exception("external event failed strategy=%s symbol=%s", event.strategy, event.symbol)
                self.store.save_event(event, "error", mode.value, strategy.version)

    def _refresh_unmanaged_live_positions(self) -> None:
        if self.config.mode is not RunMode.LIVE:
            return
        managed = {position.symbol for position in self.positions.values()}
        unmanaged = set(self.exchange.open_position_symbols()) - managed
        new_symbols = unmanaged - self.reported_unmanaged
        if new_symbols:
            message = ("[EXTERNAL POSITION] Skipping new live entries for: "
                       f"{', '.join(sorted(new_symbols))}")
            logging.warning(message)
            self.notifier.send(message)
        self.reported_unmanaged = unmanaged
        self.unmanaged_live_symbols = unmanaged

    @staticmethod
    def signal_message(signal: Any) -> str:
        return (f"[SIGNAL] {signal.strategy} {signal.symbol} {signal.side.value.upper()}\n"
                f"Price: {signal.signal_price}\nStop: {signal.stop_price}\nScore: {signal.score}\n"
                f"Reason: {signal.reason}")
