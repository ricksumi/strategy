"""Pure candle indicators used by strategy plugins."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from ..core.models import Candle


def ema(values: Sequence[Decimal], period: int) -> list[Optional[Decimal]]:
    result: list[Optional[Decimal]] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = current
    multiplier = Decimal("2") / Decimal(period + 1)
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        result[index] = current
    return result


def atr_ratio(candles: Sequence[Candle], period: int = 14) -> Optional[Decimal]:
    if len(candles) < period + 1 or candles[-1].close <= 0:
        return None
    ranges = []
    for index in range(len(candles) - period, len(candles)):
        candle = candles[index]
        previous = candles[index - 1]
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous.close),
                abs(candle.low - previous.close),
            )
        )
    return sum(ranges, Decimal("0")) / Decimal(period) / candles[-1].close


def directional_index(candles: Sequence[Candle], period: int = 14) -> Optional[Decimal]:
    if len(candles) < period * 2 + 1:
        return None
    true_ranges: list[Decimal] = []
    plus_dm: list[Decimal] = []
    minus_dm: list[Decimal] = []
    for index in range(1, len(candles)):
        current, previous = candles[index], candles[index - 1]
        up = current.high - previous.high
        down = previous.low - current.low
        plus_dm.append(up if up > down and up > 0 else Decimal("0"))
        minus_dm.append(down if down > up and down > 0 else Decimal("0"))
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    dx: list[Decimal] = []
    for end in range(period, len(true_ranges) + 1):
        tr = sum(true_ranges[end - period:end], Decimal("0"))
        if tr <= 0:
            continue
        plus = sum(plus_dm[end - period:end], Decimal("0")) / tr
        minus = sum(minus_dm[end - period:end], Decimal("0")) / tr
        total = plus + minus
        if total > 0:
            dx.append(abs(plus - minus) / total * Decimal("100"))
    return sum(dx[-period:], Decimal("0")) / Decimal(period) if len(dx) >= period else None


def volume_ratio(candles: Sequence[Candle], bars: int = 12) -> Decimal:
    if len(candles) < bars * 2:
        return Decimal("0")
    recent = sum((c.quote_volume for c in candles[-bars:]), Decimal("0"))
    previous = sum((c.quote_volume for c in candles[-bars * 2:-bars]), Decimal("0"))
    return recent / previous if previous > 0 else Decimal("0")
