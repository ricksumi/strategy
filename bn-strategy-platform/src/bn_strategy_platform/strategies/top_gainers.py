"""Explicit continuation, exhaustion-short, and pullback-long strategies."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..core.models import Candle, MarketCandidate, Position, PositionAction, Side, Signal
from ..core.ports import MarketDataPort
from .indicators import atr_ratio, directional_index, ema, volume_ratio


@dataclass(frozen=True)
class TopGainersSettings:
    model: str = "exhaustion_short"
    interval: str = "5m"
    candle_limit: int = 200
    active_symbol_limit: int = 30
    min_quote_volume: Decimal = Decimal("15000000")
    min_range_24h: Decimal = Decimal("0.03")
    max_spread: Decimal = Decimal("0.0015")
    min_atr: Decimal = Decimal("0.005")
    max_atr: Decimal = Decimal("0.06")
    min_adx: Decimal = Decimal("25")
    stop_atr: Decimal = Decimal("2")
    min_stop_distance: Decimal = Decimal("0.015")
    max_stop_distance: Decimal = Decimal("0.04")
    profit_guard_roi: Decimal = Decimal("0.08")
    locked_roi: Decimal = Decimal("0.03")
    trailing_activation_roi: Decimal = Decimal("0.15")
    trailing_callback_price: Decimal = Decimal("0.012")
    tight_trailing_activation_roi: Decimal = Decimal("0.25")
    tight_trailing_callback_price: Decimal = Decimal("0.006")
    risk_reduction_r: Decimal = Decimal("0.60")
    reduced_risk_r: Decimal = Decimal("0.25")
    breakeven_r: Decimal = Decimal("1")
    breakeven_buffer_price: Decimal = Decimal("0.006")
    profit_lock_r: Decimal = Decimal("1.5")
    locked_r: Decimal = Decimal("0.75")
    tp1_r: Decimal = Decimal("2")
    post_tp1_lock_r: Decimal = Decimal("1")
    short_tp1_fraction: Decimal = Decimal("0.5")
    long_tp1_fraction: Decimal = Decimal("0.3")
    trailing_activation_r: Decimal = Decimal("3")
    trailing_offset_r: Decimal = Decimal("1")
    tight_trailing_activation_r: Decimal = Decimal("5")
    tight_trailing_offset_r: Decimal = Decimal("0.5")
    scale_in_enabled: bool = False
    scale_in_trigger_r: Decimal = Decimal("1")
    scale_in_margin_usdt: Decimal = Decimal("75")
    scale_in_breakout_lookback: int = 2
    entry_confirmation_window_minutes: int = 20
    entry_invalidation_atr: Decimal = Decimal("0.5")
    pullback_reclaim_volume_ratio: Decimal = Decimal("1.1")
    structure_stop_buffer_atr: Decimal = Decimal("0.25")
    loss_cooldown_minutes: int = 15
    reentry_breakout_lookback: int = 3
    max_symbol_stop_losses_per_day: int = 2

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TopGainersSettings":
        values = {}
        for field_name, field_def in cls.__dataclass_fields__.items():
            if field_name not in raw:
                continue
            value = raw[field_name]
            values[field_name] = Decimal(str(value)) if isinstance(field_def.default, Decimal) else value
        settings = cls(**values)
        if settings.model not in {
            "continuation_long", "continuation_short", "exhaustion_short", "exhaustion_pullback_long",
        }:
            raise ValueError("unsupported top-gainers model")
        for fraction in (settings.short_tp1_fraction, settings.long_tp1_fraction):
            if not Decimal("0") < fraction < Decimal("1"):
                raise ValueError("partial take-profit fractions must be between zero and one")
        if (settings.loss_cooldown_minutes < 0 or settings.reentry_breakout_lookback < 1
                or settings.max_symbol_stop_losses_per_day < 1):
            raise ValueError("loss re-entry settings are invalid")
        if (settings.entry_confirmation_window_minutes < 1
                or settings.entry_invalidation_atr <= 0
                or settings.pullback_reclaim_volume_ratio <= 0
                or settings.structure_stop_buffer_atr < 0):
            raise ValueError("entry confirmation settings are invalid")
        if not (Decimal("0") <= settings.locked_r <= settings.post_tp1_lock_r
                < settings.tp1_r):
            raise ValueError("profit-lock R levels are invalid")
        if (settings.scale_in_trigger_r <= 0 or settings.scale_in_margin_usdt <= 0
                or settings.scale_in_breakout_lookback < 1):
            raise ValueError("scale-in settings are invalid")
        return settings


@dataclass(frozen=True)
class EntrySetup:
    source: Signal
    armed_close_time_ms: int
    expires_at_ms: int
    setup_high: Decimal
    setup_low: Decimal


class TopGainersStrategy:
    name = "bn-stra-top-gainers-platform-1"
    version = "3.3.0"

    def __init__(self, settings: TopGainersSettings) -> None:
        self.settings = settings
        if settings.model == "exhaustion_pullback_long":
            self.version = "4.8.0"
        elif settings.model == "continuation_short":
            self.version = "3.11.0"
        else:
            self.version = "3.3.0"
        self.interval = settings.interval
        self.candle_limit = settings.candle_limit
        self._entry_setups: dict[str, EntrySetup] = {}
        self._entry_rearm_after_close: dict[str, int] = {}
        self._entry_lock = RLock()

    def select_universe(self, market: MarketDataPort) -> Sequence[str]:
        books = market.market_summaries()
        candidates: list[MarketCandidate] = []
        for row in books:
            try:
                symbol = str(row["symbol"])
                if not symbol.endswith("USDT"):
                    continue
                quote_volume = Decimal(str(row["quoteVolume"]))
                last = Decimal(str(row["lastPrice"]))
                high = Decimal(str(row["highPrice"]))
                low = Decimal(str(row["lowPrice"]))
                bid = Decimal(str(row["bidPrice"]))
                ask = Decimal(str(row["askPrice"]))
                range_24h = (high - low) / low if low > 0 else Decimal("0")
                spread = (ask - bid) / last if last > 0 else Decimal("999")
                if (
                    quote_volume < self.settings.min_quote_volume
                    or range_24h < self.settings.min_range_24h
                    or spread > self.settings.max_spread
                ):
                    continue
                change = Decimal(str(row.get("priceChangePercent", "0"))) / Decimal("100")
                score = change * Decimal("100") + range_24h * Decimal("20")
                candidates.append(
                    MarketCandidate(symbol, quote_volume, range_24h, spread, Decimal("0"),
                                    Decimal("0"), Decimal("0"), score)
                )
            except (KeyError, ValueError, ArithmeticError):
                continue
        candidates.sort(key=lambda item: (item.score, item.quote_volume_24h), reverse=True)
        selected = [item.symbol for item in candidates[: self.settings.active_symbol_limit]]
        with self._entry_lock:
            setup_symbols = tuple(self._entry_setups)
        selected.extend(symbol for symbol in setup_symbols if symbol not in selected)
        return selected

    def evaluate(self, symbol: str, candles: Sequence[Candle], mark: Decimal) -> Optional[Signal]:
        if len(candles) < 80:
            return None
        atr = atr_ratio(candles)
        adx = directional_index(candles)
        if atr is None or adx is None or not self.settings.min_atr <= atr <= self.settings.max_atr:
            return None
        closes = [c.close for c in candles]
        ema20 = ema(closes, 20)[-1]
        ema60 = ema(closes, 60)[-1]
        if ema20 is None or ema60 is None:
            return None
        vol_ratio = volume_ratio(candles)
        with self._entry_lock:
            has_setup = symbol in self._entry_setups
            rearm_after = self._entry_rearm_after_close.get(symbol, 0)
        if has_setup:
            return self._confirm_entry(symbol, candles, mark, atr, adx, ema20, vol_ratio)
        if candles[-1].close_time_ms <= rearm_after:
            return None
        if self.settings.model == "continuation_long":
            return self._continuation_long(symbol, candles, mark, atr, adx, ema20, ema60, vol_ratio)
        if self.settings.model == "continuation_short":
            long_signal = self._continuation_long(symbol, candles, mark, atr, adx, ema20, ema60, vol_ratio)
            if long_signal is None:
                return None
            self._arm_entry(long_signal, candles)
            return None
        exhaustion = self._exhaustion_short(symbol, candles, mark, atr, adx, ema20, ema60, vol_ratio)
        if exhaustion is None or self.settings.model == "exhaustion_short":
            return exhaustion
        self._arm_entry(exhaustion, candles)
        return None

    def _arm_entry(self, source: Signal, candles: Sequence[Candle]) -> None:
        now_ms = int(time.time() * 1000)
        setup = EntrySetup(
            source=source,
            armed_close_time_ms=candles[-1].close_time_ms,
            expires_at_ms=now_ms + self.settings.entry_confirmation_window_minutes * 60_000,
            setup_high=max(candle.high for candle in candles[-3:]),
            setup_low=min(candle.low for candle in candles[-3:]),
        )
        with self._entry_lock:
            self._entry_setups[source.symbol] = setup
        logging.info(
            "entry confirmation armed strategy=%s symbol=%s model=%s side=%s "
            "setup_high=%s setup_low=%s expires_at_ms=%s",
            self.name, source.symbol, self.settings.model,
            "short" if self.settings.model == "continuation_short" else "long",
            setup.setup_high, setup.setup_low, setup.expires_at_ms,
        )

    def _confirm_entry(
        self, symbol: str, candles: Sequence[Candle], mark: Decimal, atr: Decimal,
        adx: Decimal, ema20: Decimal, vol_ratio: Decimal,
    ) -> Optional[Signal]:
        with self._entry_lock:
            setup = self._entry_setups.get(symbol)
        if setup is None:
            return None
        now_ms = int(time.time() * 1000)
        latest, previous = candles[-1], candles[-2]
        if now_ms > setup.expires_at_ms:
            self._discard_confirmation(symbol, "expired", latest.close_time_ms)
            return None
        if latest.close_time_ms <= setup.armed_close_time_ms:
            return None
        atr_price = max(Decimal("0"), setup.source.signal_price * atr)
        invalidation = atr_price * self.settings.entry_invalidation_atr
        if self.settings.model == "continuation_short":
            if latest.high >= setup.setup_high + invalidation:
                self._discard_confirmation(symbol, "continued_higher", latest.close_time_ms)
                return None
            ema9 = ema([candle.close for candle in candles], 9)[-1]
            momentum_15m = latest.close / candles[-3].open - Decimal("1")
            confirmed = (
                ema9 is not None
                and latest.high < previous.high
                and latest.close < previous.low
                and latest.close < ema9
                and momentum_15m < 0
                and mark <= latest.close + atr_price * Decimal("0.25")
            )
            if not confirmed:
                return None
            stop_distance = self._stop_distance(atr)
            structure_stop = setup.setup_high * (
                Decimal("1") + atr * self.settings.structure_stop_buffer_atr
            )
            stop = self._exhaustion_stop(mark, structure_stop, stop_distance)
            score, breakdown = self._confirmed_short_score(
                setup, mark, atr, adx, vol_ratio, momentum_15m,
            )
            side = Side.SHORT
            reason = "momentum_exhaustion_confirmed_short"
            model = "momentum-short-v5"
            extra = {"momentum_15m": str(momentum_15m)}
        else:
            if latest.low <= setup.setup_low - invalidation:
                self._discard_confirmation(symbol, "continued_lower", latest.close_time_ms)
                return None
            prior = candles[-13:-1]
            average_volume = (sum((candle.quote_volume for candle in prior), Decimal("0"))
                              / Decimal(len(prior))) if prior else Decimal("0")
            reclaim_volume = (latest.quote_volume / average_volume
                              if average_volume > 0 else Decimal("0"))
            confirmed = (
                latest.low > previous.low
                and latest.close > previous.high
                and latest.close > ema20
                and reclaim_volume >= self.settings.pullback_reclaim_volume_ratio
                and mark >= latest.close - atr_price * Decimal("0.25")
            )
            if not confirmed:
                return None
            stop_distance = self._stop_distance(atr)
            stop = mark * (Decimal("1") - stop_distance)
            score, breakdown = self._confirmed_long_score(
                setup, latest, previous, atr_price, reclaim_volume,
            )
            side = Side.LONG
            reason = "exhaustion_reclaim_confirmed_long"
            model = "pullback-long-v3"
            extra = {"reclaim_volume_ratio": str(reclaim_volume)}
        with self._entry_lock:
            if self._entry_setups.get(symbol) is not setup:
                return None
            del self._entry_setups[symbol]
            self._entry_rearm_after_close[symbol] = latest.close_time_ms
        logging.info(
            "entry confirmation passed strategy=%s symbol=%s side=%s score=%s",
            self.name, symbol, side.value, score,
        )
        return Signal(
            self.name, self.version, symbol, side, mark, stop, score, reason, now_ms,
            {
                **setup.source.metrics,
                **extra,
                "model": self.settings.model,
                "score_model_version": model,
                "score_breakdown": breakdown,
                "source_signal_price": str(setup.source.signal_price),
                "setup_high": str(setup.setup_high),
                "setup_low": str(setup.setup_low),
                "confirmation_age_ms": now_ms - setup.source.observed_at_ms,
            },
        )

    def _discard_confirmation(self, symbol: str, reason: str, close_time_ms: int) -> None:
        with self._entry_lock:
            setup = self._entry_setups.pop(symbol, None)
            if setup is not None:
                self._entry_rearm_after_close[symbol] = close_time_ms
        if setup is not None:
            logging.info(
                "entry confirmation discarded strategy=%s symbol=%s reason=%s",
                self.name, symbol, reason,
            )

    @staticmethod
    def _confirmed_long_score(
        setup: EntrySetup, latest: Candle, previous: Candle, atr_price: Decimal,
        reclaim_volume: Decimal,
    ) -> tuple[Decimal, dict[str, str]]:
        source_quality = min(Decimal("25"), setup.source.score * Decimal("0.25"))
        reclaim = ((latest.close - previous.high) / atr_price
                   if atr_price > 0 else Decimal("0"))
        higher_low = ((latest.low - previous.low) / atr_price
                      if atr_price > 0 else Decimal("0"))
        components = {
            "source_quality": source_quality,
            "reclaim_strength": min(Decimal("25"), max(Decimal("0"), reclaim * Decimal("25"))),
            "higher_low_quality": min(Decimal("20"), max(Decimal("0"), higher_low * Decimal("20"))),
            "reclaim_volume_quality": min(Decimal("20"), max(
                Decimal("0"), (reclaim_volume - Decimal("1")) * Decimal("20"),
            )),
        }
        return min(Decimal("99"), sum(components.values(), Decimal("10"))), {
            name: str(value) for name, value in components.items()
        }

    @staticmethod
    def _confirmed_short_score(
        setup: EntrySetup, mark: Decimal, atr: Decimal, adx: Decimal,
        vol_ratio: Decimal, momentum_15m: Decimal,
    ) -> tuple[Decimal, dict[str, str]]:
        source_momentum = abs(Decimal(str(setup.source.metrics.get("momentum", "0"))))
        atr_price = setup.source.signal_price * atr
        rejection = ((setup.setup_high - mark) / atr_price
                     if atr_price > 0 else Decimal("0"))
        components = {
            "trend_maturity": max(Decimal("0"), Decimal("20") - abs(adx - Decimal("40")) / 2),
            "impulse_quality": max(Decimal("0"), Decimal("20")
                                   - abs(source_momentum - Decimal("0.035")) * Decimal("250")),
            "volume_quality": max(Decimal("0"), Decimal("18")
                                  - abs(vol_ratio - Decimal("2")) * Decimal("6")),
            "breakdown_strength": min(Decimal("16"), abs(momentum_15m) * Decimal("800")),
            "rejection_quality": min(Decimal("16"), max(Decimal("0"), rejection * Decimal("8"))),
        }
        return min(Decimal("99"), sum(components.values(), Decimal("10"))), {
            name: str(value) for name, value in components.items()
        }

    def _continuation_long(
        self, symbol: str, candles: Sequence[Candle], mark: Decimal, atr: Decimal,
        adx: Decimal, ema20: Decimal, ema60: Decimal, vol_ratio: Decimal,
    ) -> Optional[Signal]:
        latest, previous = candles[-1], candles[-2]
        return_1h = latest.close / candles[-12].open - Decimal("1")
        healthy_reclaim = latest.low > min(c.low for c in candles[-5:-1]) and latest.close > previous.high
        if not (
            ema20 > ema60 and adx >= self.settings.min_adx and return_1h >= Decimal("0.01")
            and vol_ratio >= Decimal("1.5") and healthy_reclaim and mark >= latest.close
        ):
            return None
        stop_distance = self._stop_distance(atr)
        return self._signal(symbol, Side.LONG, mark, mark * (Decimal("1") - stop_distance),
                            adx, atr, vol_ratio, return_1h, "momentum_continuation")

    def _exhaustion_short(
        self, symbol: str, candles: Sequence[Candle], mark: Decimal, atr: Decimal,
        adx: Decimal, ema20: Decimal, ema60: Decimal, vol_ratio: Decimal,
    ) -> Optional[Signal]:
        latest = candles[-1]
        prior_peak = max(c.high for c in candles[-12:-2])
        peak_drop = prior_peak / latest.close - Decimal("1") if latest.close > 0 else Decimal("0")
        ema20_previous = ema([c.close for c in candles[:-1]], 20)[-1]
        bearish_closes = sum(1 for c in candles[-3:] if c.close < c.open)
        failed_reclaim = latest.close < ema20 and latest.high <= prior_peak
        if not (
            ema20_previous is not None and ema20 < ema20_previous and failed_reclaim
            and bearish_closes >= 2 and peak_drop >= Decimal("0.008")
            and vol_ratio >= Decimal("1") and mark <= latest.close
        ):
            return None
        stop_distance = self._stop_distance(atr)
        structure_stop = max(c.high for c in candles[-5:]) * (Decimal("1") + atr * Decimal("0.25"))
        stop = self._exhaustion_stop(mark, structure_stop, stop_distance)
        return self._signal(symbol, Side.SHORT, mark, stop, adx, atr, vol_ratio, -peak_drop,
                            "exhaustion_lower_high_break")

    def _stop_distance(self, atr: Decimal) -> Decimal:
        return min(self.settings.max_stop_distance,
                   max(self.settings.min_stop_distance, atr * self.settings.stop_atr))

    def _exhaustion_stop(
        self, mark: Decimal, structure_stop: Decimal, maximum_distance: Decimal,
    ) -> Decimal:
        structure_distance = max(Decimal("0"), structure_stop / mark - Decimal("1"))
        enforced_distance = min(maximum_distance,
                                max(self.settings.min_stop_distance, structure_distance))
        return mark * (Decimal("1") + enforced_distance)

    def _signal(
        self, symbol: str, side: Side, price: Decimal, stop: Decimal, adx: Decimal,
        atr: Decimal, vol_ratio: Decimal, momentum: Decimal, reason: str,
    ) -> Signal:
        score, breakdown, score_model = self._score(adx, atr, vol_ratio, momentum)
        return Signal(self.name, self.version, symbol, side, price, stop, score, reason,
                      int(time.time() * 1000), {
                          "adx": str(adx), "atr": str(atr), "volume_ratio": str(vol_ratio),
                          "momentum": str(momentum), "model": self.settings.model,
                          "score_model_version": score_model,
                          "score_breakdown": breakdown,
                      })

    def _score(
        self, adx: Decimal, atr: Decimal, vol_ratio: Decimal, momentum: Decimal,
    ) -> tuple[Decimal, dict[str, str], str]:
        volume = max(Decimal("0"), Decimal("18")
                     - abs(vol_ratio - Decimal("1.8")) * Decimal("9"))
        volatility = max(Decimal("0"), Decimal("18")
                         - abs(atr - Decimal("0.02")) * Decimal("450"))
        if self.settings.model == "exhaustion_pullback_long":
            trend = max(Decimal("0"), Decimal("22") - abs(adx - Decimal("22")))
            depth = max(Decimal("0"), Decimal("22")
                        - abs(abs(momentum) - Decimal("0.02")) * Decimal("500"))
            components = {
                "pullback_trend_quality": trend,
                "volume_quality": volume,
                "pullback_depth_quality": depth,
                "volatility_quality": volatility,
            }
            model = "pullback-long-v2"
        else:
            trend = min(Decimal("22"), max(Decimal("0"),
                        (adx - Decimal("20")) * Decimal("0.75")))
            impulse = min(Decimal("22"), max(Decimal("0"),
                          (abs(momentum) - Decimal("0.01")) * Decimal("350")))
            components = {
                "trend_strength": trend,
                "volume_quality": volume,
                "impulse_quality": impulse,
                "volatility_quality": volatility,
            }
            model = "momentum-short-v4"
        score = sum(components.values(), Decimal("10"))
        return score, {name: str(value) for name, value in components.items()}, model

    def manage(
        self, position: Position, candles: Sequence[Candle], mark: Decimal,
    ) -> Iterable[PositionAction]:
        if (position.side is Side.LONG and mark <= position.stop_price) or (
            position.side is Side.SHORT and mark >= position.stop_price
        ):
            reason = "protected_stop" if self._profitable_stop(position) else "stop_loss"
            return [PositionAction("close", reason, position.remaining_quantity)]

        initial_entry = position.initial_entry_price or position.entry_price
        initial_stop = position.initial_stop_price or position.stop_price
        initial_risk = (
            position.initial_risk_distance
            or abs(initial_entry - initial_stop)
        )
        if initial_risk <= 0:
            return []
        current_move = (mark - initial_entry if position.side is Side.LONG
                        else initial_entry - mark)
        best_move = (position.best_price - initial_entry if position.side is Side.LONG
                     else initial_entry - position.best_price)
        current_r = current_move / initial_risk
        best_r = best_move / initial_risk

        tp1_done = 1 in position.partial_tiers_done or position.remaining_quantity < position.quantity
        if not tp1_done and current_r >= self.settings.tp1_r:
            fraction = (self.settings.long_tp1_fraction if position.side is Side.LONG
                        else self.settings.short_tp1_fraction)
            return [PositionAction(
                "close", "take_profit_1", position.quantity * fraction, tier=1,
            )]

        candidates: list[tuple[Decimal, str]] = []
        if best_r >= self.settings.risk_reduction_r:
            candidates.append((self._r_price(
                position, initial_entry, initial_risk, -self.settings.reduced_risk_r,
            ), "risk_reduction"))
        if best_r >= self.settings.breakeven_r:
            buffer = position.entry_price * self.settings.breakeven_buffer_price
            candidates.append((
                position.entry_price + buffer if position.side is Side.LONG
                else position.entry_price - buffer,
                "breakeven",
            ))
        if best_r >= self.settings.profit_lock_r:
            candidates.append((self._r_price(
                position, initial_entry, initial_risk, self.settings.locked_r,
            ), "profit_lock"))
        if tp1_done:
            candidates.append((self._r_price(
                position, initial_entry, initial_risk, self.settings.post_tp1_lock_r,
            ), "post_tp1_lock"))
        if best_r >= self.settings.trailing_activation_r:
            tight = best_r >= self.settings.tight_trailing_activation_r
            offset = (self.settings.tight_trailing_offset_r if tight
                      else self.settings.trailing_offset_r)
            candidate = (position.best_price - initial_risk * offset
                         if position.side is Side.LONG
                         else position.best_price + initial_risk * offset)
            candidates.append((candidate, "tight_trailing" if tight else "trailing"))
        scale_in = (
            self.settings.model == "exhaustion_pullback_long"
            and self.settings.scale_in_enabled
            and position.strategy_version == self.version
            and position.side is Side.LONG
            and not position.scale_in_done
            and not tp1_done
            and best_r >= self.settings.scale_in_trigger_r
            and current_r >= self.settings.scale_in_trigger_r
            and self._scale_in_confirmed(candles, mark)
        )
        if not candidates:
            return ([PositionAction(
                "add", "confirmed_profit_scale_in",
                margin=self.settings.scale_in_margin_usdt,
            )] if scale_in else [])
        candidate, reason = (max(candidates, key=lambda item: item[0])
                             if position.side is Side.LONG
                             else min(candidates, key=lambda item: item[0]))
        improved = (candidate > position.stop_price if position.side is Side.LONG
                    else candidate < position.stop_price)
        candidate_crossed = (mark <= candidate if position.side is Side.LONG
                             else mark >= candidate)
        if improved and candidate_crossed:
            close_reason = ("trailing_stop" if "trailing" in reason
                            else "protected_stop")
            return [PositionAction("close", close_reason, position.remaining_quantity)]
        actions: list[PositionAction] = []
        if improved:
            actions.append(PositionAction("move_stop", reason, stop_price=candidate))
        effective_stop = candidate if improved else position.stop_price
        protected = effective_stop >= position.entry_price
        if scale_in and protected:
            actions.append(PositionAction(
                "add", "confirmed_profit_scale_in",
                margin=self.settings.scale_in_margin_usdt,
            ))
        return actions

    @staticmethod
    def _r_price(
        position: Position, initial_entry: Decimal,
        initial_risk: Decimal, multiple: Decimal,
    ) -> Decimal:
        return (initial_entry + initial_risk * multiple
                if position.side is Side.LONG
                else initial_entry - initial_risk * multiple)

    def _scale_in_confirmed(
        self, candles: Sequence[Candle], mark: Decimal,
    ) -> bool:
        lookback = self.settings.scale_in_breakout_lookback
        if len(candles) < lookback + 1:
            return False
        latest = candles[-1]
        prior = candles[-(lookback + 1):-1]
        return (
            latest.low > prior[-1].low
            and latest.close > max(item.high for item in prior)
            and mark >= latest.close
        )

    @staticmethod
    def _profitable_stop(position: Position) -> bool:
        return (position.stop_price >= position.entry_price if position.side is Side.LONG
                else position.stop_price <= position.entry_price)
