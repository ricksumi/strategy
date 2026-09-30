"""Paper strategy that confirms and reverses rapidly stopped live trades."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..core.models import Candle, ClosedTrade, Position, PositionAction, Side, Signal, StrategyEvent
from ..core.ports import MarketDataPort, TradeStorePort


FIVE_MINUTES_MS = 300_000


@dataclass(frozen=True)
class FastStopReversalSettings:
    source_strategies: tuple[str, ...] = (
        "bn-stra-top-gainers-exhaustion-short-1",
    )
    max_source_hold_minutes: int = 15
    max_observation_minutes: int = 30
    confirmation_candle_limit: int = 6
    structure_lookback: int = 3
    volume_lookback: int = 10
    min_volume_ratio: Decimal = Decimal("1.5")
    min_source_loss_r: Decimal = Decimal("0.6")
    retest_tolerance: Decimal = Decimal("0.003")
    max_entry_deviation: Decimal = Decimal("0.015")
    min_stop_distance: Decimal = Decimal("0.012")
    max_stop_roi: Decimal = Decimal("0.10")
    structure_buffer: Decimal = Decimal("0.002")
    leverage: int = 3
    profit_guard_roi: Decimal = Decimal("0.08")
    locked_roi: Decimal = Decimal("0.03")
    trailing_activation_roi: Decimal = Decimal("0.15")
    trailing_callback_price: Decimal = Decimal("0.012")
    tight_trailing_activation_roi: Decimal = Decimal("0.25")
    tight_trailing_callback_price: Decimal = Decimal("0.006")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FastStopReversalSettings":
        values: dict[str, Any] = {}
        for field_name, field_def in cls.__dataclass_fields__.items():
            if field_name not in raw:
                continue
            value = raw[field_name]
            if field_name == "source_strategies":
                values[field_name] = tuple(str(item) for item in value)
            elif isinstance(field_def.default, Decimal):
                values[field_name] = Decimal(str(value))
            else:
                values[field_name] = value
        settings = cls(**values)
        if not settings.source_strategies:
            raise ValueError("fast-stop reversal requires at least one source strategy")
        if (settings.max_source_hold_minutes < 1 or settings.max_observation_minutes < 5
                or settings.confirmation_candle_limit < 1):
            raise ValueError("fast-stop reversal time windows must be positive")
        if settings.structure_lookback < 2:
            raise ValueError("fast-stop reversal structure_lookback must be at least two")
        if settings.volume_lookback < 2 or settings.min_volume_ratio <= 0:
            raise ValueError("fast-stop reversal volume settings are invalid")
        if settings.min_source_loss_r <= 0 or settings.retest_tolerance < 0:
            raise ValueError("fast-stop reversal loss and retest settings are invalid")
        if not Decimal("0") < settings.max_stop_roi < Decimal("1"):
            raise ValueError("fast-stop reversal max_stop_roi must be between zero and one")
        return settings


class FastStopReversalStrategy:
    name = "bn-stra-fast-stop-reversal-1"
    version = "1.3.0"
    interval = "5m"
    candle_limit = 80

    def __init__(self, settings: FastStopReversalSettings, store: TradeStorePort) -> None:
        self.settings = settings
        self.store = store
        self._symbol_locks: dict[str, int] = {}

    def poll_events(self, market: MarketDataPort) -> Sequence[StrategyEvent]:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - self.settings.max_observation_minutes * 60_000
        candidates = self.store.recent_fast_stop_candidates(
            self.settings.source_strategies,
            start_ms,
            now_ms + 1,
            self.settings.max_source_hold_minutes * 60,
        )
        self._refresh_symbol_locks(candidates, now_ms)
        events: list[StrategyEvent] = []
        for source in candidates:
            event = self._candidate_event(source, market, now_ms)
            if event is not None:
                events.append(event)
        return events

    def entry_block_reason(
        self, strategy_name: str, symbol: str, now_ms: int,
    ) -> str | None:
        if strategy_name not in self.settings.source_strategies:
            return None
        locked_until = self._symbol_locks.get(symbol)
        if locked_until is None:
            return None
        if now_ms >= locked_until:
            self._symbol_locks.pop(symbol, None)
            return None
        remaining_seconds = max(1, (locked_until - now_ms + 999) // 1000)
        return f"fast-stop reversal exclusive lock ({remaining_seconds}s remaining)"

    def _refresh_symbol_locks(
        self, candidates: Sequence[ClosedTrade], now_ms: int,
    ) -> None:
        self._symbol_locks = {
            symbol: locked_until for symbol, locked_until in self._symbol_locks.items()
            if locked_until > now_ms
        }
        lock_ms = self.settings.max_observation_minutes * 60_000
        for source in candidates:
            locked_until = source.closed_at_ms + lock_ms
            if locked_until > now_ms:
                self._symbol_locks[source.symbol] = max(
                    locked_until, self._symbol_locks.get(source.symbol, 0)
                )

    def _candidate_event(
        self, source: ClosedTrade, market: MarketDataPort, now_ms: int,
    ) -> StrategyEvent | None:
        side = Side.LONG if source.side is Side.SHORT else Side.SHORT
        event_id = f"fast-stop-reversal:{source.trade_key}"
        day_start, day_end = self._day_bounds(now_ms)
        last_open = self.store.load_last_open_times(self.name, "paper").get(source.symbol)
        if last_open is not None and day_start <= last_open < day_end:
            return StrategyEvent(
                event_id, self.name, "skip_daily_limit", source.symbol, side, now_ms
            )

        source_loss_r = self._source_loss_r(source)
        if source_loss_r < self.settings.min_source_loss_r:
            return StrategyEvent(
                event_id, self.name, "skip_source_loss", source.symbol, side, now_ms
            )

        candles = market.candles(source.symbol, self.interval, self.candle_limit)
        confirmation_open = (source.closed_at_ms // FIVE_MINUTES_MS + 1) * FIVE_MINUTES_MS
        observation_deadline = (
            source.closed_at_ms + self.settings.max_observation_minutes * 60_000
        )
        confirmations = [
            item for item in candles
            if item.open_time_ms >= confirmation_open
            and item.close_time_ms <= min(now_ms, observation_deadline)
        ][:self.settings.confirmation_candle_limit]
        if not confirmations:
            if now_ms - source.closed_at_ms >= self.settings.max_observation_minutes * 60_000:
                return StrategyEvent(
                    event_id, self.name, "skip_expired", source.symbol, side, now_ms
                )
            return None
        breakout: Candle | None = None
        breakout_level: Decimal | None = None
        breakout_volume_ratio = Decimal("0")
        saw_stop_break = False
        saw_structure_break = False
        for confirmation in confirmations:
            prior_all = [item for item in candles if item.open_time_ms < confirmation.open_time_ms]
            structure_prior = prior_all[-self.settings.structure_lookback:]
            volume_prior = prior_all[-self.settings.volume_lookback:]
            if len(structure_prior) < self.settings.structure_lookback:
                continue
            average_volume = (
                sum((item.quote_volume for item in volume_prior), Decimal("0"))
                / Decimal(len(volume_prior)) if volume_prior else Decimal("0")
            )
            volume_ratio = (
                confirmation.quote_volume / average_volume if average_volume > 0 else Decimal("0")
            )
            stop_broken = (
                confirmation.close > source.initial_stop_price
                if side is Side.LONG else confirmation.close < source.initial_stop_price
            )
            directional_body = (
                confirmation.close > confirmation.open
                if side is Side.LONG else confirmation.close < confirmation.open
            )
            if not stop_broken or not directional_body:
                continue
            saw_stop_break = True
            level = (
                max(item.high for item in structure_prior)
                if side is Side.LONG else min(item.low for item in structure_prior)
            )
            structure_broken = (
                confirmation.close > level
                if side is Side.LONG else confirmation.close < level
            )
            if not structure_broken:
                continue
            saw_structure_break = True
            if volume_ratio < self.settings.min_volume_ratio:
                continue
            breakout = confirmation
            breakout_level = level
            breakout_volume_ratio = volume_ratio
            break

        observation_expired = (
            now_ms - source.closed_at_ms
            >= self.settings.max_observation_minutes * 60_000
        )
        reached_limit = len(confirmations) >= self.settings.confirmation_candle_limit
        if breakout is None:
            if not reached_limit and not observation_expired:
                return None
            reason = (
                "skip_volume" if saw_structure_break else
                "skip_structure" if saw_stop_break else "skip_stop_break"
            )
            return StrategyEvent(event_id, self.name, reason, source.symbol, side, now_ms)

        assert breakout_level is not None
        retests = [
            item for item in confirmations if item.open_time_ms > breakout.open_time_ms
        ]
        retest: Candle | None = None
        invalidated = False
        for candidate in retests:
            if self._retest_invalidated(candidate, side, source.initial_stop_price):
                invalidated = True
                break
            if self._is_confirmed_retest(candidate, side, breakout_level):
                retest = candidate
                break
        if invalidated:
            return StrategyEvent(
                event_id, self.name, "skip_retest_invalidated", source.symbol, side, now_ms
            )
        if retest is None:
            if not reached_limit and not observation_expired:
                return None
            return StrategyEvent(
                event_id, self.name, "skip_retest", source.symbol, side, now_ms
            )

        mark = market.mark_price(source.symbol)
        mark_confirmed = mark > breakout_level if side is Side.LONG else mark < breakout_level
        deviation = (
            abs(mark / retest.close - Decimal("1")) if retest.close > 0 else Decimal("999")
        )
        if not mark_confirmed:
            return StrategyEvent(
                event_id, self.name, "skip_mark_reclaimed", source.symbol, side, now_ms
            )
        if deviation > self.settings.max_entry_deviation:
            return StrategyEvent(
                event_id, self.name, "skip_deviation", source.symbol, side, now_ms
            )

        stop = self._stop(mark, side, retest, source.initial_stop_price)
        score = self._score(source, breakout, breakout_volume_ratio)
        signal = Signal(
            self.name, self.version, source.symbol, side, mark, stop, score,
            "structured_fast_stop_reversal", retest.close_time_ms,
            {
                "source_trade_key": source.trade_key,
                "source_strategy": source.strategy,
                "source_strategy_version": source.strategy_version,
                "source_side": source.side.value,
                "source_opened_at_ms": source.opened_at_ms,
                "source_closed_at_ms": source.closed_at_ms,
                "source_duration_seconds": source.duration_seconds,
                "source_net_pnl": str(source.net_pnl),
                "source_initial_stop_price": str(source.initial_stop_price),
                "source_loss_r": str(source_loss_r),
                "breakout_open_time_ms": breakout.open_time_ms,
                "breakout_close": str(breakout.close),
                "breakout_level": str(breakout_level),
                "breakout_volume_ratio": str(breakout_volume_ratio),
                "retest_open_time_ms": retest.open_time_ms,
                "retest_close": str(retest.close),
                "entry_deviation": str(deviation),
            },
        )
        return StrategyEvent(
            event_id, self.name, "open", source.symbol, side, retest.close_time_ms, signal,
        )

    def manage(
        self, position: Position, candles: Sequence[Candle], mark: Decimal,
    ) -> Iterable[PositionAction]:
        stopped = (
            mark <= position.stop_price if position.side is Side.LONG
            else mark >= position.stop_price
        )
        if stopped:
            reason = "protected_stop" if self._profitable_stop(position) else "stop_loss"
            return [PositionAction("close", reason, position.remaining_quantity)]

        best_return = (
            position.best_price / position.entry_price - Decimal("1")
            if position.side is Side.LONG
            else Decimal("1") - position.best_price / position.entry_price
        )
        best_roi = best_return * Decimal(position.leverage)
        candidates: list[tuple[Decimal, str]] = []
        if best_roi >= self.settings.profit_guard_roi:
            move = self.settings.locked_roi / Decimal(position.leverage)
            candidates.append((
                position.entry_price * (
                    Decimal("1") + move if position.side is Side.LONG else Decimal("1") - move
                ),
                "profit_guard",
            ))
        if best_roi >= self.settings.trailing_activation_roi:
            tight = best_roi >= self.settings.tight_trailing_activation_roi
            callback = (
                self.settings.tight_trailing_callback_price
                if tight else self.settings.trailing_callback_price
            )
            candidates.append((
                position.best_price * (
                    Decimal("1") - callback
                    if position.side is Side.LONG else Decimal("1") + callback
                ),
                "tight_trailing" if tight else "trailing",
            ))
        if not candidates:
            return []
        candidate, reason = (
            max(candidates, key=lambda item: item[0])
            if position.side is Side.LONG else min(candidates, key=lambda item: item[0])
        )
        improved = (
            candidate > position.stop_price if position.side is Side.LONG
            else candidate < position.stop_price
        )
        return [PositionAction("move_stop", reason, stop_price=candidate)] if improved else []

    def _is_confirmed_retest(
        self, candle: Candle, side: Side, breakout_level: Decimal,
    ) -> bool:
        tolerance = self.settings.retest_tolerance
        if side is Side.LONG:
            touched = candle.low <= breakout_level * (Decimal("1") + tolerance)
            return touched and candle.close > breakout_level and candle.close > candle.open
        touched = candle.high >= breakout_level * (Decimal("1") - tolerance)
        return touched and candle.close < breakout_level and candle.close < candle.open

    @staticmethod
    def _retest_invalidated(
        candle: Candle, side: Side, original_stop: Decimal,
    ) -> bool:
        return (
            candle.close < original_stop if side is Side.LONG
            else candle.close > original_stop
        )

    @staticmethod
    def _source_loss_r(source: ClosedTrade) -> Decimal:
        risk = abs(source.entry_price - source.initial_stop_price)
        if risk <= 0:
            return Decimal("0")
        loss = (
            source.entry_price - source.exit_price if source.side is Side.LONG
            else source.exit_price - source.entry_price
        )
        return max(Decimal("0"), loss / risk)

    def _stop(
        self, entry: Decimal, side: Side, candle: Candle, original_stop: Decimal,
    ) -> Decimal:
        maximum = self.settings.max_stop_roi / Decimal(self.settings.leverage)
        if side is Side.LONG:
            structure = min(candle.low, original_stop) * (
                Decimal("1") - self.settings.structure_buffer
            )
            distance = entry - structure
        else:
            structure = max(candle.high, original_stop) * (
                Decimal("1") + self.settings.structure_buffer
            )
            distance = structure - entry
        ratio = distance / entry if distance > 0 else Decimal("0")
        ratio = min(maximum, max(self.settings.min_stop_distance, ratio))
        return entry * (Decimal("1") - ratio if side is Side.LONG else Decimal("1") + ratio)

    @staticmethod
    def _score(
        source: ClosedTrade, candle: Candle, volume_ratio: Decimal,
    ) -> Decimal:
        candle_range = candle.high - candle.low
        body_ratio = (
            abs(candle.close - candle.open) / candle_range
            if candle_range > 0 else Decimal("0")
        )
        follow_through = (
            abs(candle.close / source.last_stop_price - Decimal("1"))
            if source.last_stop_price > 0 else Decimal("0")
        )
        return min(
            Decimal("100"),
            Decimal("45")
            + min(Decimal("25"), (volume_ratio - Decimal("1")) * Decimal("15"))
            + min(Decimal("20"), body_ratio * Decimal("25"))
            + min(Decimal("10"), follow_through * Decimal("500")),
        )

    @staticmethod
    def _profitable_stop(position: Position) -> bool:
        return (
            position.stop_price >= position.entry_price if position.side is Side.LONG
            else position.stop_price <= position.entry_price
        )

    @staticmethod
    def _day_bounds(now_ms: int) -> tuple[int, int]:
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime.fromtimestamp(now_ms / 1000, zone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
