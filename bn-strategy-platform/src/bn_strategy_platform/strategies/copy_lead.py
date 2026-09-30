"""Event-driven Binance public lead-portfolio strategy plugin."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..core.models import Candle, Position, PositionAction, Side, Signal, StrategyEvent
from ..core.ports import MarketDataPort, TradeStorePort


@dataclass(frozen=True)
class CopyLeadSettings:
    portfolio_id: str
    margin_per_trade: Decimal = Decimal("50")
    page_size: int = 20
    lookback_seconds: int = 3600
    max_entry_delay_seconds: int = 120
    event_not_before_ms: int = 0
    min_lead_notional: Decimal = Decimal("1000")
    stop_atr_multiplier: Decimal = Decimal("3")
    stop_max_roi: Decimal = Decimal("0.25")
    max_lead_price_deviation: Decimal = Decimal("0.005")
    loss_cooldown_minutes: int = 60
    max_symbol_stop_losses_per_day: int = 2
    risk_reduction_r: Decimal = Decimal("0.5")
    reduced_risk_r: Decimal = Decimal("0.2")
    breakeven_r: Decimal = Decimal("1")
    breakeven_buffer_price: Decimal = Decimal("0.004")
    profit_lock_r: Decimal = Decimal("1.5")
    locked_r: Decimal = Decimal("0.75")
    trailing_activation_r: Decimal = Decimal("2")
    trailing_offset_r: Decimal = Decimal("1")
    leverage: int = 5
    allowed_symbols: tuple[str, ...] = ()
    blocked_symbols: tuple[str, ...] = ()
    web_base_url: str = "https://www.binance.com"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CopyLeadSettings":
        if not raw.get("portfolio_id"):
            raise ValueError("copy-lead requires portfolio_id")
        settings = cls(
            portfolio_id=str(raw["portfolio_id"]),
            margin_per_trade=Decimal(str(raw.get("margin_per_trade", "50"))),
            page_size=int(raw.get("page_size", 20)),
            lookback_seconds=int(raw.get("lookback_seconds", 3600)),
            max_entry_delay_seconds=int(raw.get("max_entry_delay_seconds", 120)),
            event_not_before_ms=int(raw.get("event_not_before_ms", 0)),
            min_lead_notional=Decimal(str(raw.get("min_lead_notional", "1000"))),
            stop_atr_multiplier=Decimal(str(raw.get("stop_atr_multiplier", "3"))),
            stop_max_roi=Decimal(str(raw.get("stop_max_roi", "0.25"))),
            max_lead_price_deviation=Decimal(str(raw.get("max_lead_price_deviation", "0.005"))),
            loss_cooldown_minutes=int(raw.get("loss_cooldown_minutes", 60)),
            max_symbol_stop_losses_per_day=int(raw.get("max_symbol_stop_losses_per_day", 2)),
            risk_reduction_r=Decimal(str(raw.get("risk_reduction_r", "0.5"))),
            reduced_risk_r=Decimal(str(raw.get("reduced_risk_r", "0.2"))),
            breakeven_r=Decimal(str(raw.get("breakeven_r", "1"))),
            breakeven_buffer_price=Decimal(str(raw.get("breakeven_buffer_price", "0.004"))),
            profit_lock_r=Decimal(str(raw.get("profit_lock_r", "1.5"))),
            locked_r=Decimal(str(raw.get("locked_r", "0.75"))),
            trailing_activation_r=Decimal(str(raw.get("trailing_activation_r", "2"))),
            trailing_offset_r=Decimal(str(raw.get("trailing_offset_r", "1"))),
            leverage=int(raw.get("leverage", 5)),
            allowed_symbols=tuple(str(item).upper() for item in raw.get("allowed_symbols", [])),
            blocked_symbols=tuple(str(item).upper() for item in raw.get("blocked_symbols", [])),
            web_base_url=str(raw.get("web_base_url", "https://www.binance.com")),
        )
        if settings.margin_per_trade <= 0 or settings.min_lead_notional < 0:
            raise ValueError("copy-lead sizing values must be valid")
        if (settings.page_size < 1 or settings.lookback_seconds < 1
                or settings.max_entry_delay_seconds < 1):
            raise ValueError("copy-lead polling values must be positive")
        if settings.event_not_before_ms < 0:
            raise ValueError("copy-lead event_not_before_ms cannot be negative")
        if (settings.max_lead_price_deviation <= 0 or settings.loss_cooldown_minutes < 0
                or settings.max_symbol_stop_losses_per_day < 1):
            raise ValueError("copy-lead entry protection settings are invalid")
        if not (Decimal("0") <= settings.reduced_risk_r < settings.risk_reduction_r
                < settings.breakeven_r < settings.profit_lock_r
                < settings.trailing_activation_r):
            raise ValueError("copy-lead profit protection settings are invalid")
        return settings


class CopyLeadStrategy:
    name = "copy-lead"
    version = "2.4.0"
    interval = "5m"
    candle_limit = 120

    def __init__(self, settings: CopyLeadSettings, store: TradeStorePort | None = None) -> None:
        self.settings = settings
        self.store = store

    def poll_events(self, market: MarketDataPort) -> Sequence[StrategyEvent]:
        now_ms = int(time.time() * 1000)
        events = [self._event(row, market) for row in self._fetch(now_ms - self.settings.lookback_seconds * 1000, now_ms)]
        return sorted((item for item in events if item is not None), key=lambda item: item.observed_at_ms)

    def manage(self, position: Position, candles: Sequence[Candle], mark: Decimal) -> Iterable[PositionAction]:
        stopped = mark <= position.stop_price if position.side is Side.LONG else mark >= position.stop_price
        if stopped:
            reason = ("protected_stop" if self._profitable_stop(position) else "stop_loss")
            return [PositionAction("close", reason, position.remaining_quantity)]
        initial_stop = position.initial_stop_price or position.stop_price
        initial_risk = abs(position.entry_price - initial_stop)
        if initial_risk <= 0:
            return []
        best_move = (position.best_price - position.entry_price
                     if position.side is Side.LONG
                     else position.entry_price - position.best_price)
        best_r = best_move / initial_risk
        candidates: list[tuple[Decimal, str]] = []
        if best_r >= self.settings.risk_reduction_r:
            candidates.append((self._r_price(
                position, initial_risk, -self.settings.reduced_risk_r,
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
                position, initial_risk, self.settings.locked_r,
            ), "profit_lock"))
        if best_r >= self.settings.trailing_activation_r:
            candidate = (
                position.best_price - initial_risk * self.settings.trailing_offset_r
                if position.side is Side.LONG
                else position.best_price + initial_risk * self.settings.trailing_offset_r
            )
            candidates.append((candidate, "trailing"))
        if not candidates:
            return []
        candidate, reason = (max(candidates, key=lambda item: item[0])
                             if position.side is Side.LONG
                             else min(candidates, key=lambda item: item[0]))
        improved = candidate > position.stop_price if position.side is Side.LONG else candidate < position.stop_price
        if not improved:
            return []
        crossed = mark <= candidate if position.side is Side.LONG else mark >= candidate
        if crossed:
            return [PositionAction("close", "protected_stop", position.remaining_quantity)]
        return [PositionAction("move_stop", reason, stop_price=candidate)]

    def event_entry_block_reason(self, event: StrategyEvent, mode: str) -> str | None:
        if event.kind != "open" or event.signal is None or self.store is None:
            return None
        now_ms = int(time.time() * 1000)
        start_ms, end_ms = self._day_bounds(now_ms)
        stops = self.store.count_stop_losses(
            self.name, mode, event.symbol, event.side, start_ms, end_ms,
        )
        if stops >= self.settings.max_symbol_stop_losses_per_day:
            return f"daily symbol stop-loss limit reached ({stops}/{self.settings.max_symbol_stop_losses_per_day})"
        previous = self.store.last_closed_trade(self.name, mode, event.symbol)
        if previous is None or previous.net_pnl >= 0 or previous.side is not event.side:
            return None
        eligible_at = previous.closed_at_ms + self.settings.loss_cooldown_minutes * 60_000
        if now_ms < eligible_at:
            remaining = max(1, (eligible_at - now_ms + 999) // 1000)
            return f"same-direction loss cooldown ({remaining}s remaining)"
        return None

    def _event(self, row: Mapping[str, Any], market: MarketDataPort) -> StrategyEvent | None:
        symbol = str(row["symbol"]).upper()
        position_side, order_side = str(row["positionSide"]).upper(), str(row["side"]).upper()
        side = Side.LONG if position_side == "LONG" else Side.SHORT
        actions = {("LONG", "BUY"): "open", ("LONG", "SELL"): "close",
                   ("SHORT", "SELL"): "open", ("SHORT", "BUY"): "close"}
        kind = actions.get((position_side, order_side))
        if kind is None or symbol in self.settings.blocked_symbols:
            return None
        if self.settings.allowed_symbols and symbol not in self.settings.allowed_symbols:
            return None
        observed = int(row.get("orderUpdateTime", row["orderTime"]))
        if observed < self.settings.event_not_before_ms:
            return None
        age = max(0, int(time.time() * 1000) - observed)
        if kind == "open" and age > self.settings.max_entry_delay_seconds * 1000:
            return None
        executed, average = Decimal(str(row["executedQty"])), Decimal(str(row["avgPrice"]))
        if kind == "open" and executed * average < self.settings.min_lead_notional:
            return None
        raw_id = "|".join((symbol, order_side, position_side, str(executed), str(average),
                           str(row["orderTime"]), str(observed)))
        event_id = hashlib.sha256(raw_id.encode()).hexdigest()
        signal = None
        if kind == "open":
            mark = market.mark_price(symbol)
            deviation = abs(mark / average - Decimal("1")) if average > 0 else Decimal("999")
            if deviation > self.settings.max_lead_price_deviation:
                return StrategyEvent(
                    event_id, self.name, "skip_price_deviation", symbol, side, observed,
                )
            stop = self._stop(mark, side, market.candles(symbol, self.interval, self.candle_limit))
            score = max(Decimal("0"), Decimal("100") - Decimal(age) / Decimal("60000") * Decimal("5"))
            signal = Signal(self.name, self.version, symbol, side, mark, stop, score, "lead_open", observed,
                            {"lead_price": str(average), "lead_notional": str(executed * average),
                             "lead_price_deviation": str(deviation),
                             "order_age_ms": age}, self.settings.margin_per_trade)
        return StrategyEvent(event_id, self.name, kind, symbol, side, observed, signal)

    def _stop(self, entry: Decimal, side: Side, candles: Sequence[Candle]) -> Decimal:
        ranges = [max(candles[i].high - candles[i].low,
                      abs(candles[i].high - candles[i - 1].close),
                      abs(candles[i].low - candles[i - 1].close)) for i in range(1, len(candles))]
        atr = sum(ranges[-14:], Decimal("0")) / Decimal(min(14, len(ranges))) if ranges else Decimal("0")
        max_move = self.settings.stop_max_roi / Decimal(self.settings.leverage)
        distance = min(entry * max_move, atr * self.settings.stop_atr_multiplier)
        if distance <= 0:
            distance = entry * min(max_move, Decimal("0.02"))
        return entry - distance if side is Side.LONG else entry + distance

    @staticmethod
    def _r_price(position: Position, initial_risk: Decimal, multiple: Decimal) -> Decimal:
        return (position.entry_price + initial_risk * multiple
                if position.side is Side.LONG
                else position.entry_price - initial_risk * multiple)

    @staticmethod
    def _profitable_stop(position: Position) -> bool:
        return (position.stop_price >= position.entry_price if position.side is Side.LONG
                else position.stop_price <= position.entry_price)

    @staticmethod
    def _day_bounds(now_ms: int) -> tuple[int, int]:
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime.fromtimestamp(now_ms / 1000, zone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _fetch(self, start_ms: int, end_ms: int) -> list[Mapping[str, Any]]:
        body = json.dumps({"portfolioId": self.settings.portfolio_id, "pageNumber": 1,
                           "pageSize": self.settings.page_size, "startTime": start_ms,
                           "endTime": end_ms}, separators=(",", ":")).encode()
        request = urllib.request.Request(
            f"{self.settings.web_base_url.rstrip('/')}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history",
            data=body, method="POST", headers={"User-Agent": "bn-strategy-platform/1.0",
                                                "Content-Type": "application/json", "clienttype": "web", "lang": "en"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Binance lead API error {exc.code}: {exc.read().decode(errors='replace')}") from exc
        if payload.get("code") != "000000" or not payload.get("success", False):
            raise RuntimeError(f"Binance lead API rejected request: {payload}")
        return list(payload.get("data", {}).get("list", []))
