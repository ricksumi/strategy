"""Top-three four-hour momentum strategy based on the CZ gainers specification."""

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


STABLECOIN_PAIRS = frozenset({
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT", "EURUSDT", "AEURUSDT",
    "BUSDUSDT", "USDPUSDT", "GUSDUSDT", "USTCUSDT",
})
SUFFIX_ALLOWLIST = frozenset({"JUPUSDT", "SYRUPUSDT"})


@dataclass(frozen=True)
class CzGainersSettings:
    min_gain_24h: Decimal = Decimal("-0.15")
    max_gain_24h: Decimal = Decimal("5")
    min_quote_volume: Decimal = Decimal("10000000")
    max_spread: Decimal = Decimal("0.0015")
    momentum_4h_min: Decimal = Decimal("0.05")
    stop_distance: Decimal = Decimal("0.02")
    tp1_multiple: Decimal = Decimal("2.25")
    tp1_fraction: Decimal = Decimal("0.5")
    runner_target_r: Decimal = Decimal("9")
    breakeven_r: Decimal = Decimal("1.5")
    breakeven_buffer: Decimal = Decimal("0.004")
    profit_lock_r: Decimal = Decimal("2")
    locked_r: Decimal = Decimal("1")
    post_tp1_lock_r: Decimal = Decimal("1")
    trailing_activation_r: Decimal = Decimal("5")
    trailing_offset_r: Decimal = Decimal("1.5")
    early_profit_guards_enabled: bool = True
    early_profit_guard_roi: Decimal = Decimal("0.10")
    early_locked_roi: Decimal = Decimal("0.02")
    second_profit_guard_roi: Decimal = Decimal("0.20")
    second_locked_roi: Decimal = Decimal("0.08")
    max_losing_hold_hours: Decimal = Decimal("4")
    cooldown_minutes: int = 30
    loss_cooldown_minutes: int = 15
    reentry_breakout_lookback: int = 3
    max_symbol_stop_losses_per_day: int = 2
    scan_workers: int = 12
    rerank_seconds: int = 60
    raw_rank_limit: int = 10
    active_candidate_limit: int = 3
    pullback_min: Decimal = Decimal("0.02")
    pullback_max: Decimal = Decimal("0.04")
    pullback_window_minutes: int = 15
    pullback_rearm_minutes: int = 15

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CzGainersSettings":
        values: dict[str, Any] = {}
        for field_name, field_def in cls.__dataclass_fields__.items():
            if field_name not in raw:
                continue
            value = raw[field_name]
            values[field_name] = (Decimal(str(value))
                                  if isinstance(field_def.default, Decimal) else value)
        settings = cls(**values)
        if not Decimal("0") < settings.stop_distance < Decimal("1"):
            raise ValueError("stop_distance must be between 0 and 1")
        if not Decimal("0") < settings.tp1_fraction < Decimal("1"):
            raise ValueError("tp1_fraction must be between 0 and 1")
        if settings.scan_workers < 1 or settings.scan_workers > 32:
            raise ValueError("scan_workers must be between 1 and 32")
        if (settings.loss_cooldown_minutes < 0 or settings.reentry_breakout_lookback < 1
                or settings.max_symbol_stop_losses_per_day < 1):
            raise ValueError("loss re-entry settings are invalid")
        if not (Decimal("0") < settings.early_locked_roi < settings.early_profit_guard_roi
                < settings.second_profit_guard_roi):
            raise ValueError("early profit guard settings are invalid")
        if not settings.early_locked_roi < settings.second_locked_roi < settings.second_profit_guard_roi:
            raise ValueError("second profit guard settings are invalid")
        if not (Decimal("0") <= settings.locked_r <= settings.post_tp1_lock_r
                < settings.tp1_multiple < settings.runner_target_r):
            raise ValueError("profit-lock R levels are invalid")
        if not Decimal("0") < settings.pullback_min < settings.pullback_max < Decimal("1"):
            raise ValueError("pullback range is invalid")
        if (settings.rerank_seconds < 15 or settings.pullback_window_minutes < 1
                or settings.pullback_rearm_minutes < 0):
            raise ValueError("pullback timing settings are invalid")
        if (settings.raw_rank_limit < settings.active_candidate_limit
                or settings.active_candidate_limit < 1):
            raise ValueError("CZ candidate limits are invalid")
        return settings


