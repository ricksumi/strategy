"""Flow-confirmed momentum ignition with a first-pullback long entry."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from threading import RLock
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..core.models import Candle, Position, PositionAction, Side, Signal
from ..core.ports import MarketDataPort
from .indicators import atr_ratio, ema


STABLECOINS = frozenset({
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT", "EURUSDT", "AEURUSDT",
    "BUSDUSDT", "USDPUSDT", "GUSDUSDT", "USTCUSDT",
})


@dataclass(frozen=True)
class MomentumIgnitionSettings:
    min_quote_volume: Decimal = Decimal("15000000")
    max_spread: Decimal = Decimal("0.0015")
    min_gain_24h: Decimal = Decimal("-0.05")
    max_gain_24h: Decimal = Decimal("0.50")
    min_return_1h: Decimal = Decimal("0.02")
    max_return_1h: Decimal = Decimal("0.12")
    min_return_4h: Decimal = Decimal("0.04")
    max_return_4h: Decimal = Decimal("0.25")
    min_relative_btc_1h: Decimal = Decimal("0.02")
    min_volume_ratio_15m: Decimal = Decimal("1.8")
    min_open_interest_change_15m: Decimal = Decimal("0.03")
    min_taker_buy_sell_ratio: Decimal = Decimal("1.2")
    max_funding_rate: Decimal = Decimal("0.0005")
    max_top_position_ratio: Decimal = Decimal("2")
    breakout_lookback: int = 20
    max_ema_distance_atr: Decimal = Decimal("1.5")
    prefilter_limit: int = 90
    flow_candidate_limit: int = 15
    active_candidate_limit: int = 5
    scan_workers: int = 12
    rerank_seconds: int = 60
    setup_expiry_minutes: int = 30
    pullback_trigger_atr: Decimal = Decimal("0.5")
    pullback_invalidation_atr: Decimal = Decimal("0.3")
    stop_buffer_atr: Decimal = Decimal("0.25")
    min_stop_distance: Decimal = Decimal("0.012")
    max_stop_distance: Decimal = Decimal("0.03")
    risk_reduction_r: Decimal = Decimal("0.6")
    reduced_risk_r: Decimal = Decimal("0.25")
    breakeven_r: Decimal = Decimal("1")
    breakeven_lock_r: Decimal = Decimal("0.1")
    profit_lock_r: Decimal = Decimal("1.5")
    locked_r: Decimal = Decimal("0.75")
    tp1_r: Decimal = Decimal("2")
    tp1_fraction: Decimal = Decimal("0.3")
    post_tp1_lock_r: Decimal = Decimal("1")
    trailing_activation_r: Decimal = Decimal("3")
    trailing_offset_r: Decimal = Decimal("1")
    tight_trailing_activation_r: Decimal = Decimal("5")
    tight_trailing_offset_r: Decimal = Decimal("0.5")
    max_losing_hold_hours: Decimal = Decimal("4")
    loss_cooldown_minutes: int = 30
    reentry_breakout_lookback: int = 3
    max_symbol_stop_losses_per_day: int = 2

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MomentumIgnitionSettings":
        values: dict[str, Any] = {}
        for field_name, field_def in cls.__dataclass_fields__.items():
            if field_name not in raw:
                continue
            value = raw[field_name]
            values[field_name] = (
                Decimal(str(value)) if isinstance(field_def.default, Decimal) else value
            )
        settings = cls(**values)
        if not Decimal("0") < settings.max_spread < Decimal("1"):
            raise ValueError("momentum ignition max_spread must be between zero and one")
        if not settings.min_return_1h < settings.max_return_1h:
            raise ValueError("momentum ignition 1h return range is invalid")
        if not settings.min_return_4h < settings.max_return_4h:
            raise ValueError("momentum ignition 4h return range is invalid")
        if (settings.breakout_lookback < 3 or settings.prefilter_limit < 1
                or settings.flow_candidate_limit < settings.active_candidate_limit
                or settings.active_candidate_limit < 1):
            raise ValueError("momentum ignition candidate limits are invalid")
        if settings.scan_workers < 1 or settings.scan_workers > 32:
            raise ValueError("momentum ignition scan_workers must be between 1 and 32")
        if not Decimal("0") < settings.min_stop_distance < settings.max_stop_distance:
            raise ValueError("momentum ignition stop range is invalid")
        if not Decimal("0") < settings.tp1_fraction < Decimal("1"):
            raise ValueError("momentum ignition tp1_fraction must be between zero and one")
        return settings


@dataclass(frozen=True)
class IgnitionSnapshot:
    symbol: str
    observed_at_ms: int
    breakout_close: Decimal
    breakout_level: Decimal
    atr_price: Decimal
    expires_at_ms: int
    score: Decimal
    metrics: Mapping[str, Any]
    rank: int = 0


@dataclass(frozen=True)
class PullbackTouch:
    touched_at_ms: int
    lowest_price: Decimal


class MomentumIgnitionLongStrategy:
    name = "bn-stra-momentum-ignition-long-1"
    version = "1.0.0"
    interval = "5m"
    candle_limit = 200
    background_universe_refresh = True

    def __init__(self, settings: MomentumIgnitionSettings) -> None:
        self.settings = settings
        self.universe_refresh_seconds = settings.rerank_seconds
        self._snapshots: dict[str, IgnitionSnapshot] = {}
        self._touches: dict[str, PullbackTouch] = {}
        self._last_open_ms: dict[str, int] = {}
        self._lock = RLock()

    def select_universe(self, market: MarketDataPort) -> Sequence[str]:
        now_ms = int(time.time() * 1000)
        self._discard_expired(now_ms)
        summaries = self._prefilter(market)
        btc = market.candles("BTCUSDT", self.interval, self.candle_limit)
        if len(btc) < 60:
            with self._lock:
                return list(self._snapshots)
        btc_1h = btc[-1].close / btc[-12].open - Decimal("1")
        with ThreadPoolExecutor(max_workers=self.settings.scan_workers) as executor:
            technical = list(executor.map(
                lambda row: self._technical_candidate(market, row, btc_1h), summaries,
            ))
        ranked = sorted(
            (item for item in technical if item is not None),
            key=lambda item: (item.score, item.symbol), reverse=True,
        )[:self.settings.flow_candidate_limit]
        with ThreadPoolExecutor(max_workers=self.settings.scan_workers) as executor:
            flowed = list(executor.map(lambda item: self._apply_flow(market, item), ranked))
        qualified = sorted(
            (item for item in flowed if item is not None),
            key=lambda item: (item.score, item.symbol), reverse=True,
        )
        with self._lock:
            available = self.settings.active_candidate_limit - len(self._snapshots)
            existing = set(self._snapshots)
        for rank, snapshot in enumerate(qualified, start=1):
            if available <= 0:
                break
            if snapshot.symbol in existing or self._cooling_down(snapshot.symbol, now_ms):
                continue
            snapshot = replace(snapshot, rank=rank)
            with self._lock:
                self._snapshots[snapshot.symbol] = snapshot
            existing.add(snapshot.symbol)
            available -= 1
            logging.info(
                "momentum ignition registered rank=%s symbol=%s score=%s breakout=%s "
                "level=%s expires_at_ms=%s",
                rank, snapshot.symbol, snapshot.score, snapshot.breakout_close,
                snapshot.breakout_level, snapshot.expires_at_ms,
            )
        with self._lock:
            return list(self._snapshots)

    def evaluate(
        self, symbol: str, candles: Sequence[Candle], mark: Decimal,
    ) -> Optional[Signal]:
        with self._lock:
            snapshot = self._snapshots.get(symbol)
            touch = self._touches.get(symbol)
        if snapshot is None:
            return None
        now_ms = int(time.time() * 1000)
        if now_ms > snapshot.expires_at_ms:
            self._discard(symbol, "expired")
            return None
        invalidation = (
            snapshot.breakout_level
            - snapshot.atr_price * self.settings.pullback_invalidation_atr
        )
        if mark < invalidation or candles[-1].close < invalidation:
            self._discard(symbol, "pullback_invalidated")
            return None
        trigger = (
            snapshot.breakout_close
            - snapshot.atr_price * self.settings.pullback_trigger_atr
        )
        if touch is None:
            if mark > trigger:
                return None
            touch = PullbackTouch(now_ms, mark)
            with self._lock:
                self._touches[symbol] = touch
            logging.info(
                "momentum ignition pullback touched symbol=%s mark=%s trigger=%s",
                symbol, mark, trigger,
            )
            return None

        touch = PullbackTouch(touch.touched_at_ms, min(touch.lowest_price, mark))
        with self._lock:
            self._touches[symbol] = touch
        closed_after_touch = [
            candle for candle in candles if candle.close_time_ms > touch.touched_at_ms
        ]
        if len(closed_after_touch) < 2:
            return None
        confirmations = closed_after_touch[-2:]
        if not all(candle.close > snapshot.breakout_level for candle in confirmations):
            return None
        if mark < snapshot.breakout_level:
            return None
        atr = atr_ratio(candles)
        ema20 = ema([candle.close for candle in candles], 20)[-1]
        if atr is None or ema20 is None or atr <= 0:
            return None
        distance_atr = (mark - ema20) / (mark * atr)
        if distance_atr > self.settings.max_ema_distance_atr:
            return None
        structure_stop = min(touch.lowest_price, snapshot.breakout_level) - (
            snapshot.atr_price * self.settings.stop_buffer_atr
        )
        stop_distance = (mark - structure_stop) / mark
        if stop_distance > self.settings.max_stop_distance:
            self._discard(symbol, "stop_too_wide")
            return None
        stop_distance = max(self.settings.min_stop_distance, stop_distance)
        stop = mark * (Decimal("1") - stop_distance)
        metrics = {
            **snapshot.metrics,
            "rank": snapshot.rank,
            "breakout_close": str(snapshot.breakout_close),
            "breakout_level": str(snapshot.breakout_level),
            "pullback_trigger": str(trigger),
            "pullback_low": str(touch.lowest_price),
            "confirmation_1_close": str(confirmations[0].close),
            "confirmation_2_close": str(confirmations[1].close),
            "entry_ema_distance_atr": str(distance_atr),
            "stop_distance": str(stop_distance),
            "score_model_version": "momentum-ignition-long-v1",
            "score_breakdown": snapshot.metrics.get("score_breakdown", {}),
        }
        return Signal(
            self.name, self.version, symbol, Side.LONG, mark, stop, snapshot.score,
            "flow_confirmed_breakout_pullback", now_ms, metrics,
        )

    def entry_scan_token(self, symbol: str, candles: Sequence[Candle]) -> int:
        with self._lock:
            active = symbol in self._snapshots
        return int(time.time() * 1000) // 60_000 if active else 0

    def record_open(self, symbol: str, observed_at_ms: int) -> None:
        self._last_open_ms[symbol] = int(time.time() * 1000)
        self._discard(symbol, "opened")

    def discard_entry(self, symbol: str, reason: str) -> None:
        self._discard(symbol, reason)

    def restore_open_times(self, values: Mapping[str, int]) -> None:
        self._last_open_ms.update(values)

    def manage(
        self, position: Position, candles: Sequence[Candle], mark: Decimal,
    ) -> Iterable[PositionAction]:
        risk = position.initial_risk_distance or abs(
            position.entry_price - (position.initial_stop_price or position.stop_price)
        )
        if risk <= 0:
            return []
        current_r = (mark - position.entry_price) / risk
        best_r = (position.best_price - position.entry_price) / risk
        if mark <= position.stop_price:
            reason = "protected_stop" if position.stop_price >= position.entry_price else "stop_loss"
            return [PositionAction("close", reason, position.remaining_quantity)]
        tp1_done = 1 in position.partial_tiers_done or position.remaining_quantity < position.quantity
        if not tp1_done and current_r >= self.settings.tp1_r:
            return [PositionAction(
                "close", "take_profit_1", position.quantity * self.settings.tp1_fraction, tier=1,
            )]
        elapsed = int(time.time() * 1000) - position.opened_at_ms
        if current_r <= 0 and elapsed >= int(self.settings.max_losing_hold_hours * Decimal("3600000")):
            return [PositionAction("close", "time_stop", position.remaining_quantity)]
        candidates: list[tuple[Decimal, str]] = []
        if best_r >= self.settings.risk_reduction_r:
            candidates.append((
                position.entry_price - risk * self.settings.reduced_risk_r,
                "risk_reduction",
            ))
        if best_r >= self.settings.breakeven_r:
            candidates.append((
                position.entry_price + risk * self.settings.breakeven_lock_r,
                "breakeven",
            ))
        if best_r >= self.settings.profit_lock_r:
            candidates.append((
                position.entry_price + risk * self.settings.locked_r,
                "profit_lock",
            ))
        if tp1_done:
            candidates.append((
                position.entry_price + risk * self.settings.post_tp1_lock_r,
                "post_tp1_lock",
            ))
        if best_r >= self.settings.trailing_activation_r:
            offset = (
                self.settings.tight_trailing_offset_r
                if best_r >= self.settings.tight_trailing_activation_r
                else self.settings.trailing_offset_r
            )
            candidates.append((position.best_price - risk * offset, "trailing"))
        if not candidates:
            return []
        candidate, reason = max(candidates, key=lambda item: item[0])
        if candidate <= position.stop_price:
            return []
        if mark <= candidate:
            return [PositionAction("close", "protected_stop", position.remaining_quantity)]
        return [PositionAction("move_stop", reason, stop_price=candidate)]

    def _prefilter(self, market: MarketDataPort) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        instruments = market.instruments()
        for row in market.market_summaries():
            try:
                symbol = str(row["symbol"])
                if symbol not in instruments or self._denied(symbol):
                    continue
                volume = Decimal(str(row["quoteVolume"]))
                last = Decimal(str(row["lastPrice"]))
                bid = Decimal(str(row["bidPrice"]))
                ask = Decimal(str(row["askPrice"]))
                gain = Decimal(str(row["priceChangePercent"])) / Decimal("100")
                spread = (ask - bid) / last if last > 0 else Decimal("999")
                if (volume < self.settings.min_quote_volume
                        or spread > self.settings.max_spread
                        or not self.settings.min_gain_24h <= gain <= self.settings.max_gain_24h):
                    continue
                rows.append(row)
            except (KeyError, ValueError, ArithmeticError):
                continue
        by_gain = sorted(rows, key=lambda row: Decimal(str(row["priceChangePercent"])), reverse=True)
        by_volume = sorted(rows, key=lambda row: Decimal(str(row["quoteVolume"])), reverse=True)
        selected: dict[str, Mapping[str, Any]] = {}
        half = max(1, self.settings.prefilter_limit // 2)
        for row in [*by_gain[:half], *by_volume[:half]]:
            selected[str(row["symbol"])] = row
        return list(selected.values())[:self.settings.prefilter_limit]

    def _technical_candidate(
        self, market: MarketDataPort, row: Mapping[str, Any], btc_1h: Decimal,
    ) -> Optional[IgnitionSnapshot]:
        try:
            symbol = str(row["symbol"])
            candles = market.candles(symbol, self.interval, self.candle_limit)
            if len(candles) < 80:
                return None
            latest = candles[-1]
            return_1h = latest.close / candles[-12].open - Decimal("1")
            return_4h = latest.close / candles[-48].open - Decimal("1")
            relative = return_1h - btc_1h
            if (not self.settings.min_return_1h <= return_1h <= self.settings.max_return_1h
                    or not self.settings.min_return_4h <= return_4h <= self.settings.max_return_4h
                    or relative < self.settings.min_relative_btc_1h):
                return None
            closes = [candle.close for candle in candles]
            ema20_values, ema60_values = ema(closes, 20), ema(closes, 60)
            ema20_now, ema60_now = ema20_values[-1], ema60_values[-1]
            ema20_old, ema60_old = ema20_values[-4], ema60_values[-4]
            atr = atr_ratio(candles)
            if None in (ema20_now, ema60_now, ema20_old, ema60_old, atr) or atr is None:
                return None
            if not (ema20_now > ema60_now and ema20_now > ema20_old and ema60_now >= ema60_old):
                return None
            atr_price = latest.close * atr
            if atr_price <= 0 or (latest.close - ema20_now) / atr_price > self.settings.max_ema_distance_atr:
                return None
            recent = candles[-3:]
            previous = candles[-15:-3]
            recent_avg = sum((item.quote_volume for item in recent), Decimal("0")) / Decimal(3)
            prior_avg = sum((item.quote_volume for item in previous), Decimal("0")) / Decimal(len(previous))
            volume_ratio = recent_avg / prior_avg if prior_avg > 0 else Decimal("0")
            if volume_ratio < self.settings.min_volume_ratio_15m:
                return None
            higher_low = min(item.low for item in candles[-3:]) > min(
                item.low for item in candles[-6:-3]
            )
            breakout_level = max(
                item.high for item in candles[-self.settings.breakout_lookback - 1:-1]
            )
            if not higher_low or latest.close <= breakout_level:
                return None
            score = self._technical_score(return_1h, return_4h, relative, volume_ratio)
            now_ms = int(time.time() * 1000)
            return IgnitionSnapshot(
                symbol, now_ms, latest.close, breakout_level, atr_price,
                now_ms + self.settings.setup_expiry_minutes * 60_000, score,
                {
                    "return_1h": str(return_1h),
                    "return_4h": str(return_4h),
                    "relative_btc_1h": str(relative),
                    "volume_ratio_15m": str(volume_ratio),
                },
            )
        except (KeyError, ValueError, ArithmeticError, RuntimeError):
            return None

    def _apply_flow(
        self, market: MarketDataPort, snapshot: IgnitionSnapshot,
    ) -> Optional[IgnitionSnapshot]:
        try:
            flow = market.futures_flow(snapshot.symbol)
            oi = Decimal(flow["open_interest_change_15m"])
            taker = Decimal(flow["taker_buy_sell_ratio_15m"])
            funding = Decimal(flow["funding_rate"])
            top_ratio = Decimal(flow["top_position_ratio"])
            if (oi < self.settings.min_open_interest_change_15m
                    or taker < self.settings.min_taker_buy_sell_ratio
                    or funding > self.settings.max_funding_rate
                    or top_ratio > self.settings.max_top_position_ratio):
                return None
            flow_score = min(Decimal("12"), oi * Decimal("200")) + min(
                Decimal("10"), (taker - Decimal("1")) * Decimal("20")
            )
            breakdown = {
                "technical": str(snapshot.score),
                "open_interest": str(min(Decimal("12"), oi * Decimal("200"))),
                "taker_flow": str(min(Decimal("10"), (taker - Decimal("1")) * Decimal("20"))),
            }
            metrics = {
                **snapshot.metrics,
                **{name: str(value) for name, value in flow.items()},
                "score_breakdown": breakdown,
            }
            return replace(snapshot, score=min(Decimal("99"), snapshot.score + flow_score), metrics=metrics)
        except (KeyError, ValueError, ArithmeticError, RuntimeError):
            return None

    @staticmethod
    def _technical_score(
        return_1h: Decimal, return_4h: Decimal, relative: Decimal, volume_ratio: Decimal,
    ) -> Decimal:
        return min(
            Decimal("77"),
            Decimal("20")
            + min(Decimal("18"), return_1h * Decimal("150"))
            + min(Decimal("16"), return_4h * Decimal("60"))
            + min(Decimal("13"), relative * Decimal("200"))
            + min(Decimal("10"), (volume_ratio - Decimal("1")) * Decimal("8")),
        )

    def _discard_expired(self, now_ms: int) -> None:
        with self._lock:
            expired = [
                symbol for symbol, item in self._snapshots.items()
                if now_ms > item.expires_at_ms
            ]
        for symbol in expired:
            self._discard(symbol, "expired")

    def _discard(self, symbol: str, reason: str) -> None:
        with self._lock:
            snapshot = self._snapshots.pop(symbol, None)
            self._touches.pop(symbol, None)
        if snapshot is not None:
            logging.info(
                "momentum ignition discarded symbol=%s reason=%s score=%s",
                symbol, reason, snapshot.score,
            )

    def _cooling_down(self, symbol: str, now_ms: int) -> bool:
        last = self._last_open_ms.get(symbol)
        return last is not None and now_ms - last < self.settings.loss_cooldown_minutes * 60_000

    @staticmethod
    def _denied(symbol: str) -> bool:
        return (
            symbol in STABLECOINS or symbol.endswith("UPUSDT")
            or symbol.endswith("DOWNUSDT") or symbol.endswith("USDC")
        )
