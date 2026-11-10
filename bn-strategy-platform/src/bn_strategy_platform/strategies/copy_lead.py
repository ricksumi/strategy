"""Event-driven Binance public lead-portfolio strategy plugin."""

from __future__ import annotations

import hashlib
import json
import logging
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
    max_pages: int = 10
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
            max_pages=int(raw.get("max_pages", 10)),
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
        if (settings.page_size < 1 or not 1 <= settings.max_pages <= 20
                or settings.lookback_seconds < 1
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
    version = "2.5.0"
    interval = "5m"
    candle_limit = 120

    def __init__(self, settings: CopyLeadSettings, store: TradeStorePort | None = None,
                 mode: str = "paper") -> None:
        self.settings = settings
        self.store = store
        self.mode = mode
        self._received_at: dict[str, int] = {}

    def poll_events(self, market: MarketDataPort) -> Sequence[StrategyEvent]:
        now_ms = int(time.time() * 1000)
        events = []
        for row in self._fetch(now_ms - self.settings.lookback_seconds * 1000, now_ms):
            try:
                event = self._event(row, market)
                if event is not None:
                    events.append(event)
            except Exception:
                logging.exception("lead order processing failed strategy=%s symbol=%s",
                                  self.name, row.get("symbol"))
        return sorted((item for item in events if item is not None), key=lambda item: item.observed_at_ms)

    def record_event_decision(self, event: StrategyEvent, decision: str, reason: str = "",
                              metrics: Mapping[str, Any] | None = None) -> None:
        if self.store is not None:
            self.store.decide_lead_order(
                self.name, self.mode, self.settings.portfolio_id, event.event_id,
                decision, reason, metrics if metrics is not None
                else (event.signal.metrics if event.signal else None),
            )

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
        if event.kind != "open" or event.signal is None:
            return None
        now_ms = int(time.time() * 1000)
        if now_ms - event.observed_at_ms > self.settings.max_entry_delay_seconds * 1000:
            return "entry delay exceeded before execution"
        if self.store is None:
            return None
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
        symbol = str(row.get("symbol", "")).upper()
        position_side = str(row.get("positionSide", "")).upper()
        order_side = str(row.get("side", "")).upper()
        side = Side.LONG if position_side == "LONG" else Side.SHORT
        actions = {("LONG", "BUY"): "open", ("LONG", "SELL"): "close",
                   ("SHORT", "SELL"): "open", ("SHORT", "BUY"): "close"}
        kind = actions.get((position_side, order_side))
        try:
            observed = int(row.get("orderUpdateTime") or row.get("orderTime") or 0)
        except (ValueError, TypeError):
            observed = 0
        now_ms = int(time.time() * 1000)
        seen_at = self._received_at.get(json.dumps(dict(row), sort_keys=True, default=str), now_ms)
        age = max(0, now_ms - observed)
        # Preserve the existing identity for valid orders to prevent replay on upgrade.
        try:
            executed = Decimal(str(row.get("executedQty", 0)))
            average = Decimal(str(row.get("avgPrice", 0)))
            valid_values = executed.is_finite() and average.is_finite()
            raw_id = "|".join((symbol, order_side, position_side, str(executed), str(average),
                               str(row.get("orderTime", 0)), str(observed)))
        except (ValueError, ArithmeticError):
            executed, average, valid_values = Decimal("0"), Decimal("0"), False
            raw_id = json.dumps(dict(row), sort_keys=True, default=str)
        event_id = hashlib.sha256(raw_id.encode()).hexdigest()
        event = StrategyEvent(event_id, self.name, kind or "unknown", symbol, side, observed)
        processed = bool(self.store and self.store.event_seen(event_id, self.name, self.mode))
        if self.store is not None:
            self.store.observe_lead_order(
                self.name, self.version, self.mode, self.settings.portfolio_id,
                event, row, seen_at, processed,
            )
        if processed:
            return None
        metrics: dict[str, Any] = {"order_age_ms": age}

        def skip(reason: str) -> StrategyEvent:
            self.record_event_decision(event, "skipped", reason, metrics)
            return StrategyEvent(event_id, self.name, "skip_" + reason, symbol, side, observed)

        if (not symbol or kind is None or observed <= 0 or observed > seen_at
                or not valid_values or executed <= 0 or average <= 0):
            return skip("invalid_order")
        if symbol in self.settings.blocked_symbols:
            return skip("blocked_symbol")
        if self.settings.allowed_symbols and symbol not in self.settings.allowed_symbols:
            return skip("not_allowed")
        if observed < self.settings.event_not_before_ms:
            return skip("before_cutover")
        if kind == "open" and age > self.settings.max_entry_delay_seconds * 1000:
            return skip("stale")
        metrics.update(lead_price=str(average), lead_notional=str(executed * average))
        if kind == "open" and executed * average < self.settings.min_lead_notional:
            return skip("notional")
        signal = None
        if kind == "open":
            try:
                mark = market.mark_price(symbol)
            except Exception:
                self.record_event_decision(event, "error", "market_data_error", metrics)
                raise
            deviation = abs(mark / average - Decimal("1"))
            metrics.update(mark_price=str(mark), lead_price_deviation=str(deviation))
            if deviation > self.settings.max_lead_price_deviation:
                return skip("price_deviation")
            if int(time.time() * 1000) - observed > self.settings.max_entry_delay_seconds * 1000:
                return skip("stale")
            try:
                candles = market.candles(symbol, self.interval, self.candle_limit)
            except Exception:
                self.record_event_decision(event, "error", "market_data_error", metrics)
                raise
            stop = self._stop(mark, side, candles)
            if int(time.time() * 1000) - observed > self.settings.max_entry_delay_seconds * 1000:
                return skip("stale")
            score = max(Decimal("0"), Decimal("100") - Decimal(age) / Decimal("60000") * Decimal("5"))
            signal = Signal(self.name, self.version, symbol, side, mark, stop, score, "lead_open", observed,
                            metrics, self.settings.margin_per_trade)
        self.record_event_decision(event, "eligible", "", metrics)
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
        deadline = time.monotonic() + 15
        self._received_at.clear()
        result: list[Mapping[str, Any]] = []
        page_signatures: set[str] = set()
        for page in range(1, self.settings.max_pages + 1):
            try:
                payload = self._fetch_page(start_ms, end_ms, page, deadline)
                data = payload.get("data") or {}
                rows = data.get("list")
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise RuntimeError("Binance lead API returned invalid order list")
                if not rows:
                    break
                signature = json.dumps(rows, sort_keys=True, default=str)
                if signature in page_signatures:
                    raise RuntimeError("Binance lead API repeated a page")
                page_signatures.add(signature)
                received_at = int(time.time() * 1000)
                for row in rows:
                    self._received_at.setdefault(
                        json.dumps(row, sort_keys=True, default=str), received_at,
                    )
                result.extend(rows)
                total = data.get("total")
                if total is not None:
                    if len(result) >= int(total):
                        break
                elif len(rows) < self.settings.page_size:
                    break
                if page == self.settings.max_pages:
                    logging.warning("lead fetch truncated strategy=%s pages=%s rows=%s total=%s",
                                    self.name, page, len(result), total)
            except Exception:
                if not result:
                    raise
                logging.exception("lead fetch incomplete strategy=%s page=%s retained_rows=%s",
                                  self.name, page, len(result))
                break
        logging.info("lead fetch strategy=%s pages=%s rows=%s window_seconds=%s",
                     self.name, page, len(result), (end_ms - start_ms) // 1000)
        return result

    def _fetch_page(self, start_ms: int, end_ms: int, page: int,
                    deadline: float) -> Mapping[str, Any]:
        body = json.dumps({"portfolioId": self.settings.portfolio_id, "pageNumber": page,
                           "pageSize": self.settings.page_size, "startTime": start_ms,
                           "endTime": end_ms}, separators=(",", ":")).encode()
        request = urllib.request.Request(
            f"{self.settings.web_base_url.rstrip('/')}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history",
            data=body, method="POST", headers={"User-Agent": "bn-strategy-platform/1.0",
                                                "Content-Type": "application/json", "clienttype": "web", "lang": "en"})
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Binance lead API fetch budget exhausted")
            try:
                with urllib.request.urlopen(request, timeout=min(5, remaining)) as response:
                    payload = json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                if attempt == 0 and (exc.code == 429 or exc.code >= 500):
                    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
                    continue
                raise RuntimeError(f"Binance lead API HTTP error {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError):
                if attempt == 0:
                    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
                    continue
                raise
            if payload.get("code") == "000000" and payload.get("success", False):
                return payload
            if attempt == 0 and payload.get("code") == "11012005":
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
                continue
            raise RuntimeError(f"Binance lead API rejected request: {payload.get('code')}")
        raise RuntimeError("Binance lead API retry budget exhausted")