@dataclass(frozen=True)
class CandidateSnapshot:
    symbol: str
    observed_at_ms: int
    signal_price: Decimal
    momentum_4h: Decimal
    volume_ratio: Decimal
    consecutive_up: int
    momentum_30m: Decimal
    momentum_1h: Decimal
    off_high: Decimal
    expires_at_ms: int
    rank: int = 0


@dataclass(frozen=True)
class PullbackTouch:
    touched_at_ms: int
    deepest_pullback: Decimal
    lowest_price: Decimal


class CzGainersStrategy:
    name = "cz-gainers-long"
    version = "2.3.0"
    interval = "5m"
    candle_limit = 80
    background_universe_refresh = True

    def __init__(self, settings: CzGainersSettings) -> None:
        self.settings = settings
        self._snapshots: dict[str, CandidateSnapshot] = {}
        self._pullback_touches: dict[str, PullbackTouch] = {}
        self._last_open_ms: dict[str, int] = {}
        self._rearm_at_ms: dict[str, int] = {}
        self._lock = RLock()
        self.universe_refresh_seconds = settings.rerank_seconds

    def select_universe(self, market: MarketDataPort) -> Sequence[str]:
        """Track the top filtered closed-bar candidates for bounded pullbacks."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            expired = [
                symbol for symbol, snapshot in self._snapshots.items()
                if now_ms > snapshot.expires_at_ms
            ]
        for symbol in expired:
            self._discard_snapshot(symbol, "expired", now_ms)
        instruments = market.instruments()
        eligible: list[str] = []
        for row in market.market_summaries():
            try:
                symbol = str(row["symbol"])
                if symbol not in instruments or self._denied(symbol):
                    continue
                quote_volume = Decimal(str(row["quoteVolume"]))
                gain = Decimal(str(row["priceChangePercent"])) / Decimal("100")
                last = Decimal(str(row["lastPrice"]))
                bid = Decimal(str(row["bidPrice"]))
                ask = Decimal(str(row["askPrice"]))
                spread = (ask - bid) / last if last > 0 else Decimal("999")
                if (quote_volume < self.settings.min_quote_volume
                        or not self.settings.min_gain_24h <= gain <= self.settings.max_gain_24h
                        or spread > self.settings.max_spread):
                    continue
                eligible.append(symbol)
            except (KeyError, ValueError, ArithmeticError):
                continue
        with ThreadPoolExecutor(max_workers=self.settings.scan_workers) as executor:
            snapshots = executor.map(lambda item: self._rank_candidate(market, item), eligible)
            ranked = [item for item in snapshots if item is not None]
        if not ranked:
            with self._lock:
                return list(self._snapshots)

        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        shortlist = ranked[:self.settings.raw_rank_limit]
        with ThreadPoolExecutor(max_workers=self.settings.scan_workers) as executor:
            gated = list(executor.map(
                lambda item: self._gate_candidate(
                    market, item[1], item[2], item[0],
                ),
                shortlist,
            ))
        qualified = sorted(
            (item for item in gated if item is not None),
            key=lambda item: (item.momentum_4h, item.symbol), reverse=True,
        )
        with self._lock:
            available = self.settings.active_candidate_limit - len(self._snapshots)
            existing = set(self._snapshots)
        for rank, snapshot in enumerate(qualified, start=1):
            if available <= 0:
                break
            symbol = snapshot.symbol
            if (symbol in existing or now_ms < self._rearm_at_ms.get(symbol, 0)
                    or self._cooling_down(symbol, snapshot.observed_at_ms)):
                continue
            snapshot = replace(snapshot, rank=rank)
            with self._lock:
                if symbol in self._snapshots:
                    continue
                self._snapshots[symbol] = snapshot
            existing.add(symbol)
            available -= 1
            logging.info(
                "CZ pullback registered rank=%s symbol=%s signal_price=%s momentum_4h=%s "
                "trigger=%s invalidation=%s expires_at_ms=%s",
                snapshot.rank, symbol, snapshot.signal_price, snapshot.momentum_4h,
                snapshot.signal_price * (Decimal("1") - self.settings.pullback_min),
                snapshot.signal_price * (Decimal("1") - self.settings.pullback_max),
                snapshot.expires_at_ms,
            )
        with self._lock:
            return list(self._snapshots)

    def evaluate(self, symbol: str, candles: Sequence[Candle], mark: Decimal) -> Optional[Signal]:
        with self._lock:
            snapshot = self._snapshots.get(symbol)
        now_ms = int(time.time() * 1000)
        if snapshot is None or snapshot.symbol != symbol:
            return None
        if now_ms > snapshot.expires_at_ms:
            self._discard_snapshot(symbol, "expired", now_ms)
            return None
        if snapshot.signal_price <= 0:
            return None
        pullback = Decimal("1") - mark / snapshot.signal_price
        if pullback >= self.settings.pullback_max:
            self._discard_snapshot(symbol, "pullback_invalidated", now_ms)
            return None
        trigger_price = snapshot.signal_price * (Decimal("1") - self.settings.pullback_min)
        with self._lock:
            touch = self._pullback_touches.get(symbol)
        if touch is None:
            if pullback < self.settings.pullback_min:
                return None
            with self._lock:
                self._pullback_touches[symbol] = PullbackTouch(now_ms, pullback, mark)
            logging.info(
                "CZ pullback touched symbol=%s mark=%s pullback=%s; awaiting 5m reclaim",
                symbol, mark, pullback,
            )
            return None

        touch = PullbackTouch(
            touch.touched_at_ms,
            max(touch.deepest_pullback, pullback),
            min(touch.lowest_price, mark),
        )
        with self._lock:
            self._pullback_touches[symbol] = touch
        if len(candles) < 2:
            return None
        latest, previous = candles[-1], candles[-2]
        reclaimed = (
            latest.close_time_ms > touch.touched_at_ms
            and latest.low > previous.low
            and latest.close >= trigger_price
            and mark >= trigger_price
        )
        if not reclaimed:
            return None
        stop = mark * (Decimal("1") - self.settings.stop_distance)
        score, score_breakdown = self._score(snapshot, touch.deepest_pullback)
        return Signal(
            self.name, self.version, symbol, Side.LONG, mark, stop, score,
            "top3_4h_pullback_continuation", now_ms,
            {
                "momentum_4h": str(snapshot.momentum_4h),
                "rank": snapshot.rank,
                "registered_at_ms": snapshot.observed_at_ms,
                "registered_price": str(snapshot.signal_price),
                "pullback": str(touch.deepest_pullback),
                "pullback_low": str(touch.lowest_price),
                "pullback_touched_at_ms": touch.touched_at_ms,
                "reclaim_close": str(latest.close),
                "volume_ratio_15m": str(snapshot.volume_ratio),
                "consecutive_up": snapshot.consecutive_up,
                "momentum_30m": str(snapshot.momentum_30m),
                "momentum_1h": str(snapshot.momentum_1h),
                "off_high": str(snapshot.off_high),
                "score_model_version": "cz-gainers-v5",
                "score_breakdown": score_breakdown,
                "tp1_price": str(mark * (Decimal("1") + self.settings.stop_distance
                                           * self.settings.tp1_multiple)),
                "runner_target_price": str(
                    mark * (Decimal("1") + self.settings.stop_distance
                            * self.settings.runner_target_r)
                ),
                "runner_trailing_activation_price": str(
                    mark * (Decimal("1") + self.settings.stop_distance
                            * self.settings.trailing_activation_r)
                ),
            },
        )

    def entry_scan_token(self, symbol: str, candles: Sequence[Candle]) -> int:
        with self._lock:
            snapshot = self._snapshots.get(symbol)
        return int(time.time() * 1000) // 60_000 if snapshot and snapshot.symbol == symbol else 0

    def record_open(self, symbol: str, observed_at_ms: int) -> None:
        self._last_open_ms[symbol] = int(time.time() * 1000)
        with self._lock:
            self._snapshots.pop(symbol, None)
            self._pullback_touches.pop(symbol, None)

    def discard_entry(self, symbol: str, reason: str) -> None:
        self._discard_snapshot(symbol, reason, int(time.time() * 1000))

    def restore_open_times(self, values: Mapping[str, int]) -> None:
        self._last_open_ms.update(values)

    def manage(self, position: Position, candles: Sequence[Candle], mark: Decimal) -> Iterable[PositionAction]:
        initial_risk = position.entry_price * self.settings.stop_distance
        current_r = (mark - position.entry_price) / initial_risk
        best_r = (position.best_price - position.entry_price) / initial_risk
        best_roi = ((position.best_price - position.entry_price) / position.entry_price
                    * Decimal(position.leverage))
        tp1 = position.entry_price + initial_risk * self.settings.tp1_multiple

        if mark <= position.stop_price:
            reason = "protected_stop" if position.stop_price >= position.entry_price else "stop_loss"
            return [PositionAction("close", reason, position.remaining_quantity)]
        tp1_done = 1 in position.partial_tiers_done or position.remaining_quantity < position.quantity
        if tp1_done and best_r >= self.settings.runner_target_r:
            return [PositionAction("close", "take_profit_2", position.remaining_quantity, tier=2)]
        if not tp1_done and mark >= tp1:
            return [PositionAction("close", "take_profit_1",
                                   position.quantity * self.settings.tp1_fraction, tier=1)]

        elapsed_ms = int(time.time() * 1000) - position.opened_at_ms
        if (current_r <= 0 and elapsed_ms >= int(self.settings.max_losing_hold_hours
                                                 * Decimal("3600000"))):
            return [PositionAction("close", "time_stop", position.remaining_quantity)]

        stop_candidates: list[tuple[Decimal, str]] = []
        if self.settings.early_profit_guards_enabled and best_roi >= self.settings.early_profit_guard_roi:
            stop_candidates.append((
                position.entry_price * (
                    Decimal("1") + self.settings.early_locked_roi / Decimal(position.leverage)
                ),
                "early_profit_guard",
            ))
        if self.settings.early_profit_guards_enabled and best_roi >= self.settings.second_profit_guard_roi:
            stop_candidates.append((
                position.entry_price * (
                    Decimal("1") + self.settings.second_locked_roi / Decimal(position.leverage)
                ),
                "second_profit_guard",
            ))
        if best_r >= self.settings.breakeven_r:
            stop_candidates.append((position.entry_price * (Decimal("1")
                                                             + self.settings.breakeven_buffer),
                                    "breakeven"))
        if tp1_done:
            stop_candidates.append((position.entry_price
                                    + initial_risk * self.settings.post_tp1_lock_r,
                                    "post_tp1_lock"))
        if best_r >= self.settings.profit_lock_r:
            stop_candidates.append((position.entry_price + initial_risk * self.settings.locked_r,
                                    "profit_lock"))
        if best_r >= self.settings.trailing_activation_r:
            stop_candidates.append((
                position.best_price - initial_risk * self.settings.trailing_offset_r,
                "trailing",
            ))
        if stop_candidates:
            candidate, reason = max(stop_candidates, key=lambda item: item[0])
            if candidate > position.stop_price:
                if mark <= candidate:
                    return [PositionAction("close", "protected_stop", position.remaining_quantity)]
                return [PositionAction("move_stop", reason, stop_price=candidate)]
        return []

    def _gate_candidate(
        self, market: MarketDataPort, symbol: str, candles: Sequence[Candle], momentum: Decimal,
    ) -> Optional[CandidateSnapshot]:
        if momentum < self.settings.momentum_4h_min:
            return None
        latest = candles[-1]
        now_ms = int(time.time() * 1000)
        previous_volume = candles[-2].quote_volume
        volume_ratio = (latest.quote_volume / previous_volume
                        if previous_volume > 0 else Decimal("0"))
        consecutive_up = 0
        for candle in reversed(candles):
            if candle.close <= candle.open:
                break
            consecutive_up += 1
        closed_30m = market.candles(symbol, "30m", 48)
        closed_1h = market.candles(symbol, "1h", 4)
        if len(closed_30m) < 48 or len(closed_1h) < 4:
            return None
        momentum_30m = closed_30m[-1].close / closed_30m[-4].open - Decimal("1")
        momentum_1h = closed_1h[-1].close / closed_1h[-4].open - Decimal("1")
        high_24h = max([latest.high, *(candle.high for candle in closed_30m[-48:])])
        off_high = latest.close / high_24h - Decimal("1") if high_24h > 0 else Decimal("0")
        if momentum_30m < 0 or momentum_1h < 0:
            return None
        return CandidateSnapshot(
            symbol, now_ms, market.mark_price(symbol), momentum, volume_ratio,
            consecutive_up, momentum_30m, momentum_1h, off_high,
            now_ms + self.settings.pullback_window_minutes * 60_000,
        )

    def _rank_candidate(
        self, market: MarketDataPort, symbol: str,
    ) -> Optional[tuple[Decimal, str, Sequence[Candle]]]:
        try:
            candles = market.candles(symbol, "15m", 16)
            if len(candles) < 16:
                return None
            price = self._effective_latest_price(candles)
            start = candles[-16].open
            if start <= 0:
                return None
            return price / start - Decimal("1"), symbol, candles
        except (KeyError, ValueError, ArithmeticError, RuntimeError):
            return None

    def _cooling_down(self, symbol: str, now_ms: int) -> bool:
        last = self._last_open_ms.get(symbol)
        return last is not None and now_ms - last < self.settings.cooldown_minutes * 60_000

    @staticmethod
    def _score(
        snapshot: CandidateSnapshot, pullback: Decimal,
    ) -> tuple[Decimal, dict[str, str]]:
        momentum = min(Decimal("35"), max(
            Decimal("0"), (snapshot.momentum_4h - Decimal("0.05")) * Decimal("140"),
        ))
        pullback_quality = max(
            Decimal("0"), Decimal("25") - abs(pullback - Decimal("0.025")) * Decimal("1000"),
        )
        short_term = min(Decimal("12"), max(
            Decimal("0"), snapshot.momentum_30m * Decimal("100"),
        ))
        hourly = min(Decimal("13"), max(
            Decimal("0"), snapshot.momentum_1h * Decimal("50"),
        ))
        components = {
            "rank_quality": max(
                Decimal("0"), Decimal("13") - Decimal(snapshot.rank) * Decimal("3"),
            ),
            "momentum_4h": momentum,
            "pullback_quality": pullback_quality,
            "momentum_30m": short_term,
            "momentum_1h": hourly,
        }
        score = sum(components.values(), Decimal("5"))
        return score, {name: str(value) for name, value in components.items()}

    def _discard_snapshot(self, symbol: str, reason: str, now_ms: int) -> None:
        with self._lock:
            snapshot = self._snapshots.pop(symbol, None)
            self._pullback_touches.pop(symbol, None)
        if snapshot is None:
            return
        self._rearm_at_ms[symbol] = (
            now_ms + self.settings.pullback_rearm_minutes * 60_000
        )
        logging.info(
            "CZ pullback discarded symbol=%s reason=%s signal_price=%s",
            symbol, reason, snapshot.signal_price,
        )

    @staticmethod
    def _effective_latest_price(candles: Sequence[Candle]) -> Decimal:
        latest = candles[-1]
        typical = (latest.high + latest.low + latest.close) / Decimal("3")
        if latest.high > latest.close * Decimal("1.02") and latest.high > typical * Decimal("1.02"):
            return candles[-2].close
        return latest.close

    @staticmethod
    def _denied(symbol: str) -> bool:
        if symbol in SUFFIX_ALLOWLIST:
            return False
        return (symbol in STABLECOIN_PAIRS or symbol.endswith("UPUSDT")
                or symbol.endswith("DOWNUSDT") or symbol.endswith("USDC"))
