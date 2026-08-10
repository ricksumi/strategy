#!/usr/bin/env python3
"""Live Binance USD-M futures bot for strategy bn-stra-high-risk-1."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


getcontext().prec = 28

STRATEGY_NAME = "bn-stra-high-risk-1"
MAINNET_BASE_URL = "https://fapi.binance.com"
TESTNET_BASE_URL = "https://testnet.binancefuture.com"


@dataclass(frozen=True)
class BotConfig:
    strategy_mode: str
    symbols: tuple[str, ...]
    interval: str
    leverage: int
    allocation_fraction: Decimal
    stop_loss_roi: Decimal
    breakeven_roi: Decimal
    profit_lock_roi: Decimal
    fee_rate: Decimal
    trailing_activation_roi: Decimal
    trailing_callback: Decimal
    pullback_entry_pct: Decimal
    pullback_confirm_pct: Decimal
    pullback_signal_wait_seconds: int
    ema_fast: int
    ema_slow: int
    adx_period: int
    adx_min: Decimal
    atr_period: int
    atr_min_pct: Decimal
    atr_max_pct: Decimal
    atr_full_size_max_pct: Decimal
    atr_reduced_size_max_pct: Decimal
    atr_reduced_size_factor: Decimal
    atr_high_size_factor: Decimal
    atr_confirm_factor: Decimal
    max_ema_atr_distance: Decimal
    contract_position_filter: bool
    crowded_short_global_max: Decimal
    crowded_short_top_min: Decimal
    crowded_long_global_min: Decimal
    crowded_long_top_max: Decimal
    stop_update_min_pct: Decimal
    daily_stop_limit: int
    cooldown_seconds: int
    poll_seconds: int
    kline_limit: int
    working_type: str
    position_side: str
    dry_run: bool
    testnet: bool
    recv_window: int
    hermes_enabled: bool = False
    hermes_socket_path: str = ""
    hermes_target: str = "weixin"

    @classmethod
    def from_file(cls, path: str) -> "BotConfig":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return cls(
            strategy_mode=str(raw.get("strategy_mode", "core")).lower(),
            symbols=tuple(str(symbol).upper() for symbol in raw.get("symbols", [])),
            interval=str(raw.get("interval", "5m")),
            leverage=int(raw.get("leverage", 5)),
            allocation_fraction=Decimal(str(raw.get("allocation_fraction", "0.2"))),
            stop_loss_roi=Decimal(str(raw.get("stop_loss_roi", "0.10"))),
            breakeven_roi=Decimal(str(raw.get("breakeven_roi", "0.10"))),
            profit_lock_roi=Decimal(str(raw.get("profit_lock_roi", "0"))),
            fee_rate=Decimal(str(raw.get("fee_rate", "0.0004"))),
            trailing_activation_roi=Decimal(str(raw.get("trailing_activation_roi", "0.20"))),
            trailing_callback=Decimal(str(raw.get("trailing_callback", "0.015"))),
            pullback_entry_pct=Decimal(str(raw.get("pullback_entry_pct", "0"))),
            pullback_confirm_pct=Decimal(str(raw.get("pullback_confirm_pct", "0"))),
            pullback_signal_wait_seconds=int(raw.get("pullback_signal_wait_seconds", 0)),
            ema_fast=int(raw.get("ema_fast", 20)),
            ema_slow=int(raw.get("ema_slow", 60)),
            adx_period=int(raw.get("adx_period", 14)),
            adx_min=Decimal(str(raw.get("adx_min", "20"))),
            atr_period=int(raw.get("atr_period", 14)),
            atr_min_pct=Decimal(str(raw.get("atr_min_pct", "0"))),
            atr_max_pct=Decimal(str(raw.get("atr_max_pct", "0"))),
            atr_full_size_max_pct=Decimal(str(raw.get("atr_full_size_max_pct", "0.03"))),
            atr_reduced_size_max_pct=Decimal(str(raw.get("atr_reduced_size_max_pct", "0.04"))),
            atr_reduced_size_factor=Decimal(str(raw.get("atr_reduced_size_factor", "0.70"))),
            atr_high_size_factor=Decimal(str(raw.get("atr_high_size_factor", "0.40"))),
            atr_confirm_factor=Decimal(str(raw.get("atr_confirm_factor", "0.15"))),
            max_ema_atr_distance=Decimal(str(raw.get("max_ema_atr_distance", "1.5"))),
            contract_position_filter=bool(raw.get("contract_position_filter", True)),
            crowded_short_global_max=Decimal(str(raw.get("crowded_short_global_max", "0.65"))),
            crowded_short_top_min=Decimal(str(raw.get("crowded_short_top_min", "1.20"))),
            crowded_long_global_min=Decimal(str(raw.get("crowded_long_global_min", "1.55"))),
            crowded_long_top_max=Decimal(str(raw.get("crowded_long_top_max", "0.83"))),
            stop_update_min_pct=Decimal(str(raw.get("stop_update_min_pct", "0.002"))),
            daily_stop_limit=int(raw.get("daily_stop_limit", 3)),
            cooldown_seconds=int(raw.get("cooldown_seconds", 600)),
            poll_seconds=int(raw.get("poll_seconds", 15)),
            kline_limit=int(raw.get("kline_limit", 200)),
            working_type=str(raw.get("working_type", "MARK_PRICE")).upper(),
            position_side=str(raw.get("position_side", "BOTH")).upper(),
            dry_run=bool(raw.get("dry_run", True)),
            testnet=bool(raw.get("testnet", True)),
            recv_window=int(raw.get("recv_window", 5000)),
            hermes_enabled=bool(raw.get("hermes_enabled", False)),
            hermes_socket_path=str(raw.get("hermes_socket_path", "")),
            hermes_target=str(raw.get("hermes_target", "weixin")),
        )

    def validate(self) -> None:
        if not self.symbols:
            raise ValueError("symbols cannot be empty")
        if self.strategy_mode not in {"core", "high_vol"}:
            raise ValueError("strategy_mode must be core or high_vol")
        if self.interval not in {"5m", "15m"}:
            raise ValueError("bn-stra-high-risk-1 supports 5m or 15m K lines")
        if self.leverage <= 0:
            raise ValueError("leverage must be positive")
        if self.allocation_fraction <= 0 or self.allocation_fraction > 1:
            raise ValueError("allocation_fraction must be in (0, 1]")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be lower than ema_slow")
        if self.fee_rate < 0:
            raise ValueError("fee_rate cannot be negative")
        if self.profit_lock_roi < 0 or self.profit_lock_roi >= self.breakeven_roi:
            raise ValueError("profit_lock_roi must be non-negative and lower than breakeven_roi")
        if self.pullback_entry_pct < 0:
            raise ValueError("pullback_entry_pct cannot be negative")
        if self.pullback_confirm_pct < 0:
            raise ValueError("pullback_confirm_pct cannot be negative")
        if self.pullback_signal_wait_seconds < 0:
            raise ValueError("pullback_signal_wait_seconds cannot be negative")
        if self.atr_period <= 0:
            raise ValueError("atr_period must be positive")
        if self.atr_min_pct < 0 or self.atr_max_pct < 0:
            raise ValueError("atr_min_pct and atr_max_pct cannot be negative")
        if self.atr_max_pct and self.atr_min_pct > self.atr_max_pct:
            raise ValueError("atr_min_pct cannot be greater than atr_max_pct")
        if not (self.atr_full_size_max_pct <= self.atr_reduced_size_max_pct <= self.atr_max_pct):
            raise ValueError("ATR sizing thresholds must be ordered and no greater than atr_max_pct")
        if not (0 < self.atr_reduced_size_factor <= 1 and 0 < self.atr_high_size_factor <= 1):
            raise ValueError("ATR size factors must be in (0, 1]")
        if self.atr_confirm_factor < 0 or self.max_ema_atr_distance <= 0:
            raise ValueError("ATR confirmation factor must be non-negative and EMA distance must be positive")
        if self.stop_update_min_pct < 0:
            raise ValueError("stop_update_min_pct cannot be negative")
        if self.working_type not in {"MARK_PRICE", "CONTRACT_PRICE"}:
            raise ValueError("working_type must be MARK_PRICE or CONTRACT_PRICE")
        if self.position_side != "BOTH":
            raise ValueError("bn-stra-high-risk-1 currently supports one-way mode only: position_side=BOTH")
        if self.hermes_enabled and (not self.hermes_socket_path or not self.hermes_target):
            raise ValueError("hermes_socket_path and hermes_target are required when Hermes notifications are enabled")


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    close_time: int


@dataclass(frozen=True)
class SymbolRules:
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal


@dataclass
class PositionState:
    symbol: str
    side: str | None = None
    entry_price: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    best_price: Decimal = Decimal("0")
    stop_price: Decimal = Decimal("0")
    stop_order_id: int | None = None
    stop_client_id: str | None = None
    stop_reason: str = "stop_loss"
    breakeven_done: bool = False
    trailing_active: bool = False
    pending_signal_side: str | None = None
    pending_signal_price: Decimal = Decimal("0")
    pending_signal_until: float = 0
    pending_confirm_pct: Decimal = Decimal("0")
    pending_pullback_reached: bool = False
    pending_pullback_extreme: Decimal = Decimal("0")
    cooldown_until: float = 0
    daily_stop_day: str = ""
    daily_stop_count: int = 0
    daily_limit_logged: bool = False
    opened_at_ms: int = 0
    initial_margin: Decimal = Decimal("0")


class HermesNotifier:
    def __init__(self, enabled: bool, socket_path: str, target: str) -> None:
        self.enabled = enabled
        self.socket_path = socket_path
        self.target = target

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        request = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": "submit",
            "params": {"target": self.target, "message": message, "media_path": None},
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(self.socket_path)
                client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
                response = b""
                while not response.endswith(b"\n"):
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    response += chunk
            result = json.loads(response)
            if "error" in result:
                raise RuntimeError(result["error"].get("message", "Hermes RPC error"))
            logging.info("Hermes notification queued job_id=%s", result.get("result", {}).get("job_id"))
        except Exception as exc:
            logging.warning("Hermes notification failed: %s", exc)


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, recv_window: int) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window

    def public_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, params or {}, signed=False)

    def signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = self.recv_window
        query = urllib.parse.urlencode(payload)
        payload["signature"] = hmac.new(self.api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        return self._request(method, path, payload, signed=True)

    def _request(self, method: str, path: str, params: dict[str, Any], signed: bool) -> Any:
        encoded = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}"
        data = None
        if method in {"GET", "DELETE"} and encoded:
            url = f"{url}?{encoded}"
        elif method == "POST":
            data = encoded.encode("utf-8")

        headers = {"User-Agent": "bn-stra-high-risk-1/1.0"}
        if signed:
            headers["X-MBX-APIKEY"] = self.api_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance API error {exc.code}: {body}") from exc


class BnStraHighRisk1:
    def __init__(self, config: BotConfig, client: BinanceClient) -> None:
        self.config = config
        self.client = client
        self.rules: dict[str, SymbolRules] = {}
        self.states = {symbol: PositionState(symbol=symbol) for symbol in config.symbols}
        self.managed_symbols = list(config.symbols)
        self.tz = ZoneInfo("Asia/Shanghai")
        self.state_path = Path(os.environ.get("BN_STRA_STATE_FILE", ".bn-stra-high-risk-1-state.json"))
        self.notifier = HermesNotifier(
            config.hermes_enabled and not config.dry_run,
            config.hermes_socket_path,
            config.hermes_target,
        )
        self.load_runtime_state()

    def run_forever(self) -> None:
        self.config.validate()
        self.ensure_one_way_mode()
        self.discover_existing_positions()
        self.rules = self.load_symbol_rules()
        self.set_leverage_for_all()
        logging.info("Starting %s for symbols=%s dry_run=%s", STRATEGY_NAME, self.config.symbols, self.config.dry_run)
        while True:
            started = time.time()
            for symbol in self.managed_symbols:
                try:
                    self.tick_symbol(symbol)
                except Exception:
                    logging.exception("Tick failed for %s", symbol)
            elapsed = time.time() - started
            time.sleep(max(1, self.config.poll_seconds - elapsed))

    def run_once(self) -> None:
        self.config.validate()
        self.ensure_one_way_mode()
        self.discover_existing_positions()
        self.rules = self.load_symbol_rules()
        self.set_leverage_for_all()
        logging.info("Running one %s scan for symbols=%s dry_run=%s", STRATEGY_NAME, self.config.symbols, self.config.dry_run)
        for symbol in self.managed_symbols:
            self.tick_symbol(symbol)

    def tick_symbol(self, symbol: str) -> None:
        state = self.states[symbol]
        self.reset_daily_counter_if_needed(state)
        position = self.get_position(symbol)
        position_amt = Decimal(position.get("positionAmt", "0"))
        if position_amt != 0:
            self.sync_state_from_position(state, position)
            mark_price = self.get_mark_price(symbol)
            self.manage_open_position(state, mark_price)
            return

        if state.quantity != 0:
            self.on_position_closed(state)

        if symbol not in self.config.symbols:
            return

        if state.daily_stop_count >= self.config.daily_stop_limit:
            if not state.daily_limit_logged:
                logging.info("%s daily stop limit reached: %s", symbol, state.daily_stop_count)
                state.daily_limit_logged = True
            return
        if time.time() < state.cooldown_until:
            return

        candles = self.fetch_candles(symbol)
        signal = strategy_signal(
            candles,
            self.config.ema_fast,
            self.config.ema_slow,
            self.config.adx_period,
            self.config.adx_min,
            self.config.atr_period,
            self.config.atr_min_pct,
            self.config.atr_max_pct,
        )
        if signal == "none":
            self.clear_pending_signal(state)
            return
        atr_pct = atr_percent(candles, self.config.atr_period)
        if not entry_near_ema(candles, self.config.ema_fast, atr_pct, self.config.max_ema_atr_distance):
            logging.info("%s signal=%s blocked: price too far from EMA%s", symbol, signal, self.config.ema_fast)
            self.clear_pending_signal(state)
            return
        if self.config.contract_position_filter:
            global_ratio, top_ratio = self.get_contract_position_ratios(symbol)
            if not contract_position_allows(
                signal,
                global_ratio,
                top_ratio,
                self.config.crowded_short_global_max,
                self.config.crowded_short_top_min,
                self.config.crowded_long_global_min,
                self.config.crowded_long_top_max,
            ):
                logging.info(
                    "%s signal=%s blocked: contract positioning global_ls=%s top_position_ls=%s",
                    symbol,
                    signal,
                    global_ratio,
                    top_ratio,
                )
                self.clear_pending_signal(state)
                return
        confirm_pct = dynamic_confirm_pct(self.config.pullback_confirm_pct, atr_pct, self.config.atr_confirm_factor)
        if not self.pullback_entry_ready(state, signal, candles[-1].close, confirm_pct):
            return
        size_factor = atr_size_factor(
            atr_pct,
            self.config.atr_full_size_max_pct,
            self.config.atr_reduced_size_max_pct,
            self.config.atr_reduced_size_factor,
            self.config.atr_high_size_factor,
        )
        self.open_position(symbol, signal, size_factor)

    def open_position(self, symbol: str, side: str, size_factor: Decimal = Decimal("1")) -> None:
        equity = self.get_total_usdt_equity()
        margin = self.margin_per_symbol(equity) * size_factor
        mark_price = self.get_mark_price(symbol)
        qty = round_to_step((margin * Decimal(self.config.leverage)) / mark_price, self.rules[symbol].step_size)
        if qty < self.rules[symbol].min_qty:
            logging.warning("%s quantity %s below minQty %s", symbol, qty, self.rules[symbol].min_qty)
            return

        order_side = "BUY" if side == "long" else "SELL"
        logging.info("%s opening %s qty=%s margin=%s size_factor=%s mark=%s", symbol, side, qty, margin, size_factor, mark_price)
        order = self.place_market_order(symbol, order_side, qty)
        entry_price = Decimal(str(order.get("avgPrice", "0"))) if order else mark_price
        if entry_price <= 0:
            entry_price = mark_price
        stop_price = initial_stop_price(entry_price, side, self.config.stop_loss_roi, self.config.leverage)
        stop_price = round_stop_price(stop_price, self.rules[symbol].tick_size, side)
        try:
            stop_order = self.place_stop_order(symbol, side, qty, stop_price, "initial")
        except Exception:
            logging.exception("%s stop order failed after market entry; emergency closing position", symbol)
            self.emergency_close_position(symbol, side, qty)
            raise

        state = self.states[symbol]
        state.side = side
        state.entry_price = entry_price
        state.quantity = qty
        state.best_price = entry_price
        state.stop_price = stop_price
        state.stop_order_id = int(stop_order.get("algoId")) if stop_order and stop_order.get("algoId") else None
        state.stop_client_id = stop_order.get("clientAlgoId") if stop_order else None
        state.stop_reason = "stop_loss"
        state.breakeven_done = False
        state.trailing_active = False
        state.opened_at_ms = int(time.time() * 1000)
        state.initial_margin = margin
        self.clear_pending_signal(state)
        self.save_runtime_state()
        logging.info(
            "%s opened %s entry=%s qty=%s initial_stop=%s stop_algo_id=%s stop_client_id=%s margin=%s equity=%s",
            symbol,
            side,
            entry_price,
            qty,
            stop_price,
            state.stop_order_id,
            state.stop_client_id,
            margin,
            equity,
        )
        self.notify_position_opened(state)

    def margin_per_symbol(self, equity: Decimal) -> Decimal:
        return equity / Decimal(len(self.config.symbols))

    def manage_open_position(self, state: PositionState, mark_price: Decimal) -> None:
        assert state.side is not None
        if state.side == "long":
            state.best_price = max(state.best_price, mark_price)
        else:
            state.best_price = min(state.best_price, mark_price)

        new_stop = state.stop_price
        breakeven_trigger = profit_trigger_price(state.entry_price, state.side, self.config.breakeven_roi, self.config.leverage)
        trailing_trigger = profit_trigger_price(
            state.entry_price, state.side, self.config.trailing_activation_roi, self.config.leverage
        )
        old_stop = state.stop_price
        old_stop_order_id = state.stop_order_id
        old_stop_client_id = state.stop_client_id
        move_reasons = []

        if reached_profit_trigger(mark_price, state.side, breakeven_trigger):
            state.breakeven_done = True
            profit_lock_stop = profit_trigger_price(
                state.entry_price, state.side, self.config.profit_lock_roi, self.config.leverage
            )
            new_stop = improve_stop(new_stop, profit_lock_stop, state.side)
            move_reasons.append("profit_lock")

        if reached_profit_trigger(mark_price, state.side, trailing_trigger):
            state.trailing_active = True
            if state.side == "long":
                trailing_stop = state.best_price * (Decimal("1") - self.config.trailing_callback)
            else:
                trailing_stop = state.best_price * (Decimal("1") + self.config.trailing_callback)
            new_stop = improve_stop(new_stop, trailing_stop, state.side)
            move_reasons.append("trailing")

        new_stop = round_stop_price(new_stop, self.rules[state.symbol].tick_size, state.side)
        if stop_improved_by(new_stop, state.stop_price, state.side, self.config.stop_update_min_pct):
            if state.trailing_active:
                state.stop_reason = "trailing_stop"
            elif state.breakeven_done:
                state.stop_reason = "break_even"
            self.replace_stop_order(state, new_stop)
            logging.info(
                (
                    "%s stop moved reason=%s side=%s entry=%s mark=%s best=%s margin_roi=%s "
                    "breakeven_trigger=%s trailing_trigger=%s old_stop=%s new_stop=%s "
                    "old_algo_id=%s new_algo_id=%s old_client_id=%s new_client_id=%s stop_reason=%s"
                ),
                state.symbol,
                "+".join(move_reasons) if move_reasons else "improved",
                state.side,
                state.entry_price,
                mark_price,
                state.best_price,
                margin_roi(mark_price, state.entry_price, state.side, self.config.leverage),
                breakeven_trigger,
                trailing_trigger,
                old_stop,
                state.stop_price,
                old_stop_order_id,
                state.stop_order_id,
                old_stop_client_id,
                state.stop_client_id,
                state.stop_reason,
            )

    def replace_stop_order(self, state: PositionState, new_stop: Decimal) -> None:
        if state.stop_order_id is not None:
            self.cancel_stop_order(state.symbol, state.stop_order_id)
        order = self.place_stop_order(state.symbol, state.side or "long", state.quantity, new_stop, "managed")
        state.stop_price = new_stop
        state.stop_order_id = int(order.get("algoId")) if order and order.get("algoId") else None
        state.stop_client_id = order.get("clientAlgoId") if order else None

    def on_position_closed(self, state: PositionState) -> None:
        close_side = state.side
        close_entry = state.entry_price
        close_qty = state.quantity
        close_stop_price = state.stop_price
        close_stop_order_id = state.stop_order_id
        close_stop_client_id = state.stop_client_id
        close_opened_at_ms = state.opened_at_ms
        close_margin = state.initial_margin
        logging.info(
            "%s position closed side=%s entry=%s qty=%s last_stop=%s stop_algo_id=%s stop_client_id=%s stop_reason=%s; entering cooldown",
            state.symbol,
            close_side,
            close_entry,
            close_qty,
            close_stop_price,
            close_stop_order_id,
            close_stop_client_id,
            state.stop_reason,
        )
        stop_filled = self.was_stop_order_filled(state)
        if not stop_filled and state.stop_reason == "stop_loss" and state.stop_client_id:
            stop_filled = True
            logging.info("%s stop order not confirmed yet; counting stop_loss conservatively", state.symbol)
        if stop_filled:
            state.daily_stop_count += 1
            self.save_runtime_state()
            logging.info("%s stop count today=%s", state.symbol, state.daily_stop_count)
        self.cancel_open_orders(state.symbol)
        self.cancel_algo_open_orders(state.symbol)
        self.notify_position_closed(state, close_opened_at_ms, close_margin)
        state.side = None
        state.entry_price = Decimal("0")
        state.quantity = Decimal("0")
        state.best_price = Decimal("0")
        state.stop_price = Decimal("0")
        state.stop_order_id = None
        state.stop_client_id = None
        state.stop_reason = "stop_loss"
        state.breakeven_done = False
        state.trailing_active = False
        state.opened_at_ms = 0
        state.initial_margin = Decimal("0")
        state.cooldown_until = time.time() + self.config.cooldown_seconds
        self.save_runtime_state()
        logging.info("%s cooldown_until=%s", state.symbol, datetime.fromtimestamp(state.cooldown_until, self.tz).isoformat())

    def notify_position_opened(self, state: PositionState) -> None:
        assert state.side is not None
        breakeven_trigger = profit_trigger_price(
            state.entry_price, state.side, self.config.breakeven_roi, self.config.leverage
        )
        trailing_trigger = profit_trigger_price(
            state.entry_price, state.side, self.config.trailing_activation_roi, self.config.leverage
        )
        self.notifier.send(
            "\n".join(
                [
                    f"[OPEN] {state.symbol} {state.side.upper()} {self.config.leverage}x",
                    f"Entry: {format_decimal(state.entry_price)}",
                    f"Quantity: {format_decimal(state.quantity)}",
                    f"Margin: {state.initial_margin:.4f} USDT",
                    f"Notional: {(state.entry_price * state.quantity):.4f} USDT",
                    f"Initial stop: {format_decimal(state.stop_price)}",
                    f"Profit-lock trigger: {format_decimal(breakeven_trigger)} (+{self.config.breakeven_roi * 100}% ROI)",
                    f"Locked ROI after trigger: +{self.config.profit_lock_roi * 100}%",
                    f"Trailing trigger: {format_decimal(trailing_trigger)} (+{self.config.trailing_activation_roi * 100}% ROI)",
                ]
            )
        )

    def notify_position_closed(self, state: PositionState, opened_at_ms: int, initial_margin: Decimal) -> None:
        now_ms = int(time.time() * 1000)
        realized = Decimal("0")
        commission = Decimal("0")
        funding = Decimal("0")
        complete = False
        if not self.config.dry_run and opened_at_ms > 0:
            try:
                rows = self.client.signed_request(
                    "GET",
                    "/fapi/v1/income",
                    {"symbol": state.symbol, "startTime": max(0, opened_at_ms - 5000), "endTime": now_ms, "limit": 1000},
                )
                for row in rows:
                    value = Decimal(str(row.get("income", "0")))
                    income_type = row.get("incomeType")
                    if income_type == "REALIZED_PNL":
                        realized += value
                    elif income_type == "COMMISSION":
                        commission += value
                    elif income_type == "FUNDING_FEE":
                        funding += value
                complete = True
            except Exception as exc:
                logging.warning("%s PnL summary query failed: %s", state.symbol, exc)
        net = realized + commission + funding
        roi = (net / initial_margin * Decimal("100")) if complete and initial_margin > 0 else None
        duration = max(0, (now_ms - opened_at_ms) // 1000) if opened_at_ms else 0
        pnl_label = f"{net:+.4f} USDT" if complete else "unavailable"
        roi_label = f"{roi:+.2f}%" if roi is not None else "unavailable"
        self.notifier.send(
            "\n".join(
                [
                    f"[CLOSED] {state.symbol} {(state.side or 'unknown').upper()}",
                    f"Reason: {state.stop_reason}",
                    f"Entry: {format_decimal(state.entry_price)}",
                    f"Quantity: {format_decimal(state.quantity)}",
                    f"Realized PnL: {realized:+.4f} USDT" if complete else "Realized PnL: unavailable",
                    f"Commission: {commission:+.4f} USDT" if complete else "Commission: unavailable",
                    f"Funding: {funding:+.4f} USDT" if complete else "Funding: unavailable",
                    f"Net PnL: {pnl_label}",
                    f"Margin ROI: {roi_label}",
                    f"Duration: {duration // 3600}h {(duration % 3600) // 60}m {duration % 60}s",
                ]
            )
        )

    def pullback_entry_ready(
        self, state: PositionState, side: str, signal_price: Decimal, confirm_pct: Decimal | None = None
    ) -> bool:
        if self.config.pullback_entry_pct <= 0:
            return True

        confirm_pct = self.config.pullback_confirm_pct if confirm_pct is None else confirm_pct
        now = time.time()
        if state.pending_signal_side != side or now >= state.pending_signal_until:
            state.pending_signal_side = side
            state.pending_signal_price = signal_price
            state.pending_signal_until = now + self.config.pullback_signal_wait_seconds
            state.pending_confirm_pct = confirm_pct
            state.pending_pullback_reached = False
            state.pending_pullback_extreme = Decimal("0")
            target = pullback_target_price(signal_price, side, self.config.pullback_entry_pct)
            logging.info(
                "%s signal=%s waiting pullback signal_price=%s target=%s confirm_pct=%s wait_until=%s",
                state.symbol,
                side,
                signal_price,
                target,
                state.pending_confirm_pct,
                datetime.fromtimestamp(state.pending_signal_until, self.tz).isoformat(),
            )

        mark_price = self.get_mark_price(state.symbol)
        target = pullback_target_price(state.pending_signal_price, side, self.config.pullback_entry_pct)
        if not state.pending_pullback_reached:
            if not pullback_entry_allowed(mark_price, side, state.pending_signal_price, self.config.pullback_entry_pct):
                logging.info(
                    "%s waiting pullback side=%s mark=%s signal_price=%s target=%s",
                    state.symbol,
                    side,
                    mark_price,
                    state.pending_signal_price,
                    target,
                )
                return False
            state.pending_pullback_reached = True
            state.pending_pullback_extreme = mark_price
            logging.info(
                "%s pullback reached side=%s mark=%s signal_price=%s target=%s waiting reversal confirm_pct=%s",
                state.symbol,
                side,
                mark_price,
                state.pending_signal_price,
                target,
                state.pending_confirm_pct,
            )
            if state.pending_confirm_pct <= 0:
                return True
            return False

        if side == "long":
            state.pending_pullback_extreme = min(state.pending_pullback_extreme, mark_price)
        else:
            state.pending_pullback_extreme = max(state.pending_pullback_extreme, mark_price)
        confirmation = pullback_confirmation_price(state.pending_pullback_extreme, side, state.pending_confirm_pct)
        if pullback_reversal_confirmed(mark_price, side, state.pending_pullback_extreme, state.pending_confirm_pct):
            logging.info(
                "%s pullback reversal confirmed side=%s mark=%s extreme=%s confirmation=%s signal_price=%s target=%s",
                state.symbol,
                side,
                mark_price,
                state.pending_pullback_extreme,
                confirmation,
                state.pending_signal_price,
                target,
            )
            return True
        logging.info(
            "%s waiting pullback reversal side=%s mark=%s extreme=%s confirmation=%s signal_price=%s target=%s",
            state.symbol,
            side,
            mark_price,
            state.pending_pullback_extreme,
            confirmation,
            state.pending_signal_price,
            target,
        )
        return False

    def clear_pending_signal(self, state: PositionState) -> None:
        state.pending_signal_side = None
        state.pending_signal_price = Decimal("0")
        state.pending_signal_until = 0
        state.pending_confirm_pct = Decimal("0")
        state.pending_pullback_reached = False
        state.pending_pullback_extreme = Decimal("0")

    def was_stop_order_filled(self, state: PositionState) -> bool:
        if self.config.dry_run or not state.stop_client_id:
            return False
        try:
            order = self.client.signed_request(
                "GET", "/fapi/v1/algoOrder", {"symbol": state.symbol, "clientAlgoId": state.stop_client_id}
            )
        except RuntimeError as exc:
            logging.warning("%s could not resolve close reason: %s", state.symbol, exc)
            return False
        status = order.get("status") or order.get("algoStatus")
        return status in {"FILLED", "TRIGGERED", "FINISHED"} and state.stop_reason == "stop_loss"

    def load_symbol_rules(self) -> dict[str, SymbolRules]:
        data = self.client.public_request("GET", "/fapi/v1/exchangeInfo")
        result: dict[str, SymbolRules] = {}
        wanted = set(self.managed_symbols)
        for item in data["symbols"]:
            if item["symbol"] not in wanted:
                continue
            tick_size = Decimal("0.01")
            step_size = Decimal("0.001")
            min_qty = Decimal("0")
            for filt in item["filters"]:
                if filt["filterType"] == "PRICE_FILTER":
                    tick_size = Decimal(filt["tickSize"])
                elif filt["filterType"] == "LOT_SIZE":
                    step_size = Decimal(filt["stepSize"])
                    min_qty = Decimal(filt["minQty"])
            result[item["symbol"]] = SymbolRules(tick_size=tick_size, step_size=step_size, min_qty=min_qty)
        missing = wanted - set(result)
        if missing:
            raise RuntimeError(f"Missing Binance futures symbols: {sorted(missing)}")
        return result

    def set_leverage_for_all(self) -> None:
        for symbol in self.config.symbols:
            params = {"symbol": symbol, "leverage": self.config.leverage}
            if self.config.dry_run:
                logging.info("[dry-run] set leverage %s", params)
            else:
                self.client.signed_request("POST", "/fapi/v1/leverage", params)

    def discover_existing_positions(self) -> None:
        if self.config.dry_run:
            return
        positions = self.client.signed_request("GET", "/fapi/v2/positionRisk")
        for position in positions:
            symbol = str(position.get("symbol", "")).upper()
            if not symbol or Decimal(position.get("positionAmt", "0")) == 0:
                continue
            if position.get("positionSide", "BOTH") != "BOTH":
                continue
            if symbol not in self.states:
                self.states[symbol] = PositionState(symbol=symbol)
            if symbol not in self.managed_symbols:
                self.managed_symbols.append(symbol)
                logging.warning("Managing existing position for non-entry symbol %s; new entries remain disabled", symbol)

    def ensure_one_way_mode(self) -> None:
        if self.config.dry_run:
            return
        data = self.client.signed_request("GET", "/fapi/v1/positionSide/dual")
        if str(data.get("dualSidePosition", "")).lower() == "true":
            raise RuntimeError(
                "Binance account is in hedge mode. bn-stra-high-risk-1 requires one-way mode. "
                "Switch USD-M Futures position mode to One-way before running."
            )

    def fetch_candles(self, symbol: str) -> list[Candle]:
        rows = self.client.public_request(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": self.config.interval, "limit": self.config.kline_limit}
        )
        return [
            Candle(
                open_time=int(row[0]),
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                close_time=int(row[6]),
            )
            for row in rows
        ]

    def get_mark_price(self, symbol: str) -> Decimal:
        data = self.client.public_request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return Decimal(data["markPrice"])

    def get_contract_position_ratios(self, symbol: str) -> tuple[Decimal, Decimal]:
        params = {"symbol": symbol, "period": self.config.interval, "limit": 1}
        global_rows = self.client.public_request("GET", "/futures/data/globalLongShortAccountRatio", params)
        top_rows = self.client.public_request("GET", "/futures/data/topLongShortPositionRatio", params)
        if not global_rows or not top_rows:
            raise RuntimeError(f"No contract positioning data returned for {symbol}")
        return Decimal(global_rows[-1]["longShortRatio"]), Decimal(top_rows[-1]["longShortRatio"])

    def get_total_usdt_equity(self) -> Decimal:
        if self.config.dry_run:
            return Decimal("10000")
        account = self.client.signed_request("GET", "/fapi/v2/account")
        return Decimal(account["totalMarginBalance"])

    def get_position(self, symbol: str) -> dict[str, Any]:
        if self.config.dry_run:
            return {"symbol": symbol, "positionAmt": "0", "entryPrice": "0"}
        data = self.client.signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(data, dict):
            data = [data]
        symbol_positions = [position for position in data if position.get("symbol") == symbol]
        for position in data:
            if position.get("symbol") == symbol and position.get("positionSide", "BOTH") == "BOTH":
                return position
        if not symbol_positions:
            return {"symbol": symbol, "positionAmt": "0", "entryPrice": "0", "positionSide": "BOTH"}
        nonzero = [position for position in symbol_positions if Decimal(position.get("positionAmt", "0")) != 0]
        if nonzero:
            sides = sorted(position.get("positionSide", "UNKNOWN") for position in nonzero)
            raise RuntimeError(
                f"{symbol} has non-BOTH position entries {sides}. "
                "This usually means hedge mode is enabled; switch USD-M Futures to one-way mode."
            )
        return {"symbol": symbol, "positionAmt": "0", "entryPrice": "0", "positionSide": "BOTH"}

    def sync_state_from_position(self, state: PositionState, position: dict[str, Any]) -> None:
        amt = Decimal(position.get("positionAmt", "0"))
        entry = Decimal(position.get("entryPrice", "0"))
        side = "long" if amt > 0 else "short"
        if state.quantity == 0:
            state.side = side
            state.quantity = abs(amt)
            state.entry_price = entry
            state.best_price = entry
            state.stop_price = initial_stop_price(entry, side, self.config.stop_loss_roi, self.config.leverage)
            if state.opened_at_ms <= 0:
                state.opened_at_ms = int(time.time() * 1000)
            if state.initial_margin <= 0:
                state.initial_margin = entry * abs(amt) / Decimal(self.config.leverage)
            self.save_runtime_state()

    def place_market_order(self, symbol: str, side: str, quantity: Decimal) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format_decimal(quantity),
            "newOrderRespType": "RESULT",
        }
        if self.config.dry_run:
            logging.info("[dry-run] market order %s", params)
            return {"avgPrice": str(self.get_mark_price(symbol)), "executedQty": str(quantity)}
        return self.client.signed_request("POST", "/fapi/v1/order", params)

    def emergency_close_position(self, symbol: str, position_side: str, quantity: Decimal) -> None:
        side = "SELL" if position_side == "long" else "BUY"
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format_decimal(quantity),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
        }
        if self.config.dry_run:
            logging.info("[dry-run] emergency close %s", params)
            return
        self.client.signed_request("POST", "/fapi/v1/order", params)

    def place_stop_order(self, symbol: str, position_side: str, quantity: Decimal, stop_price: Decimal, label: str) -> dict[str, Any]:
        side = "SELL" if position_side == "long" else "BUY"
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "quantity": format_decimal(quantity),
            "triggerPrice": format_decimal(stop_price),
            "reduceOnly": "true",
            "workingType": self.config.working_type,
            "clientAlgoId": client_order_id(symbol, label),
        }
        if self.config.dry_run:
            logging.info("[dry-run] stop order %s", params)
            return {"algoId": int(time.time() * 1000), "clientAlgoId": params["clientAlgoId"]}
        return self.client.signed_request("POST", "/fapi/v1/algoOrder", params)

    def cancel_stop_order(self, symbol: str, algo_id: int) -> None:
        params = {"symbol": symbol, "algoId": algo_id}
        if self.config.dry_run:
            logging.info("[dry-run] cancel algo order %s", params)
            return
        try:
            self.client.signed_request("DELETE", "/fapi/v1/algoOrder", params)
        except RuntimeError as exc:
            logging.warning("%s cancel algo order failed: %s", symbol, exc)

    def cancel_open_orders(self, symbol: str) -> None:
        params = {"symbol": symbol}
        if self.config.dry_run:
            logging.info("[dry-run] cancel open orders %s", params)
            return
        self.client.signed_request("DELETE", "/fapi/v1/allOpenOrders", params)

    def cancel_algo_open_orders(self, symbol: str) -> None:
        params = {"symbol": symbol}
        if self.config.dry_run:
            logging.info("[dry-run] cancel algo open orders %s", params)
            return
        try:
            self.client.signed_request("DELETE", "/fapi/v1/algoOpenOrders", params)
        except RuntimeError as exc:
            logging.warning("%s cancel algo open orders failed: %s", symbol, exc)

    def reset_daily_counter_if_needed(self, state: PositionState) -> None:
        today = datetime.now(self.tz).strftime("%Y-%m-%d")
        if state.daily_stop_day != today:
            state.daily_stop_day = today
            state.daily_stop_count = 0
            state.daily_limit_logged = False
            self.save_runtime_state()

    def load_runtime_state(self) -> None:
        today = datetime.now(self.tz).strftime("%Y-%m-%d")
        for state in self.states.values():
            state.daily_stop_day = today
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            counts = data.get("daily_stop_counts", {}) if data.get("day") == today else {}
            if not isinstance(counts, dict):
                raise ValueError("daily_stop_counts must be an object")
            for symbol, state in self.states.items():
                state.daily_stop_count = max(0, int(counts.get(symbol, 0)))
            active_trades = data.get("active_trades", {})
            if not isinstance(active_trades, dict):
                raise ValueError("active_trades must be an object")
            for symbol, raw_trade in active_trades.items():
                if symbol not in self.states or not isinstance(raw_trade, dict):
                    continue
                state = self.states[symbol]
                state.opened_at_ms = max(0, int(raw_trade.get("opened_at_ms", 0)))
                state.initial_margin = max(Decimal("0"), Decimal(str(raw_trade.get("initial_margin", "0"))))
            logging.info("Restored daily stop counts for %s: %s", today, counts)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logging.warning("Could not load runtime state from %s: %s", self.state_path, exc)

    def save_runtime_state(self) -> None:
        today = datetime.now(self.tz).strftime("%Y-%m-%d")
        data = {
            "day": today,
            "daily_stop_counts": {
                symbol: state.daily_stop_count
                for symbol, state in self.states.items()
                if state.daily_stop_day == today and state.daily_stop_count > 0
            },
            "active_trades": {
                symbol: {
                    "opened_at_ms": state.opened_at_ms,
                    "initial_margin": format_decimal(state.initial_margin),
                }
                for symbol, state in self.states.items()
                if state.opened_at_ms > 0
            },
        }
        temp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
        try:
            temp_path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
            temp_path.replace(self.state_path)
        except OSError as exc:
            logging.error("Could not save runtime state to %s: %s", self.state_path, exc)


def strategy_signal(
    candles: list[Candle],
    ema_fast: int,
    ema_slow: int,
    adx_period: int,
    adx_min: Decimal,
    atr_period: int = 14,
    atr_min_pct: Decimal = Decimal("0"),
    atr_max_pct: Decimal = Decimal("0"),
) -> str:
    if len(candles) < max(ema_slow, adx_period * 2, atr_period + 1):
        return "none"
    fast = ema_values([c.close for c in candles], ema_fast)[-1]
    slow = ema_values([c.close for c in candles], ema_slow)[-1]
    adx = adx_values(candles, adx_period)[-1]
    if fast is None or slow is None or adx is None or adx <= adx_min or fast == slow:
        return "none"
    atr_pct = atr_percent(candles, atr_period)
    if atr_pct is None:
        return "none"
    if atr_min_pct and atr_pct < atr_min_pct:
        return "none"
    if atr_max_pct and atr_pct > atr_max_pct:
        return "none"
    return "long" if fast > slow else "short"


def atr_percent(candles: list[Candle], period: int) -> Decimal | None:
    if len(candles) < period + 1 or candles[-1].close <= 0:
        return None
    ranges: list[Decimal] = []
    for i in range(len(candles) - period, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        ranges.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return (sum(ranges, Decimal("0")) / Decimal(period)) / candles[-1].close


def ema_values(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    multiplier = Decimal("2") / Decimal(period + 1)
    ema = sum(values[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = ema
    for idx in range(period, len(values)):
        ema = (values[idx] - ema) * multiplier + ema
        result[idx] = ema
    return result


def adx_values(candles: list[Candle], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(candles)
    if len(candles) < period * 2:
        return result
    tr = [Decimal("0")]
    plus_dm = [Decimal("0")]
    minus_dm = [Decimal("0")]
    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        high_move = cur.high - prev.high
        low_move = prev.low - cur.low
        tr.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        plus_dm.append(high_move if high_move > low_move and high_move > 0 else Decimal("0"))
        minus_dm.append(low_move if low_move > high_move and low_move > 0 else Decimal("0"))

    smooth_tr = sum(tr[1 : period + 1], Decimal("0"))
    smooth_plus = sum(plus_dm[1 : period + 1], Decimal("0"))
    smooth_minus = sum(minus_dm[1 : period + 1], Decimal("0"))
    dx: list[Decimal | None] = [None] * len(candles)
    for i in range(period, len(candles)):
        if i > period:
            smooth_tr = smooth_tr - smooth_tr / Decimal(period) + tr[i]
            smooth_plus = smooth_plus - smooth_plus / Decimal(period) + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / Decimal(period) + minus_dm[i]
        if smooth_tr == 0:
            continue
        plus_di = Decimal("100") * smooth_plus / smooth_tr
        minus_di = Decimal("100") * smooth_minus / smooth_tr
        denominator = plus_di + minus_di
        if denominator:
            dx[i] = Decimal("100") * abs(plus_di - minus_di) / denominator

    first = period * 2 - 1
    seed = [value for value in dx[period : first + 1] if value is not None]
    if len(seed) < period:
        return result
    adx = sum(seed, Decimal("0")) / Decimal(period)
    result[first] = adx
    for i in range(first + 1, len(candles)):
        if dx[i] is not None:
            adx = (adx * Decimal(period - 1) + dx[i]) / Decimal(period)
            result[i] = adx
    return result


def initial_stop_price(entry: Decimal, side: str, stop_loss_roi: Decimal, leverage: int) -> Decimal:
    move = stop_loss_roi / Decimal(leverage)
    return entry * (Decimal("1") - move) if side == "long" else entry * (Decimal("1") + move)


def profit_trigger_price(entry: Decimal, side: str, roi: Decimal, leverage: int) -> Decimal:
    move = roi / Decimal(leverage)
    return entry * (Decimal("1") + move) if side == "long" else entry * (Decimal("1") - move)


def breakeven_stop_price(entry: Decimal, side: str, fee_rate: Decimal) -> Decimal:
    fee_buffer = fee_rate * Decimal("2")
    return entry * (Decimal("1") + fee_buffer) if side == "long" else entry * (Decimal("1") - fee_buffer)


def reached_profit_trigger(price: Decimal, side: str, trigger: Decimal) -> bool:
    return price >= trigger if side == "long" else price <= trigger


def pullback_target_price(signal_price: Decimal, side: str, pullback_pct: Decimal) -> Decimal:
    multiplier = Decimal("1") - pullback_pct if side == "long" else Decimal("1") + pullback_pct
    return signal_price * multiplier


def pullback_entry_allowed(price: Decimal, side: str, signal_price: Decimal, pullback_pct: Decimal) -> bool:
    target = pullback_target_price(signal_price, side, pullback_pct)
    return price <= target if side == "long" else price >= target


def pullback_confirmation_price(extreme_price: Decimal, side: str, confirm_pct: Decimal) -> Decimal:
    multiplier = Decimal("1") + confirm_pct if side == "long" else Decimal("1") - confirm_pct
    return extreme_price * multiplier


def pullback_reversal_confirmed(price: Decimal, side: str, extreme_price: Decimal, confirm_pct: Decimal) -> bool:
    confirmation = pullback_confirmation_price(extreme_price, side, confirm_pct)
    return price >= confirmation if side == "long" else price <= confirmation


def margin_roi(price: Decimal, entry: Decimal, side: str, leverage: int) -> Decimal:
    if entry <= 0:
        return Decimal("0")
    if side == "long":
        return (price / entry - Decimal("1")) * Decimal(leverage)
    return (entry / price - Decimal("1")) * Decimal(leverage) if price > 0 else Decimal("0")


def improve_stop(current: Decimal, candidate: Decimal, side: str) -> Decimal:
    return max(current, candidate) if side == "long" else min(current, candidate)


def stop_improved(new: Decimal, old: Decimal, side: str) -> bool:
    return new > old if side == "long" else new < old


def stop_improved_by(new: Decimal, old: Decimal, side: str, minimum_pct: Decimal) -> bool:
    if not stop_improved(new, old, side):
        return False
    if old <= 0 or minimum_pct <= 0:
        return True
    improvement = (new - old) / old if side == "long" else (old - new) / old
    return improvement >= minimum_pct


def atr_size_factor(
    atr_pct: Decimal | None,
    full_size_max_pct: Decimal,
    reduced_size_max_pct: Decimal,
    reduced_size_factor: Decimal,
    high_size_factor: Decimal,
) -> Decimal:
    if atr_pct is None or atr_pct <= full_size_max_pct:
        return Decimal("1")
    if atr_pct <= reduced_size_max_pct:
        return reduced_size_factor
    return high_size_factor


def dynamic_confirm_pct(base_pct: Decimal, atr_pct: Decimal | None, atr_factor: Decimal) -> Decimal:
    if atr_pct is None:
        return base_pct
    return max(base_pct, atr_pct * atr_factor)


def entry_near_ema(
    candles: list[Candle], ema_period: int, atr_pct: Decimal | None, max_atr_distance: Decimal
) -> bool:
    if not candles or atr_pct is None or atr_pct <= 0:
        return False
    ema = ema_values([c.close for c in candles], ema_period)[-1]
    close = candles[-1].close
    if ema is None or close <= 0:
        return False
    atr_value = atr_pct * close
    return abs(close - ema) <= atr_value * max_atr_distance


def contract_position_allows(
    side: str,
    global_ratio: Decimal,
    top_ratio: Decimal,
    crowded_short_global_max: Decimal,
    crowded_short_top_min: Decimal,
    crowded_long_global_min: Decimal,
    crowded_long_top_max: Decimal,
) -> bool:
    if side == "short" and global_ratio <= crowded_short_global_max and top_ratio >= crowded_short_top_min:
        return False
    if side == "long" and global_ratio >= crowded_long_global_min and top_ratio <= crowded_long_top_max:
        return False
    return True


def round_stop_price(price: Decimal, tick_size: Decimal, side: str) -> Decimal:
    rounding = ROUND_DOWN if side == "long" else ROUND_UP
    return (price / tick_size).to_integral_value(rounding=rounding) * tick_size


def round_to_step(value: Decimal, step_size: Decimal) -> Decimal:
    return (value / step_size).to_integral_value(rounding=ROUND_DOWN) * step_size


def format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")


def client_order_id(symbol: str, label: str) -> str:
    short_symbol = symbol.lower().replace("usdt", "")
    short_label = label[:2]
    return f"bshr1_{short_symbol}_{short_label}_{int(time.time() * 1000) % 1000000000000}"


def load_env_file(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def build_client(config: BotConfig) -> BinanceClient:
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not config.dry_run and (not api_key or not api_secret):
        raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET are required when dry_run=false")
    return BinanceClient(api_key, api_secret, TESTNET_BASE_URL if config.testnet else MAINNET_BASE_URL, config.recv_window)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live bot for strategy bn-stra-high-risk-1")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(message)s")
    config_path = Path(args.config)
    load_env_file(config_path.resolve().parent / ".env")
    config = BotConfig.from_file(args.config)
    bot = BnStraHighRisk1(config, build_client(config))
    if args.once:
        bot.run_once()
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
