"""Small in-process cache for shared public market data."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Mapping, Sequence

from ..core.models import Candle, Instrument
from ..core.ports import MarketDataPort


class CachedMarketData:
    def __init__(self, source: MarketDataPort, ttl_seconds: int = 10) -> None:
        self.source = source
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}

    def _get(self, key: tuple[Any, ...], loader: Any, ttl: int | None = None) -> Any:
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < (ttl or self.ttl_seconds):
            return cached[1]
        value = loader()
        self._cache[key] = (now, value)
        return value

    def instruments(self) -> Mapping[str, Instrument]:
        return self._get(("instruments",), self.source.instruments, 3600)

    def market_summaries(self) -> Sequence[Mapping[str, Any]]:
        return self._get(("summaries",), self.source.market_summaries, 60)

    def candles(self, symbol: str, interval: str, limit: int) -> Sequence[Candle]:
        return self._get(("candles", symbol, interval, limit), lambda: self.source.candles(symbol, interval, limit))

    def live_candles(self, symbol: str, interval: str, limit: int) -> Sequence[Candle]:
        return self._get(("live_candles", symbol, interval, limit),
                         lambda: self.source.live_candles(symbol, interval, limit), 3)

    def mark_price(self, symbol: str) -> Decimal:
        return self._get(("mark", symbol), lambda: self.source.mark_price(symbol), 3)

    def book(self, symbol: str) -> Mapping[str, Decimal]:
        return self._get(("book", symbol), lambda: self.source.book(symbol), 2)

    def futures_flow(self, symbol: str) -> Mapping[str, Decimal]:
        return self._get(
            ("futures_flow", symbol), lambda: self.source.futures_flow(symbol), 60,
        )
