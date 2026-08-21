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
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    margin_per_trade: Decimal
    entry_max_slippage_pct: Decimal
    stop_loss_roi: Decimal
    breakeven_roi: Decimal
    profit_lock_roi: Decimal
    fee_rate: Decimal
    trailing_activation_roi: Decimal
    trailing_callback: Decimal
    trailing_tier_2_roi: Decimal
    trailing_tier_2_callback: Decimal
    trailing_tier_3_roi: Decimal
    trailing_tier_3_callback: Decimal
    partial_take_1_roi: Decimal
    partial_take_1_fraction: Decimal
    partial_take_2_roi: Decimal
    partial_take_2_fraction: Decimal
    pullback_entry_pct: Decimal
    pullback_confirm_pct: Decimal
    pullback_signal_wait_seconds: int
    require_signal_recross: bool
    signal_recross_wait_seconds: int
    signal_recross_confirm_polls: int
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
    atr_stop_multiplier: Decimal
    max_ema_atr_distance: Decimal
    max_pullback_atr_distance: Decimal
    contract_position_filter: bool
    crowded_short_global_max: Decimal
    crowded_short_top_min: Decimal
    crowded_long_global_min: Decimal
    crowded_long_top_max: Decimal
    top_long_veto_max: Decimal
    top_short_veto_min: Decimal
    stop_update_min_pct: Decimal
    daily_stop_limit: int
    global_daily_stop_limit: int
    max_concurrent_positions: int
    cooldown_seconds: int
    poll_seconds: int
    kline_limit: int
    working_type: str
    position_side: str
    dry_run: bool
    testnet: bool
    recv_window: int
    insufficient_margin_notice_cooldown_seconds: int = 600
    hermes_enabled: bool = False
    hermes_socket_path: str = ""
    hermes_target: str = "weixin"
    paper_signals_after_global_stop: bool = False
    paper_trading_only: bool = False
    global_daily_net_loss_limit: Decimal = Decimal("0")
    entry_signal_max_distance_pct: Decimal = Decimal("0.003")
    entry_signal_max_atr_factor: Decimal = Decimal("0.25")
    dynamic_universe_enabled: bool = False
    active_symbol_limit: int = 60
    universe_refresh_seconds: int = 900
    universe_min_quote_volume: Decimal = Decimal("20000000")
    universe_max_spread_pct: Decimal = Decimal("0.0015")
    universe_min_listing_days: int = 30
    universe_min_24h_range_pct: Decimal = Decimal("0.03")
    trade_db_path: str = ".bn-stra-high-risk-1-trades.sqlite3"

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
            margin_per_trade=Decimal(str(raw.get("margin_per_trade", "200"))),
            entry_max_slippage_pct=Decimal(str(raw.get("entry_max_slippage_pct", "0.002"))),
            stop_loss_roi=Decimal(str(raw.get("stop_loss_roi", "0.10"))),
            breakeven_roi=Decimal(str(raw.get("breakeven_roi", "0.10"))),
            profit_lock_roi=Decimal(str(raw.get("profit_lock_roi", "0"))),
            fee_rate=Decimal(str(raw.get("fee_rate", "0.0004"))),
            trailing_activation_roi=Decimal(str(raw.get("trailing_activation_roi", "0.20"))),
            trailing_callback=Decimal(str(raw.get("trailing_callback", "0.015"))),
            trailing_tier_2_roi=Decimal(str(raw.get("trailing_tier_2_roi", "0.50"))),
            trailing_tier_2_callback=Decimal(str(raw.get("trailing_tier_2_callback", "0.03"))),
            trailing_tier_3_roi=Decimal(str(raw.get("trailing_tier_3_roi", "0.80"))),
            trailing_tier_3_callback=Decimal(str(raw.get("trailing_tier_3_callback", "0.02"))),
            partial_take_1_roi=Decimal(str(raw.get("partial_take_1_roi", "0.25"))),
            partial_take_1_fraction=Decimal(str(raw.get("partial_take_1_fraction", "0.25"))),
            partial_take_2_roi=Decimal(str(raw.get("partial_take_2_roi", "0.40"))),
            partial_take_2_fraction=Decimal(str(raw.get("partial_take_2_fraction", "0.25"))),
            pullback_entry_pct=Decimal(str(raw.get("pullback_entry_pct", "0"))),
            pullback_confirm_pct=Decimal(str(raw.get("pullback_confirm_pct", "0"))),
            pullback_signal_wait_seconds=int(raw.get("pullback_signal_wait_seconds", 0)),
            require_signal_recross=bool(raw.get("require_signal_recross", False)),
            signal_recross_wait_seconds=int(raw.get("signal_recross_wait_seconds", 180)),
            signal_recross_confirm_polls=int(raw.get("signal_recross_confirm_polls", 2)),
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
            atr_stop_multiplier=Decimal(str(raw.get("atr_stop_multiplier", "1.5"))),
            max_ema_atr_distance=Decimal(str(raw.get("max_ema_atr_distance", "1.5"))),
            max_pullback_atr_distance=Decimal(str(raw.get("max_pullback_atr_distance", "1.5"))),
            contract_position_filter=bool(raw.get("contract_position_filter", True)),
            crowded_short_global_max=Decimal(str(raw.get("crowded_short_global_max", "0.65"))),
            crowded_short_top_min=Decimal(str(raw.get("crowded_short_top_min", "1.20"))),
            crowded_long_global_min=Decimal(str(raw.get("crowded_long_global_min", "1.55"))),
            crowded_long_top_max=Decimal(str(raw.get("crowded_long_top_max", "0.83"))),
            top_long_veto_max=Decimal(str(raw.get("top_long_veto_max", "0.80"))),
            top_short_veto_min=Decimal(str(raw.get("top_short_veto_min", "1.25"))),
            stop_update_min_pct=Decimal(str(raw.get("stop_update_min_pct", "0.002"))),
            daily_stop_limit=int(raw.get("daily_stop_limit", 3)),
            global_daily_stop_limit=int(raw.get("global_daily_stop_limit", 0)),
            max_concurrent_positions=int(raw.get("max_concurrent_positions", 0)),
            cooldown_seconds=int(raw.get("cooldown_seconds", 600)),
            poll_seconds=int(raw.get("poll_seconds", 15)),
            kline_limit=int(raw.get("kline_limit", 200)),
            working_type=str(raw.get("working_type", "MARK_PRICE")).upper(),
            position_side=str(raw.get("position_side", "BOTH")).upper(),
            dry_run=bool(raw.get("dry_run", True)),
            testnet=bool(raw.get("testnet", True)),
            recv_window=int(raw.get("recv_window", 5000)),
            insufficient_margin_notice_cooldown_seconds=int(
                raw.get("insufficient_margin_notice_cooldown_seconds", 600)
            ),
            hermes_enabled=bool(raw.get("hermes_enabled", False)),
            hermes_socket_path=str(raw.get("hermes_socket_path", "")),
            hermes_target=str(raw.get("hermes_target", "weixin")),
            paper_signals_after_global_stop=bool(raw.get("paper_signals_after_global_stop", False)),
            paper_trading_only=bool(raw.get("paper_trading_only", False)),
            global_daily_net_loss_limit=Decimal(
                str(raw.get("global_daily_net_loss_limit", "0"))
            ),
            entry_signal_max_distance_pct=Decimal(
                str(raw.get("entry_signal_max_distance_pct", "0.003"))
            ),
            entry_signal_max_atr_factor=Decimal(
                str(raw.get("entry_signal_max_atr_factor", "0.25"))
            ),
            dynamic_universe_enabled=bool(raw.get("dynamic_universe_enabled", False)),
            active_symbol_limit=int(raw.get("active_symbol_limit", 60)),
            universe_refresh_seconds=int(raw.get("universe_refresh_seconds", 900)),
            universe_min_quote_volume=Decimal(
                str(raw.get("universe_min_quote_volume", "20000000"))
            ),
            universe_max_spread_pct=Decimal(
                str(raw.get("universe_max_spread_pct", "0.0015"))
            ),
            universe_min_listing_days=int(raw.get("universe_min_listing_days", 30)),
            universe_min_24h_range_pct=Decimal(
                str(raw.get("universe_min_24h_range_pct", "0.03"))
            ),
            trade_db_path=str(
                raw.get("trade_db_path", ".bn-stra-high-risk-1-trades.sqlite3")
            ),
        )

    def validate(self) -> None:
        if not self.symbols:
            raise ValueError("symbols cannot be empty")
        if self.active_symbol_limit <= 0:
            raise ValueError("active_symbol_limit must be positive")
        if self.universe_refresh_seconds < 60:
            raise ValueError("universe_refresh_seconds must be at least 60")
        if self.universe_min_quote_volume < 0:
            raise ValueError("universe_min_quote_volume cannot be negative")
        if not 0 < self.universe_max_spread_pct < 1:
            raise ValueError("universe_max_spread_pct must be in (0, 1)")
        if self.universe_min_listing_days < 0:
            raise ValueError("universe_min_listing_days cannot be negative")
        if self.universe_min_24h_range_pct < 0:
            raise ValueError("universe_min_24h_range_pct cannot be negative")
        if not self.trade_db_path:
            raise ValueError("trade_db_path cannot be empty")
        if self.strategy_mode not in {"core", "high_vol"}:
            raise ValueError("strategy_mode must be core or high_vol")
        if self.interval not in {"5m", "15m"}:
            raise ValueError("bn-stra-high-risk-1 supports 5m or 15m K lines")
        if self.leverage <= 0:
            raise ValueError("leverage must be positive")
        if self.allocation_fraction <= 0 or self.allocation_fraction > 1:
            raise ValueError("allocation_fraction must be in (0, 1]")
        if self.margin_per_trade <= 0:
            raise ValueError("margin_per_trade must be positive")
        if self.entry_max_slippage_pct <= 0 or self.entry_max_slippage_pct > Decimal("0.05"):
            raise ValueError("entry_max_slippage_pct must be in (0, 0.05]")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be lower than ema_slow")
        if self.fee_rate < 0:
            raise ValueError("fee_rate cannot be negative")
        if self.profit_lock_roi < 0 or self.profit_lock_roi >= self.breakeven_roi:
            raise ValueError("profit_lock_roi must be non-negative and lower than breakeven_roi")
        if not (self.trailing_activation_roi < self.trailing_tier_2_roi < self.trailing_tier_3_roi):
            raise ValueError("trailing ROI tiers must be strictly increasing")
        if not (self.trailing_callback >= self.trailing_tier_2_callback >= self.trailing_tier_3_callback > 0):
            raise ValueError("trailing callbacks must be positive and non-increasing")
        if not (0 < self.partial_take_1_roi < self.partial_take_2_roi):
            raise ValueError("partial take-profit ROI tiers must be positive and increasing")
        if not (0 < self.partial_take_1_fraction < 1 and 0 < self.partial_take_2_fraction < 1):
            raise ValueError("partial take-profit fractions must be in (0, 1)")
        if self.partial_take_1_fraction + self.partial_take_2_fraction >= 1:
            raise ValueError("partial take-profit fractions must leave a trailing position")
        if self.pullback_entry_pct < 0:
            raise ValueError("pullback_entry_pct cannot be negative")
        if self.pullback_confirm_pct < 0:
            raise ValueError("pullback_confirm_pct cannot be negative")
        if self.pullback_signal_wait_seconds < 0:
            raise ValueError("pullback_signal_wait_seconds cannot be negative")
        if self.signal_recross_wait_seconds <= 0:
            raise ValueError("signal_recross_wait_seconds must be positive")
        if self.signal_recross_confirm_polls <= 0:
            raise ValueError("signal_recross_confirm_polls must be positive")
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
        if self.atr_confirm_factor < 0 or self.max_ema_atr_distance <= 0 or self.max_pullback_atr_distance <= 0:
            raise ValueError("ATR confirmation factor must be non-negative and ATR distance limits must be positive")
        if self.atr_stop_multiplier <= 0:
            raise ValueError("atr_stop_multiplier must be positive")
        if self.stop_update_min_pct < 0:
            raise ValueError("stop_update_min_pct cannot be negative")
        if self.daily_stop_limit <= 0:
            raise ValueError("daily_stop_limit must be positive")
        if self.global_daily_stop_limit < 0:
            raise ValueError("global_daily_stop_limit cannot be negative")
        if self.global_daily_net_loss_limit < 0:
            raise ValueError("global_daily_net_loss_limit cannot be negative")
        if self.entry_signal_max_distance_pct <= 0:
            raise ValueError("entry_signal_max_distance_pct must be positive")
        if self.entry_signal_max_atr_factor <= 0:
            raise ValueError("entry_signal_max_atr_factor must be positive")
        if self.max_concurrent_positions < 0:
            raise ValueError("max_concurrent_positions cannot be negative")
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
    pending_reversal_confirmed: bool = False
    pending_recross_count: int = 0
    pending_recross_candle_open_after_ms: int = 0
    rejected_signal_side: str | None = None
    rejected_signal_price: Decimal = Decimal("0")
    cooldown_until: float = 0
    daily_stop_day: str = ""
    daily_stop_count: int = 0
    daily_limit_logged: bool = False
    opened_at_ms: int = 0
    initial_margin: Decimal = Decimal("0")
    initial_quantity: Decimal = Decimal("0")
    partial_take_1_done: bool = False
    partial_take_2_done: bool = False
    last_insufficient_margin_notice: float = 0


@dataclass
class PaperPosition:
    symbol: str
    side: str
    entry_price: Decimal
    quantity: Decimal
    remaining_quantity: Decimal
    best_price: Decimal
    stop_price: Decimal
    stop_reason: str
    opened_at_ms: int
    initial_margin: Decimal
    realized_gross_pnl: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    partial_take_1_done: bool = False
    partial_take_2_done: bool = False
    breakeven_done: bool = False
    trailing_active: bool = False


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


class TradeStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        try:
            self.connection = sqlite3.connect(path, timeout=5)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_key TEXT NOT NULL UNIQUE,
                    strategy TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    leverage INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    opened_at_ms INTEGER NOT NULL,
                    closed_at_ms INTEGER,
                    entry_price TEXT NOT NULL,
                    exit_price TEXT,
                    initial_quantity TEXT NOT NULL,
                    remaining_quantity TEXT NOT NULL,
                    initial_margin TEXT NOT NULL,
                    initial_stop_price TEXT NOT NULL,
                    last_stop_price TEXT NOT NULL,
                    best_price TEXT NOT NULL,
                    close_reason TEXT,
                    realized_gross_pnl TEXT,
                    commission TEXT,
                    funding TEXT,
                    net_pnl TEXT,
                    margin_roi TEXT,
                    duration_seconds INTEGER,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trades_opened_at ON trades(opened_at_ms);
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE TABLE IF NOT EXISTS trade_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_at_ms INTEGER NOT NULL,
                    price TEXT,
                    quantity TEXT,
                    remaining_quantity TEXT,
                    stop_price TEXT,
                    realized_gross_pnl TEXT,
                    commission TEXT,
                    details_json TEXT,
                    FOREIGN KEY(trade_key) REFERENCES trades(trade_key)
                );
                CREATE INDEX IF NOT EXISTS idx_trade_events_key_time
                    ON trade_events(trade_key, event_at_ms);
                """
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            logging.error("Could not initialize trade database %s: %s", path, exc)
            self.connection = None

    @staticmethod
    def trade_key(mode: str, symbol: str, opened_at_ms: int) -> str:
        return f"{mode}:{symbol}:{opened_at_ms}"

    def record_open(
        self,
        mode: str,
        symbol: str,
        side: str,
        leverage: int,
        opened_at_ms: int,
        entry_price: Decimal,
        quantity: Decimal,
        margin: Decimal,
        stop_price: Decimal,
        best_price: Decimal,
    ) -> None:
        if self.connection is None:
            return
        key = self.trade_key(mode, symbol, opened_at_ms)
        now_ms = int(time.time() * 1000)
        try:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO trades (
                    trade_key, strategy, mode, symbol, side, leverage, status,
                    opened_at_ms, entry_price, initial_quantity, remaining_quantity,
                    initial_margin, initial_stop_price, last_stop_price, best_price,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, STRATEGY_NAME, mode, symbol, side, leverage, opened_at_ms,
                    format_decimal(entry_price), format_decimal(quantity), format_decimal(quantity),
                    format_decimal(margin), format_decimal(stop_price), format_decimal(stop_price),
                    format_decimal(best_price), now_ms, now_ms,
                ),
            )
            if cursor.rowcount:
                self._record_event(key, "open", opened_at_ms, entry_price, quantity, quantity, stop_price)
            self.connection.commit()
        except sqlite3.Error as exc:
            logging.error("Trade database open write failed for %s: %s", key, exc)

    def record_position_update(
        self,
        mode: str,
        symbol: str,
        opened_at_ms: int,
        best_price: Decimal,
        stop_price: Decimal,
        remaining_quantity: Decimal,
    ) -> None:
        if self.connection is None:
            return
        key = self.trade_key(mode, symbol, opened_at_ms)
        try:
            self.connection.execute(
                """
                UPDATE trades SET best_price=?, last_stop_price=?, remaining_quantity=?, updated_at_ms=?
                WHERE trade_key=? AND status='open'
                """,
                (
                    format_decimal(best_price), format_decimal(stop_price),
                    format_decimal(remaining_quantity), int(time.time() * 1000), key,
                ),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            logging.error("Trade database position update failed for %s: %s", key, exc)

    def record_event(
        self,
        mode: str,
        symbol: str,
        opened_at_ms: int,
        event_type: str,
        price: Decimal | None = None,
        quantity: Decimal | None = None,
        remaining_quantity: Decimal | None = None,
        stop_price: Decimal | None = None,
        realized_gross_pnl: Decimal | None = None,
        commission: Decimal | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.connection is None:
            return
        key = self.trade_key(mode, symbol, opened_at_ms)
        try:
            self._record_event(
                key, event_type, int(time.time() * 1000), price, quantity,
                remaining_quantity, stop_price, realized_gross_pnl, commission, details,
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            logging.error("Trade database event write failed for %s: %s", key, exc)

    def record_close(
        self,
        mode: str,
        symbol: str,
        opened_at_ms: int,
        exit_price: Decimal | None,
        best_price: Decimal,
        last_stop_price: Decimal,
        close_reason: str,
        realized_gross_pnl: Decimal | None,
        commission: Decimal | None,
        funding: Decimal | None,
        net_pnl: Decimal | None,
        margin_roi_value: Decimal | None,
        remaining_quantity: Decimal,
    ) -> None:
        if self.connection is None:
            return
        key = self.trade_key(mode, symbol, opened_at_ms)
        closed_at_ms = int(time.time() * 1000)
        try:
            self.connection.execute(
                """
                UPDATE trades SET status='closed', closed_at_ms=?, exit_price=?,
                    remaining_quantity=?, last_stop_price=?, best_price=?, close_reason=?,
                    realized_gross_pnl=?, commission=?, funding=?, net_pnl=?, margin_roi=?,
                    duration_seconds=?, updated_at_ms=?
                WHERE trade_key=?
                """,
                (
                    closed_at_ms, self._decimal(exit_price), "0",
                    format_decimal(last_stop_price), format_decimal(best_price), close_reason,
                    self._decimal(realized_gross_pnl), self._decimal(commission),
                    self._decimal(funding), self._decimal(net_pnl),
                    self._decimal(margin_roi_value), max(0, (closed_at_ms - opened_at_ms) // 1000),
                    closed_at_ms, key,
                ),
            )
            self._record_event(
                key, "close", closed_at_ms, exit_price, remaining_quantity,
                Decimal("0"), last_stop_price, realized_gross_pnl, commission,
                {"reason": close_reason, "net_pnl": self._decimal(net_pnl)},
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            logging.error("Trade database close write failed for %s: %s", key, exc)

    def closed_net_pnl(self, mode: str, start_ms: int, end_ms: int) -> Decimal:
        if self.connection is None:
            return Decimal("0")
        try:
            rows = self.connection.execute(
                """
                SELECT net_pnl FROM trades
                WHERE mode=? AND status='closed' AND closed_at_ms>=? AND closed_at_ms<?
                    AND net_pnl IS NOT NULL
                """,
                (mode, start_ms, end_ms),
            ).fetchall()
            return sum((Decimal(str(row[0])) for row in rows), Decimal("0"))
        except (sqlite3.Error, ArithmeticError, ValueError) as exc:
            logging.error("Trade database daily net PnL query failed: %s", exc)
            return Decimal("0")

    def _record_event(
        self,
        trade_key: str,
        event_type: str,
        event_at_ms: int,
        price: Decimal | None = None,
        quantity: Decimal | None = None,
        remaining_quantity: Decimal | None = None,
        stop_price: Decimal | None = None,
        realized_gross_pnl: Decimal | None = None,
        commission: Decimal | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        assert self.connection is not None
        self.connection.execute(
            """
            INSERT INTO trade_events (
                trade_key, event_type, event_at_ms, price, quantity, remaining_quantity,
                stop_price, realized_gross_pnl, commission, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_key, event_type, event_at_ms, self._decimal(price), self._decimal(quantity),
                self._decimal(remaining_quantity), self._decimal(stop_price),
                self._decimal(realized_gross_pnl), self._decimal(commission),
                json.dumps(details, sort_keys=True) if details else None,
            ),
        )

    @staticmethod
    def _decimal(value: Decimal | None) -> str | None:
        return format_decimal(value) if value is not None else None


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, recv_window: int) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window
        self.used_weight_1m = 0
        self.backoff_until = 0.0
        self.last_weight_warning = 0.0

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
        remaining_backoff = self.backoff_until - time.time()
        if remaining_backoff > 0:
            logging.warning("Binance rate-limit backoff active; sleeping %.1fs", remaining_backoff)
            time.sleep(remaining_backoff)

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
                self._record_rate_limit_headers(resp.headers)
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            self._record_rate_limit_headers(exc.headers)
            if exc.code in {418, 429}:
                retry_after = max(1, int(exc.headers.get("Retry-After", "60")))
                self.backoff_until = max(self.backoff_until, time.time() + retry_after)
                logging.error(
                    "Binance rate limit HTTP %s; backing off for %ss used_weight_1m=%s",
                    exc.code,
                    retry_after,
                    self.used_weight_1m,
                )
            raise RuntimeError(f"Binance API error {exc.code}: {body}") from exc

    def _record_rate_limit_headers(self, headers: Any) -> None:
        raw_weight = headers.get("X-MBX-USED-WEIGHT-1M") if headers else None
        if raw_weight is None:
            return
        try:
            self.used_weight_1m = int(raw_weight)
        except (TypeError, ValueError):
            return
        now = time.time()
        if self.used_weight_1m >= 1920 and now - self.last_weight_warning >= 60:
            logging.warning(
                "Binance request weight is above 80%%: used_weight_1m=%s limit=2400",
                self.used_weight_1m,
            )
            self.last_weight_warning = now


class BnStraHighRisk1:
    def __init__(self, config: BotConfig, client: BinanceClient) -> None:
        self.config = config
        self.client = client
        self.rules: dict[str, SymbolRules] = {}
        self.states = {symbol: PositionState(symbol=symbol) for symbol in config.symbols}
        self.managed_symbols = list(config.symbols)
        self.entry_symbols = set(config.symbols)
        self.existing_position_symbols: set[str] = set()
        self.next_universe_refresh = 0.0
        self.leverage_symbols: set[str] = set()
        self.candle_cache: dict[tuple[str, str, int], tuple[float, list[Candle]]] = {}
        self.entry_gate_cache: dict[str, tuple[int, str]] = {}
        self.tz = ZoneInfo("Asia/Shanghai")
        self.state_path = Path(os.environ.get("BN_STRA_STATE_FILE", ".bn-stra-high-risk-1-state.json"))
        self.trade_store = TradeStore(os.environ.get("BN_STRA_TRADE_DB", config.trade_db_path))
        self.notifier = HermesNotifier(
            config.hermes_enabled and not config.dry_run,
            config.hermes_socket_path,
            config.hermes_target,
        )
        self.global_daily_stop_day = datetime.now(self.tz).strftime("%Y-%m-%d")
        self.global_daily_stop_count = 0
        self.global_daily_net_pnl = Decimal("0")
        self.global_limit_logged = False
        self.global_limit_notified = False
        self.global_net_loss_limit_logged = False
        self.global_net_loss_limit_notified = False
        self.position_limit_logged = False
        self.paper_positions: dict[str, PaperPosition] = {}
        self.load_runtime_state()
        self.refresh_global_daily_net_pnl()
        for position in self.paper_positions.values():
            self.record_paper_open(position)

    def run_forever(self) -> None:
        self.config.validate()
        self.ensure_one_way_mode()
        self.discover_existing_positions()
        if self.config.dynamic_universe_enabled:
            self.refresh_active_universe(force=True)
        else:
            self.rules = self.load_symbol_rules()
        self.set_leverage_for_all()
        logging.info(
            "Starting %s active_symbols=%s dynamic_universe=%s dry_run=%s",
            STRATEGY_NAME,
            tuple(self.entry_symbols),
            self.config.dynamic_universe_enabled,
            self.config.dry_run,
        )
        while True:
            started = time.time()
            self.refresh_active_universe()
            for symbol in tuple(self.managed_symbols):
                try:
                    self.tick_symbol(symbol)
                except Exception:
                    logging.exception("Tick failed for %s", symbol)
            elapsed = time.time() - started
            if elapsed > self.config.poll_seconds:
                logging.warning(
                    "Scan exceeded poll interval symbols=%s elapsed=%.2fs target=%ss used_weight_1m=%s",
                    len(self.managed_symbols),
                    elapsed,
                    self.config.poll_seconds,
                    self.client.used_weight_1m,
                )
            time.sleep(max(1, self.config.poll_seconds - elapsed))

    def run_once(self) -> None:
        self.config.validate()
        self.ensure_one_way_mode()
        self.discover_existing_positions()
        if self.config.dynamic_universe_enabled:
            self.refresh_active_universe(force=True)
        else:
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

        if symbol not in self.entry_symbols and state.pending_signal_side is None:
            return

        paper_position = self.paper_positions.get(symbol)
        if paper_position is not None:
            self.manage_paper_position(paper_position, self.get_mark_price(symbol))
            return

        global_paused = self.global_entries_paused()
        paper_mode = self.config.paper_trading_only or (
            global_paused and self.config.paper_signals_after_global_stop
        )
        if global_paused:
            self.log_and_notify_global_pause()
        if not paper_mode and not self.entry_limits_allow(state):
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
        candle_close_time = candles[-1].close_time
        cached_gate = self.entry_gate_cache.get(symbol)
        if cached_gate is not None and cached_gate[0] == candle_close_time:
            return
        signal_price = candles[-1].close
        if state.rejected_signal_side == signal and state.rejected_signal_price == signal_price:
            return
        if state.rejected_signal_price != signal_price:
            state.rejected_signal_side = None
            state.rejected_signal_price = Decimal("0")
        atr_pct = atr_percent(candles, self.config.atr_period)
        if not entry_near_ema(candles, self.config.ema_fast, atr_pct, self.config.max_ema_atr_distance):
            close = candles[-1].close
            ema = ema_values([c.close for c in candles], self.config.ema_fast)[-1]
            distance_pct = abs(close - ema) / close if ema is not None and close > 0 else Decimal("0")
            distance_limit_pct = (atr_pct or Decimal("0")) * self.config.max_ema_atr_distance
            self.entry_gate_cache[symbol] = (candle_close_time, "ema_distance")
            logging.info(
                "%s signal=%s blocked: price too far from EMA%s "
                "price=%s ema=%s distance=%.3f%% limit=%.3f%% atr=%.3f%% max_atr_distance=%s",
                symbol,
                signal,
                self.config.ema_fast,
                close,
                ema,
                distance_pct * Decimal("100"),
                distance_limit_pct * Decimal("100"),
                (atr_pct or Decimal("0")) * Decimal("100"),
                self.config.max_ema_atr_distance,
            )
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
                self.config.top_long_veto_max,
                self.config.top_short_veto_min,
            ):
                self.entry_gate_cache[symbol] = (candle_close_time, "contract_positioning")
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
        max_pullback_pct = atr_pct * self.config.max_pullback_atr_distance
        if not self.pullback_entry_ready(state, signal, signal_price, confirm_pct, max_pullback_pct):
            return
        entry_price = self.get_mark_price(symbol)
        signal_distance_limit = min(
            self.config.entry_signal_max_distance_pct,
            atr_pct * self.config.entry_signal_max_atr_factor,
        )
        if not entry_signal_price_allows(
            entry_price, state.pending_signal_price, signal, signal_distance_limit
        ):
            distance = adverse_entry_signal_distance(entry_price, state.pending_signal_price, signal)
            logging.info(
                "%s signal=%s invalidated: entry too far from original signal entry=%s signal_price=%s "
                "distance=%.3f%% limit=%.3f%% atr=%.3f%%",
                symbol,
                signal,
                entry_price,
                state.pending_signal_price,
                distance * Decimal("100"),
                signal_distance_limit * Decimal("100"),
                atr_pct * Decimal("100"),
            )
            state.rejected_signal_side = signal
            state.rejected_signal_price = state.pending_signal_price
            self.clear_pending_signal(state)
            return
        base_stop_pct = self.config.stop_loss_roi / Decimal(self.config.leverage)
        stop_distance_pct = capped_atr_stop_distance(base_stop_pct, atr_pct, self.config.atr_stop_multiplier)
        if paper_mode:
            self.open_paper_position(symbol, signal, stop_distance_pct, entry_price)
        else:
            self.open_position(symbol, signal, stop_distance_pct)

    def global_entries_paused(self) -> bool:
        return self.stop_count_entries_paused() or self.net_loss_entries_paused()

    def stop_count_entries_paused(self) -> bool:
        return bool(
            self.config.global_daily_stop_limit
            and self.global_daily_stop_count > self.config.global_daily_stop_limit
        )

    def net_loss_entries_paused(self) -> bool:
        return bool(
            self.config.global_daily_net_loss_limit
            and (
                self.global_net_loss_limit_notified
                or self.global_daily_net_pnl <= -self.config.global_daily_net_loss_limit
            )
        )

    def refresh_global_daily_net_pnl(self) -> None:
        now = datetime.now(self.tz)
        start = datetime(now.year, now.month, now.day, tzinfo=self.tz)
        end = start + timedelta(days=1)
        self.global_daily_net_pnl = self.trade_store.closed_net_pnl(
            "real", int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        )

    def log_and_notify_global_pause(self) -> None:
        if self.stop_count_entries_paused() and not self.global_limit_logged:
            logging.warning(
                "Global daily stop limit exceeded: count=%s limit=%s; real entries paused until next day",
                self.global_daily_stop_count,
                self.config.global_daily_stop_limit,
            )
            self.global_limit_logged = True
        if self.stop_count_entries_paused():
            self.notify_global_daily_stop_limit()
        if self.net_loss_entries_paused() and not self.global_net_loss_limit_logged:
            logging.warning(
                "Global daily net loss limit reached: net_pnl=%s loss_limit=%s; "
                "real entries paused until next day",
                self.global_daily_net_pnl,
                self.config.global_daily_net_loss_limit,
            )
            self.global_net_loss_limit_logged = True
        if self.net_loss_entries_paused():
            self.notify_global_daily_net_loss_limit()

    def entry_limits_allow(self, state: PositionState) -> bool:
        if self.global_entries_paused():
            self.log_and_notify_global_pause()
            return False

        self.global_limit_logged = False
        if state.daily_stop_count >= self.config.daily_stop_limit:
            if not state.daily_limit_logged:
                logging.info("%s daily stop limit reached: %s", state.symbol, state.daily_stop_count)
                state.daily_limit_logged = True
            return False

        active_count = sum(1 for item in self.states.values() if item.quantity != 0)
        if self.config.max_concurrent_positions and active_count >= self.config.max_concurrent_positions:
            if not self.position_limit_logged:
                logging.info(
                    "Concurrent position limit reached: active=%s limit=%s",
                    active_count,
                    self.config.max_concurrent_positions,
                )
                self.position_limit_logged = True
            return False

        self.position_limit_logged = False
        return True

    def open_position(
        self,
        symbol: str,
        side: str,
        stop_distance_pct: Decimal | None = None,
    ) -> None:
        if self.config.paper_trading_only:
            logging.error("%s real entry suppressed by paper_trading_only", symbol)
            self.clear_pending_signal(self.states[symbol])
            return
        if not self.entry_limits_allow(self.states[symbol]):
            self.clear_pending_signal(self.states[symbol])
            return
        equity, available_balance = self.get_usdt_account_balances()
        margin = self.config.margin_per_trade
        required_available = margin * (Decimal("1") + self.config.fee_rate * Decimal(self.config.leverage))
        if available_balance < required_available:
            state = self.states[symbol]
            now = time.time()
            logging.warning(
                "%s insufficient margin side=%s required=%s available=%s configured_margin=%s",
                symbol, side, required_available, available_balance, margin,
            )
            if now - state.last_insufficient_margin_notice >= self.config.insufficient_margin_notice_cooldown_seconds:
                self.notifier.send(
                    "\n".join(
                        [
                            f"[INSUFFICIENT MARGIN] {symbol} {side.upper()}",
                            f"- Required available balance: {required_available:.4f} USDT",
                            f"- Available balance: {available_balance:.4f} USDT",
                            f"- Configured margin: {margin:.4f} USDT",
                            "- Order was not placed.",
                        ]
                    )
                )
                state.last_insufficient_margin_notice = now
            self.clear_pending_signal(state)
            return
        mark_price = self.get_mark_price(symbol)
        qty = round_to_step((margin * Decimal(self.config.leverage)) / mark_price, self.rules[symbol].step_size)
        if qty < self.rules[symbol].min_qty:
            logging.warning("%s quantity %s below minQty %s", symbol, qty, self.rules[symbol].min_qty)
            return

        order_side = "BUY" if side == "long" else "SELL"
        stop_distance_pct = stop_distance_pct or self.config.stop_loss_roi / Decimal(self.config.leverage)
        logging.info(
            "%s opening %s qty=%s margin=%s stop_distance_pct=%s mark=%s",
            symbol, side, qty, margin, stop_distance_pct, mark_price,
        )
        order = self.place_entry_order(symbol, order_side, qty, mark_price, side)
        executed_qty = Decimal(str(order.get("executedQty", "0")))
        if executed_qty <= 0:
            logging.info(
                "%s entry IOC expired without a fill side=%s reference=%s max_slippage=%.3f%%",
                symbol,
                side,
                mark_price,
                self.config.entry_max_slippage_pct * Decimal("100"),
            )
            self.clear_pending_signal(self.states[symbol])
            return
        if executed_qty < qty:
            logging.warning(
                "%s entry IOC partially filled requested=%s executed=%s; protecting actual quantity",
                symbol,
                qty,
                executed_qty,
            )
            self.notifier.send(
                "\n".join(
                    [
                        f"[PARTIAL ENTRY] {symbol} {side.upper()}",
                        f"- Requested quantity: {format_decimal(qty)}",
                        f"- Executed quantity: {format_decimal(executed_qty)}",
                        "- The unfilled quantity was canceled and the filled position remains protected.",
                    ]
                )
            )
        entry_price = self.resolve_order_fill_price(symbol, order, mark_price)
        stop_price = stop_price_from_distance(entry_price, side, stop_distance_pct)
        stop_price = round_stop_price(stop_price, self.rules[symbol].tick_size, side)
        try:
            stop_order = self.place_stop_order(symbol, side, executed_qty, stop_price, "initial")
        except Exception:
            logging.exception("%s stop order failed after IOC entry; emergency closing position", symbol)
            self.emergency_close_position(symbol, side, executed_qty)
            raise

        state = self.states[symbol]
        state.side = side
        state.entry_price = entry_price
        state.quantity = executed_qty
        state.best_price = entry_price
        state.stop_price = stop_price
        state.stop_order_id = int(stop_order.get("algoId")) if stop_order and stop_order.get("algoId") else None
        state.stop_client_id = stop_order.get("clientAlgoId") if stop_order else None
        state.stop_reason = "stop_loss"
        state.breakeven_done = False
        state.trailing_active = False
        state.opened_at_ms = int(time.time() * 1000)
        state.initial_margin = entry_price * executed_qty / Decimal(self.config.leverage)
        state.initial_quantity = executed_qty
        state.partial_take_1_done = False
        state.partial_take_2_done = False
        self.clear_pending_signal(state)
        self.save_runtime_state()
        self.trade_store.record_open(
            "real", symbol, side, self.config.leverage, state.opened_at_ms,
            entry_price, executed_qty, state.initial_margin, stop_price, state.best_price,
        )
        logging.info(
            "%s opened %s entry=%s qty=%s initial_stop=%s stop_algo_id=%s stop_client_id=%s margin=%s equity=%s",
            symbol,
            side,
            entry_price,
            executed_qty,
            stop_price,
            state.stop_order_id,
            state.stop_client_id,
            margin,
            equity,
        )
        self.notify_position_opened(state)

    def margin_per_symbol(self, equity: Decimal) -> Decimal:
        return self.config.margin_per_trade

    def open_paper_position(
        self,
        symbol: str,
        side: str,
        stop_distance_pct: Decimal,
        entry_price: Decimal | None = None,
    ) -> None:
        if symbol in self.paper_positions:
            return
        active_real_positions = sum(1 for state in self.states.values() if state.quantity != 0)
        simulated_portfolio_size = active_real_positions + len(self.paper_positions)
        if self.config.max_concurrent_positions and simulated_portfolio_size >= self.config.max_concurrent_positions:
            logging.info(
                "%s paper signal blocked: simulated position limit reached active=%s limit=%s",
                symbol,
                simulated_portfolio_size,
                self.config.max_concurrent_positions,
            )
            self.clear_pending_signal(self.states[symbol])
            return
        entry_price = entry_price or self.get_mark_price(symbol)
        quantity = round_to_step(
            (self.config.margin_per_trade * Decimal(self.config.leverage)) / entry_price,
            self.rules[symbol].step_size,
        )
        if quantity < self.rules[symbol].min_qty:
            logging.warning("%s paper quantity %s below minQty %s", symbol, quantity, self.rules[symbol].min_qty)
            self.clear_pending_signal(self.states[symbol])
            return
        stop_price = round_stop_price(
            stop_price_from_distance(entry_price, side, stop_distance_pct),
            self.rules[symbol].tick_size,
            side,
        )
        position = PaperPosition(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            remaining_quantity=quantity,
            best_price=entry_price,
            stop_price=stop_price,
            stop_reason="stop_loss",
            opened_at_ms=int(time.time() * 1000),
            initial_margin=entry_price * quantity / Decimal(self.config.leverage),
            commission=entry_price * quantity * self.config.fee_rate,
        )
        self.paper_positions[symbol] = position
        self.clear_pending_signal(self.states[symbol])
        self.save_runtime_state()
        self.record_paper_open(position)
        logging.info(
            "%s paper opened side=%s entry=%s qty=%s initial_stop=%s margin=%s",
            symbol, side, entry_price, quantity, stop_price, position.initial_margin,
        )
        self.notify_paper_position_opened(position)

    def manage_paper_position(self, position: PaperPosition, mark_price: Decimal) -> None:
        stop_reached = (
            mark_price <= position.stop_price if position.side == "long" else mark_price >= position.stop_price
        )
        if stop_reached:
            self.close_paper_position(position, position.stop_price)
            return

        position.best_price = (
            max(position.best_price, mark_price)
            if position.side == "long"
            else min(position.best_price, mark_price)
        )
        current_roi = margin_roi(mark_price, position.entry_price, position.side, self.config.leverage)
        if current_roi >= self.config.partial_take_1_roi and not position.partial_take_1_done:
            self.execute_paper_partial_take_profit(
                position, 1, self.config.partial_take_1_fraction, mark_price
            )
        if current_roi >= self.config.partial_take_2_roi and not position.partial_take_2_done:
            self.execute_paper_partial_take_profit(
                position, 2, self.config.partial_take_2_fraction, mark_price
            )

        new_stop = position.stop_price
        breakeven_trigger = profit_trigger_price(
            position.entry_price, position.side, self.config.breakeven_roi, self.config.leverage
        )
        if reached_profit_trigger(mark_price, position.side, breakeven_trigger):
            position.breakeven_done = True
            new_stop = improve_stop(
                new_stop,
                profit_trigger_price(
                    position.entry_price, position.side, self.config.profit_lock_roi, self.config.leverage
                ),
                position.side,
            )

        best_roi = margin_roi(position.best_price, position.entry_price, position.side, self.config.leverage)
        callback = trailing_callback_for_roi(
            best_roi,
            self.config.trailing_activation_roi,
            self.config.trailing_callback,
            self.config.trailing_tier_2_roi,
            self.config.trailing_tier_2_callback,
            self.config.trailing_tier_3_roi,
            self.config.trailing_tier_3_callback,
        )
        if callback is not None:
            position.trailing_active = True
            trailing_stop = (
                position.best_price * (Decimal("1") - callback)
                if position.side == "long"
                else position.best_price * (Decimal("1") + callback)
            )
            new_stop = improve_stop(new_stop, trailing_stop, position.side)

        new_stop = round_stop_price(new_stop, self.rules[position.symbol].tick_size, position.side)
        if stop_improved_by(new_stop, position.stop_price, position.side, self.config.stop_update_min_pct):
            old_stop = position.stop_price
            position.stop_price = new_stop
            if position.trailing_active:
                position.stop_reason = "trailing_stop"
            elif position.breakeven_done:
                position.stop_reason = "break_even"
            logging.info(
                "%s paper stop moved side=%s mark=%s best=%s old_stop=%s new_stop=%s reason=%s",
                position.symbol, position.side, mark_price, position.best_price, old_stop, new_stop,
                position.stop_reason,
            )
            self.trade_store.record_event(
                "paper", position.symbol, position.opened_at_ms, "stop_moved",
                price=mark_price, remaining_quantity=position.remaining_quantity,
                stop_price=position.stop_price,
                details={"reason": position.stop_reason, "old_stop": format_decimal(old_stop)},
            )
        self.trade_store.record_position_update(
            "paper", position.symbol, position.opened_at_ms, position.best_price,
            position.stop_price, position.remaining_quantity,
        )
        self.save_runtime_state()

    def execute_paper_partial_take_profit(
        self, position: PaperPosition, tier: int, fraction: Decimal, fill_price: Decimal
    ) -> None:
        target_qty = round_to_step(position.quantity * fraction, self.rules[position.symbol].step_size)
        close_qty = min(position.remaining_quantity, target_qty)
        if close_qty < self.rules[position.symbol].min_qty:
            logging.warning(
                "%s paper partial tier=%s quantity=%s below minQty", position.symbol, tier, close_qty
            )
            return
        position.realized_gross_pnl += gross_pnl(
            position.entry_price, fill_price, close_qty, position.side
        )
        position.commission += fill_price * close_qty * self.config.fee_rate
        position.remaining_quantity -= close_qty
        position.partial_take_1_done = position.partial_take_1_done or tier == 1
        position.partial_take_2_done = position.partial_take_2_done or tier == 2
        logging.info(
            "%s paper partial take-profit tier=%s qty=%s fill=%s remaining=%s gross_pnl=%s",
            position.symbol, tier, close_qty, fill_price, position.remaining_quantity,
            position.realized_gross_pnl,
        )
        self.trade_store.record_event(
            "paper", position.symbol, position.opened_at_ms, f"partial_take_{tier}",
            price=fill_price, quantity=close_qty,
            remaining_quantity=position.remaining_quantity, stop_price=position.stop_price,
            realized_gross_pnl=position.realized_gross_pnl,
            commission=-position.commission,
        )
        self.trade_store.record_position_update(
            "paper", position.symbol, position.opened_at_ms, position.best_price,
            position.stop_price, position.remaining_quantity,
        )

    def close_paper_position(self, position: PaperPosition, fill_price: Decimal) -> None:
        position.realized_gross_pnl += gross_pnl(
            position.entry_price, fill_price, position.remaining_quantity, position.side
        )
        position.commission += fill_price * position.remaining_quantity * self.config.fee_rate
        net_pnl = position.realized_gross_pnl - position.commission
        duration = max(0, (int(time.time() * 1000) - position.opened_at_ms) // 1000)
        roi = net_pnl / position.initial_margin * Decimal("100") if position.initial_margin > 0 else Decimal("0")
        logging.info(
            "%s paper closed side=%s reason=%s fill=%s gross=%s commission=%s net=%s roi=%s%%",
            position.symbol, position.side, position.stop_reason, fill_price,
            position.realized_gross_pnl, position.commission, net_pnl, roi,
        )
        self.trade_store.record_close(
            "paper", position.symbol, position.opened_at_ms, fill_price,
            position.best_price, position.stop_price, position.stop_reason,
            position.realized_gross_pnl, -position.commission, Decimal("0"), net_pnl,
            roi, position.remaining_quantity,
        )
        self.notifier.send(
            "\n".join(
                [
                    f"[PAPER CLOSED - NO REAL ORDER] {position.symbol} {position.side.upper()}",
                    f"- Reason: {position.stop_reason}",
                    f"- Entry: {format_decimal(position.entry_price)}",
                    f"- Simulated exit: {format_decimal(fill_price)}",
                    f"- Initial quantity: {format_decimal(position.quantity)}",
                    f"- Estimated gross PnL: {position.realized_gross_pnl:+.4f} USDT",
                    f"- Estimated commission: {-position.commission:+.4f} USDT",
                    "- Estimated funding: +0.0000 USDT",
                    f"- Estimated net PnL: {net_pnl:+.4f} USDT",
                    f"- Simulated margin ROI: {roi:+.2f}%",
                    f"- Duration: {duration // 3600}h {(duration % 3600) // 60}m {duration % 60}s",
                ]
            )
        )
        del self.paper_positions[position.symbol]
        self.states[position.symbol].cooldown_until = time.time() + self.config.cooldown_seconds
        self.save_runtime_state()

    def record_paper_open(self, position: PaperPosition) -> None:
        self.trade_store.record_open(
            "paper", position.symbol, position.side, self.config.leverage,
            position.opened_at_ms, position.entry_price, position.quantity,
            position.initial_margin, position.stop_price, position.best_price,
        )

    def notify_paper_position_opened(self, position: PaperPosition) -> None:
        reason = (
            "Paper-only mode is enabled; no Binance entry order was submitted."
            if self.config.paper_trading_only
            else "Real entries are paused by the global daily stop limit; no Binance entry order was submitted."
        )
        self.notifier.send(
            "\n".join(
                [
                    f"[PAPER OPEN - NO REAL ORDER] {position.symbol} {position.side.upper()} {self.config.leverage}x",
                    f"- Simulated entry: {format_decimal(position.entry_price)}",
                    f"- Simulated quantity: {format_decimal(position.quantity)}",
                    f"- Margin: {position.initial_margin:.4f} USDT",
                    f"- Notional: {(position.entry_price * position.quantity):.4f} USDT",
                    f"- Initial stop: {format_decimal(position.stop_price)}",
                    f"- {reason}",
                ]
            )
        )

    def manage_open_position(self, state: PositionState, mark_price: Decimal) -> None:
        assert state.side is not None
        if state.side == "long":
            state.best_price = max(state.best_price, mark_price)
        else:
            state.best_price = min(state.best_price, mark_price)

        current_roi = margin_roi(mark_price, state.entry_price, state.side, self.config.leverage)
        if current_roi >= self.config.partial_take_1_roi and not state.partial_take_1_done:
            self.execute_partial_take_profit(state, 1, self.config.partial_take_1_fraction, mark_price)
        if current_roi >= self.config.partial_take_2_roi and not state.partial_take_2_done:
            self.execute_partial_take_profit(state, 2, self.config.partial_take_2_fraction, mark_price)

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

        best_roi = margin_roi(state.best_price, state.entry_price, state.side, self.config.leverage)
        active_callback = trailing_callback_for_roi(
            best_roi,
            self.config.trailing_activation_roi,
            self.config.trailing_callback,
            self.config.trailing_tier_2_roi,
            self.config.trailing_tier_2_callback,
            self.config.trailing_tier_3_roi,
            self.config.trailing_tier_3_callback,
        )
        if active_callback is not None:
            state.trailing_active = True
            if state.side == "long":
                trailing_stop = state.best_price * (Decimal("1") - active_callback)
            else:
                trailing_stop = state.best_price * (Decimal("1") + active_callback)
            new_stop = improve_stop(new_stop, trailing_stop, state.side)
            move_reasons.append(f"trailing_{active_callback}")

        new_stop = round_stop_price(new_stop, self.rules[state.symbol].tick_size, state.side)
        if stop_improved_by(new_stop, state.stop_price, state.side, self.config.stop_update_min_pct):
            if state.trailing_active:
                state.stop_reason = "trailing_stop"
            elif state.breakeven_done:
                state.stop_reason = "break_even"
            self.replace_stop_order(state, new_stop)
            self.save_runtime_state()
            logging.info(
                (
                    "%s stop moved reason=%s side=%s entry=%s mark=%s best=%s margin_roi=%s "
                    "breakeven_trigger=%s trailing_trigger=%s best_roi=%s callback=%s old_stop=%s new_stop=%s "
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
                best_roi,
                active_callback,
                old_stop,
                state.stop_price,
                old_stop_order_id,
                state.stop_order_id,
                old_stop_client_id,
                state.stop_client_id,
                state.stop_reason,
            )
            self.trade_store.record_event(
                "real", state.symbol, state.opened_at_ms, "stop_moved",
                price=mark_price, remaining_quantity=state.quantity, stop_price=state.stop_price,
                details={"reason": state.stop_reason, "old_stop": format_decimal(old_stop)},
            )
        self.trade_store.record_position_update(
            "real", state.symbol, state.opened_at_ms, state.best_price,
            state.stop_price, state.quantity,
        )

    def execute_partial_take_profit(
        self, state: PositionState, tier: int, fraction: Decimal, mark_price: Decimal
    ) -> None:
        target_qty = round_to_step(state.initial_quantity * fraction, self.rules[state.symbol].step_size)
        close_qty = min(state.quantity, target_qty)
        if close_qty < self.rules[state.symbol].min_qty:
            logging.warning("%s partial take-profit tier=%s quantity=%s below minQty", state.symbol, tier, close_qty)
            return
        params = {
            "symbol": state.symbol,
            "side": "SELL" if state.side == "long" else "BUY",
            "type": "MARKET",
            "quantity": format_decimal(close_qty),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
        }
        if self.config.dry_run:
            logging.info("[dry-run] partial take-profit %s", params)
            order = {"avgPrice": str(mark_price)}
        else:
            order = self.client.signed_request("POST", "/fapi/v1/order", params)
        fill_price = self.resolve_order_fill_price(state.symbol, order, mark_price)
        state.quantity -= close_qty
        state.partial_take_1_done = state.partial_take_1_done or tier == 1
        state.partial_take_2_done = state.partial_take_2_done or tier == 2
        stop_order = self.place_stop_order(state.symbol, state.side or "long", state.quantity, state.stop_price, "managed")
        old_stop_order_id = state.stop_order_id
        if old_stop_order_id is not None:
            self.cancel_stop_order(state.symbol, old_stop_order_id)
        else:
            self.cancel_algo_open_orders(state.symbol)
        state.stop_order_id = int(stop_order.get("algoId")) if stop_order and stop_order.get("algoId") else None
        state.stop_client_id = stop_order.get("clientAlgoId") if stop_order else None
        self.save_runtime_state()
        realized_estimate = (fill_price - state.entry_price) * close_qty
        if state.side == "short":
            realized_estimate = (state.entry_price - fill_price) * close_qty
        logging.info(
            "%s partial take-profit tier=%s qty=%s fill=%s remaining=%s estimated_gross_pnl=%s",
            state.symbol, tier, close_qty, fill_price, state.quantity, realized_estimate,
        )
        self.trade_store.record_event(
            "real", state.symbol, state.opened_at_ms, f"partial_take_{tier}",
            price=fill_price, quantity=close_qty, remaining_quantity=state.quantity,
            stop_price=state.stop_price, realized_gross_pnl=realized_estimate,
        )
        self.trade_store.record_position_update(
            "real", state.symbol, state.opened_at_ms, state.best_price,
            state.stop_price, state.quantity,
        )

    def replace_stop_order(self, state: PositionState, new_stop: Decimal) -> None:
        order = self.place_stop_order(state.symbol, state.side or "long", state.quantity, new_stop, "managed")
        old_stop_order_id = state.stop_order_id
        if old_stop_order_id is not None:
            self.cancel_stop_order(state.symbol, old_stop_order_id)
        state.stop_price = new_stop
        state.stop_order_id = int(order.get("algoId")) if order and order.get("algoId") else None
        state.stop_client_id = order.get("clientAlgoId") if order else None

    def resolve_order_fill_price(self, symbol: str, order: dict[str, Any] | None, fallback: Decimal) -> Decimal:
        avg_price = Decimal(str((order or {}).get("avgPrice", "0")))
        if avg_price > 0 or self.config.dry_run:
            return avg_price if avg_price > 0 else fallback

        order_id = (order or {}).get("orderId")
        if order_id is not None:
            try:
                status = self.client.signed_request(
                    "GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
                )
                avg_price = Decimal(str(status.get("avgPrice", "0")))
                if avg_price > 0:
                    return avg_price
            except Exception as exc:
                logging.warning("%s order lookup failed for order_id=%s; trying trade fills: %s", symbol, order_id, exc)

            try:
                trades = self.client.signed_request(
                    "GET", "/fapi/v1/userTrades", {"symbol": symbol, "orderId": order_id, "limit": 1000}
                )
                total_qty = sum((Decimal(str(row.get("qty", "0"))) for row in trades), Decimal("0"))
                if total_qty > 0:
                    total_notional = sum(
                        (
                            Decimal(str(row.get("price", "0"))) * Decimal(str(row.get("qty", "0")))
                            for row in trades
                        ),
                        Decimal("0"),
                    )
                    if total_notional > 0:
                        return total_notional / total_qty
            except Exception as exc:
                logging.warning("%s trade fill lookup failed for order_id=%s: %s", symbol, order_id, exc)

        logging.warning("%s order_id=%s returned no fill price; using fallback=%s", symbol, order_id, fallback)
        return fallback

    def on_position_closed(self, state: PositionState) -> None:
        close_side = state.side
        close_entry = state.entry_price
        close_qty = state.quantity
        close_stop_price = state.stop_price
        close_stop_order_id = state.stop_order_id
        close_stop_client_id = state.stop_client_id
        close_opened_at_ms = state.opened_at_ms
        close_margin = state.initial_margin
        close_best_price = state.best_price
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
        if stop_filled and state.stop_reason == "stop_loss":
            state.daily_stop_count += 1
            self.global_daily_stop_count += 1
            self.save_runtime_state()
            logging.info(
                "%s stop count today=%s global_stop_count=%s",
                state.symbol,
                state.daily_stop_count,
                self.global_daily_stop_count,
            )
            if (
                self.config.global_daily_stop_limit
                and self.global_daily_stop_count > self.config.global_daily_stop_limit
            ):
                self.notify_global_daily_stop_limit()
        self.cancel_open_orders(state.symbol)
        self.cancel_algo_open_orders(state.symbol)
        pnl_summary = self.notify_position_closed(state, close_opened_at_ms, close_margin)
        self.trade_store.record_close(
            "real", state.symbol, close_opened_at_ms, pnl_summary["exit_price"], close_best_price,
            close_stop_price, state.stop_reason,
            pnl_summary["realized"], pnl_summary["commission"], pnl_summary["funding"],
            pnl_summary["net"], pnl_summary["roi"], close_qty,
        )
        self.refresh_global_daily_net_pnl()
        if self.net_loss_entries_paused():
            self.notify_global_daily_net_loss_limit()
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
        state.initial_quantity = Decimal("0")
        state.partial_take_1_done = False
        state.partial_take_2_done = False
        state.cooldown_until = time.time() + self.config.cooldown_seconds
        self.save_runtime_state()
        logging.info("%s cooldown_until=%s", state.symbol, datetime.fromtimestamp(state.cooldown_until, self.tz).isoformat())

    def notify_global_daily_stop_limit(self) -> None:
        if self.global_limit_notified:
            return
        self.global_limit_notified = True
        self.save_runtime_state()
        self.notifier.send(
            "\n".join(
                [
                    "[GLOBAL DAILY STOP LIMIT EXCEEDED]",
                    f"- Confirmed initial stop losses: {self.global_daily_stop_count}",
                    f"- Allowed before pause: {self.config.global_daily_stop_limit}",
                    "- New entries are paused until the next Asia/Shanghai day.",
                    "- Existing positions remain protected and managed.",
                    "- Qualified signals will be tracked as paper trades when enabled.",
                ]
            )
        )

    def notify_global_daily_net_loss_limit(self) -> None:
        if self.global_net_loss_limit_notified:
            return
        self.global_net_loss_limit_notified = True
        self.save_runtime_state()
        self.notifier.send(
            "\n".join(
                [
                    "[GLOBAL DAILY NET LOSS LIMIT REACHED]",
                    f"- Realized net PnL today: {self.global_daily_net_pnl:+.4f} USDT",
                    f"- Maximum daily net loss: {self.config.global_daily_net_loss_limit:.4f} USDT",
                    "- New real entries are paused until the next Asia/Shanghai day.",
                    "- Existing positions remain protected and managed.",
                    "- Qualified signals will be tracked as paper trades when enabled.",
                ]
            )
        )

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
                    f"- Entry: {format_decimal(state.entry_price)}",
                    f"- Quantity: {format_decimal(state.quantity)}",
                    f"- Margin: {state.initial_margin:.4f} USDT",
                    f"- Notional: {(state.entry_price * state.quantity):.4f} USDT",
                    f"- Initial stop: {format_decimal(state.stop_price)}",
                    f"- Profit-lock trigger: {format_decimal(breakeven_trigger)} (+{self.config.breakeven_roi * 100}% ROI)",
                    f"- Locked ROI after trigger: +{self.config.profit_lock_roi * 100}%",
                    f"- Trailing trigger: {format_decimal(trailing_trigger)} (+{self.config.trailing_activation_roi * 100}% ROI)",
                ]
            )
        )

    def notify_position_closed(
        self, state: PositionState, opened_at_ms: int, initial_margin: Decimal
    ) -> dict[str, Decimal | None]:
        now_ms = int(time.time() * 1000)
        realized = Decimal("0")
        commission = Decimal("0")
        funding = Decimal("0")
        exit_price: Decimal | None = None
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
            try:
                trade_rows = self.client.signed_request(
                    "GET",
                    "/fapi/v1/userTrades",
                    {
                        "symbol": state.symbol,
                        "startTime": max(0, opened_at_ms - 5000),
                        "endTime": now_ms,
                        "limit": 1000,
                    },
                )
                exit_price = weighted_exit_fill_price(trade_rows, state.side)
            except Exception as exc:
                logging.warning("%s exit fill summary query failed: %s", state.symbol, exc)
        net = realized + commission + funding
        roi = (net / initial_margin * Decimal("100")) if complete and initial_margin > 0 else None
        duration = max(0, (now_ms - opened_at_ms) // 1000) if opened_at_ms else 0
        pnl_label = f"{net:+.4f} USDT" if complete else "unavailable"
        roi_label = f"{roi:+.2f}%" if roi is not None else "unavailable"
        self.notifier.send(
            "\n".join(
                [
                    f"[CLOSED] {state.symbol} {(state.side or 'unknown').upper()}",
                    f"- Reason: {state.stop_reason}",
                    f"- Entry: {format_decimal(state.entry_price)}",
                    f"- Exit: {format_decimal(exit_price) if exit_price is not None else 'unavailable'}",
                    f"- Quantity: {format_decimal(state.quantity)}",
                    f"- Realized PnL: {realized:+.4f} USDT" if complete else "- Realized PnL: unavailable",
                    f"- Commission: {commission:+.4f} USDT" if complete else "- Commission: unavailable",
                    f"- Funding: {funding:+.4f} USDT" if complete else "- Funding: unavailable",
                    f"- Net PnL: {pnl_label}",
                    f"- Margin ROI: {roi_label}",
                    f"- Duration: {duration // 3600}h {(duration % 3600) // 60}m {duration % 60}s",
                ]
            )
        )
        return {
            "exit_price": exit_price,
            "realized": realized if complete else None,
            "commission": commission if complete else None,
            "funding": funding if complete else None,
            "net": net if complete else None,
            "roi": roi,
        }

    def pullback_entry_ready(
        self,
        state: PositionState,
        side: str,
        signal_price: Decimal,
        confirm_pct: Decimal | None = None,
        max_pullback_pct: Decimal | None = None,
    ) -> bool:
        if self.config.pullback_entry_pct <= 0:
            return True

        confirm_pct = self.config.pullback_confirm_pct if confirm_pct is None else confirm_pct
        now = time.time()
        if (
            state.pending_signal_side == side
            and state.pending_reversal_confirmed
            and now >= state.pending_signal_until
        ):
            mark_price = self.get_mark_price(state.symbol)
            if self.signal_recross_ready(state, side, mark_price):
                return True
            rejected_price = state.pending_signal_price
            state.rejected_signal_side = side
            state.rejected_signal_price = rejected_price
            logging.info(
                "%s signal=%s invalidated: signal-price recross window expired signal_price=%s",
                state.symbol,
                side,
                rejected_price,
            )
            self.clear_pending_signal(state)
            return False
        if state.pending_signal_side != side or now >= state.pending_signal_until:
            state.pending_signal_side = side
            state.pending_signal_price = signal_price
            state.pending_signal_until = now + self.config.pullback_signal_wait_seconds
            state.pending_confirm_pct = confirm_pct
            state.pending_pullback_reached = False
            state.pending_pullback_extreme = Decimal("0")
            state.pending_reversal_confirmed = False
            state.pending_recross_count = 0
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
        if max_pullback_pct is not None and pullback_limit_exceeded(
            mark_price, side, state.pending_signal_price, max_pullback_pct
        ):
            adverse_move = adverse_pullback_pct(mark_price, side, state.pending_signal_price)
            state.rejected_signal_side = side
            state.rejected_signal_price = signal_price
            logging.info(
                "%s signal=%s invalidated: pullback exceeded ATR limit mark=%s signal_price=%s "
                "adverse_move=%.3f%% limit=%.3f%%",
                state.symbol,
                side,
                mark_price,
                state.pending_signal_price,
                adverse_move * Decimal("100"),
                max_pullback_pct * Decimal("100"),
            )
            self.clear_pending_signal(state)
            return False

        if state.pending_reversal_confirmed:
            return self.signal_recross_ready(state, side, mark_price)

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
                return self.on_pullback_reversal_confirmed(state, side, mark_price)
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
            return self.on_pullback_reversal_confirmed(state, side, mark_price)
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

    def on_pullback_reversal_confirmed(
        self, state: PositionState, side: str, mark_price: Decimal
    ) -> bool:
        if not self.config.require_signal_recross:
            return True
        state.pending_reversal_confirmed = True
        state.pending_recross_count = 0
        now_ms = int(time.time() * 1000)
        state.pending_recross_candle_open_after_ms = ((now_ms // 60000) + 1) * 60000
        state.pending_signal_until = (
            state.pending_recross_candle_open_after_ms / 1000
            + self.config.signal_recross_wait_seconds
        )
        logging.info(
            "%s reversal confirmed side=%s; waiting closed 1m signal-price recross signal_price=%s "
            "first_eligible_candle_open=%s wait_until=%s",
            state.symbol,
            side,
            state.pending_signal_price,
            datetime.fromtimestamp(state.pending_recross_candle_open_after_ms / 1000, self.tz).isoformat(),
            datetime.fromtimestamp(state.pending_signal_until, self.tz).isoformat(),
        )
        return self.signal_recross_ready(state, side, mark_price)

    def signal_recross_ready(
        self, state: PositionState, side: str, mark_price: Decimal
    ) -> bool:
        candles = self.fetch_closed_candles(state.symbol, "1m", 3)
        eligible = [
            candle
            for candle in candles
            if candle.open_time >= state.pending_recross_candle_open_after_ms
            and candle.close_time <= int(state.pending_signal_until * 1000)
        ]
        if not eligible:
            logging.info(
                "%s waiting closed 1m signal-price recross side=%s signal_price=%s first_eligible_open=%s",
                state.symbol,
                side,
                state.pending_signal_price,
                datetime.fromtimestamp(state.pending_recross_candle_open_after_ms / 1000, self.tz).isoformat(),
            )
            return False
        candle = eligible[-1]
        crossed = (
            candle.close >= state.pending_signal_price
            if side == "long"
            else candle.close <= state.pending_signal_price
        )
        logging.info(
            "%s closed 1m signal-price recross side=%s candle_open=%s close=%s signal_price=%s "
            "crossed=%s current_mark=%s",
            state.symbol,
            side,
            datetime.fromtimestamp(candle.open_time / 1000, self.tz).isoformat(),
            candle.close,
            state.pending_signal_price,
            crossed,
            mark_price,
        )
        if not crossed:
            return False
        return (
            mark_price >= state.pending_signal_price
            if side == "long"
            else mark_price <= state.pending_signal_price
        )

    def clear_pending_signal(self, state: PositionState) -> None:
        state.pending_signal_side = None
        state.pending_signal_price = Decimal("0")
        state.pending_signal_until = 0
        state.pending_confirm_pct = Decimal("0")
        state.pending_pullback_reached = False
        state.pending_pullback_extreme = Decimal("0")
        state.pending_reversal_confirmed = False
        state.pending_recross_count = 0
        state.pending_recross_candle_open_after_ms = 0

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

    def refresh_active_universe(self, force: bool = False) -> None:
        if not self.config.dynamic_universe_enabled:
            return
        now = time.time()
        if not force and now < self.next_universe_refresh:
            return
        try:
            exchange_info = self.client.public_request("GET", "/fapi/v1/exchangeInfo")
            tickers = self.client.public_request("GET", "/fapi/v1/ticker/24hr")
            books = self.client.public_request("GET", "/fapi/v1/ticker/bookTicker")
            selected, stats = select_active_universe(
                exchange_info.get("symbols", []),
                tickers,
                books,
                self.config.active_symbol_limit,
                self.config.universe_min_quote_volume,
                self.config.universe_max_spread_pct,
                self.config.universe_min_listing_days,
                self.config.universe_min_24h_range_pct,
                int(now * 1000),
            )
            if not selected:
                raise RuntimeError("Dynamic universe filters returned no eligible symbols")

            previous = set(self.entry_symbols)
            self.entry_symbols = set(selected)
            sticky = set(self.paper_positions) | self.existing_position_symbols
            sticky.update(
                symbol
                for symbol, state in self.states.items()
                if state.quantity != 0 or state.pending_signal_side is not None
            )
            self.managed_symbols = selected + sorted(sticky - self.entry_symbols)
            for symbol in self.managed_symbols:
                self.states.setdefault(symbol, PositionState(symbol=symbol))
            self.rules.update(self._rules_from_exchange_info(exchange_info, set(self.managed_symbols)))
            missing = set(self.managed_symbols) - set(self.rules)
            if missing:
                raise RuntimeError(f"Missing Binance futures symbols: {sorted(missing)}")
            self.set_leverage_for_symbols(self.entry_symbols - self.leverage_symbols)
            self.next_universe_refresh = now + self.config.universe_refresh_seconds
            logging.info(
                "Dynamic universe refreshed candidates=%s eligible=%s active=%s added=%s removed=%s "
                "min_quote_volume=%s max_spread=%.3f%% min_24h_range=%.2f%% symbols=%s",
                stats["candidates"],
                stats["eligible"],
                len(selected),
                sorted(self.entry_symbols - previous),
                sorted(previous - self.entry_symbols),
                self.config.universe_min_quote_volume,
                self.config.universe_max_spread_pct * Decimal("100"),
                self.config.universe_min_24h_range_pct * Decimal("100"),
                selected,
            )
        except Exception:
            self.next_universe_refresh = now + min(60, self.config.universe_refresh_seconds)
            logging.exception(
                "Dynamic universe refresh failed; retaining %s active symbols",
                len(self.entry_symbols),
            )
            if force and not self.entry_symbols:
                raise

    def load_symbol_rules(self) -> dict[str, SymbolRules]:
        data = self.client.public_request("GET", "/fapi/v1/exchangeInfo")
        result = self._rules_from_exchange_info(data, set(self.managed_symbols))
        missing = set(self.managed_symbols) - set(result)
        if missing:
            raise RuntimeError(f"Missing Binance futures symbols: {sorted(missing)}")
        return result

    @staticmethod
    def _rules_from_exchange_info(
        data: dict[str, Any], wanted: set[str]
    ) -> dict[str, SymbolRules]:
        result: dict[str, SymbolRules] = {}
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
        return result

    def set_leverage_for_all(self) -> None:
        self.set_leverage_for_symbols(self.entry_symbols)

    def set_leverage_for_symbols(self, symbols: set[str]) -> None:
        if self.config.paper_trading_only:
            self.leverage_symbols.update(symbols)
            return
        for symbol in sorted(symbols):
            params = {"symbol": symbol, "leverage": self.config.leverage}
            if self.config.dry_run:
                logging.info("[dry-run] set leverage %s", params)
            else:
                try:
                    self.client.signed_request("POST", "/fapi/v1/leverage", params)
                except RuntimeError as exc:
                    self.entry_symbols.discard(symbol)
                    if symbol in self.states:
                        self.clear_pending_signal(self.states[symbol])
                    logging.error(
                        "%s leverage setup failed; new entries disabled for this refresh: %s",
                        symbol,
                        exc,
                    )
                    continue
            self.leverage_symbols.add(symbol)

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
            self.existing_position_symbols.add(symbol)

    def ensure_one_way_mode(self) -> None:
        if self.config.dry_run:
            return
        data = self.client.signed_request("GET", "/fapi/v1/positionSide/dual")
        if str(data.get("dualSidePosition", "")).lower() == "true":
            raise RuntimeError(
                "Binance account is in hedge mode. bn-stra-high-risk-1 requires one-way mode. "
                "Switch USD-M Futures position mode to One-way before running."
            )

    def fetch_closed_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        cache_key = (symbol, interval, limit)
        cached = self.candle_cache.get(cache_key)
        if cached is not None and time.time() < cached[0]:
            return cached[1]
        rows = self.client.public_request(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )
        candles = [
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
        now_ms = int(time.time() * 1000)
        closed = [candle for candle in candles if candle.close_time < now_ms]
        if closed:
            interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
            next_close_at = (closed[-1].close_time + interval_ms + 1) / 1000
            self.candle_cache[cache_key] = (max(time.time() + 1, next_close_at), closed)
        return closed

    def fetch_candles(self, symbol: str) -> list[Candle]:
        return self.fetch_closed_candles(symbol, self.config.interval, self.config.kline_limit)

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
        return self.get_usdt_account_balances()[0]

    def get_usdt_account_balances(self) -> tuple[Decimal, Decimal]:
        if self.config.dry_run:
            return Decimal("10000"), Decimal("10000")
        account = self.client.signed_request("GET", "/fapi/v2/account")
        return Decimal(account["totalMarginBalance"]), Decimal(account["availableBalance"])

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
            if state.best_price <= 0:
                state.best_price = entry
            if state.stop_price <= 0:
                state.stop_price = initial_stop_price(entry, side, self.config.stop_loss_roi, self.config.leverage)
            if state.opened_at_ms <= 0:
                state.opened_at_ms = int(time.time() * 1000)
            if state.initial_margin <= 0:
                state.initial_margin = entry * abs(amt) / Decimal(self.config.leverage)
            if state.initial_quantity <= 0:
                state.initial_quantity = abs(amt)
            self.save_runtime_state()
            self.trade_store.record_open(
                "real", state.symbol, side, self.config.leverage, state.opened_at_ms,
                entry, abs(amt), state.initial_margin, state.stop_price, state.best_price,
            )
        else:
            state.quantity = abs(amt)

    def place_entry_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        reference_price: Decimal,
        position_side: str,
    ) -> dict[str, Any]:
        multiplier = (
            Decimal("1") + self.config.entry_max_slippage_pct
            if side == "BUY"
            else Decimal("1") - self.config.entry_max_slippage_pct
        )
        limit_price = round_stop_price(
            reference_price * multiplier,
            self.rules[symbol].tick_size,
            position_side,
        )
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "IOC",
            "quantity": format_decimal(quantity),
            "price": format_decimal(limit_price),
            "newOrderRespType": "RESULT",
        }
        if self.config.dry_run:
            logging.info("[dry-run] IOC entry order %s", params)
            return {
                "avgPrice": str(reference_price),
                "executedQty": str(quantity),
                "status": "FILLED",
            }
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
        changed = False
        if self.global_daily_stop_day != today:
            self.global_daily_stop_day = today
            self.global_daily_stop_count = 0
            self.global_daily_net_pnl = Decimal("0")
            self.global_limit_logged = False
            self.global_limit_notified = False
            self.global_net_loss_limit_logged = False
            self.global_net_loss_limit_notified = False
            changed = True
        if state.daily_stop_day != today:
            state.daily_stop_day = today
            state.daily_stop_count = 0
            state.daily_limit_logged = False
            changed = True
        if changed:
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
            if data.get("day") == today:
                raw_global_count = data.get("global_daily_stop_count")
                self.global_daily_stop_count = (
                    max(0, int(raw_global_count))
                    if raw_global_count is not None
                    else sum(max(0, int(value)) for value in counts.values())
                )
                self.global_limit_notified = bool(data.get("global_limit_notified", False))
                self.global_net_loss_limit_notified = bool(
                    data.get("global_net_loss_limit_notified", False)
                )
            else:
                self.global_daily_stop_count = 0
                self.global_limit_notified = False
                self.global_net_loss_limit_notified = False
            for symbol in counts:
                if symbol not in self.states:
                    self.states[symbol] = PositionState(symbol=symbol)
                if symbol not in self.managed_symbols:
                    self.managed_symbols.append(symbol)
            for symbol, state in self.states.items():
                state.daily_stop_day = today
                state.daily_stop_count = max(0, int(counts.get(symbol, 0)))
            cooldowns = data.get("cooldown_until", {})
            if not isinstance(cooldowns, dict):
                raise ValueError("cooldown_until must be an object")
            now = time.time()
            for symbol, value in cooldowns.items():
                if symbol not in self.states:
                    self.states[symbol] = PositionState(symbol=symbol)
                restored_until = float(value)
                self.states[symbol].cooldown_until = restored_until if restored_until > now else 0
            active_trades = data.get("active_trades", {})
            if not isinstance(active_trades, dict):
                raise ValueError("active_trades must be an object")
            for symbol, raw_trade in active_trades.items():
                if not isinstance(raw_trade, dict):
                    continue
                if symbol not in self.states:
                    self.states[symbol] = PositionState(symbol=symbol)
                state = self.states[symbol]
                state.opened_at_ms = max(0, int(raw_trade.get("opened_at_ms", 0)))
                state.initial_margin = max(Decimal("0"), Decimal(str(raw_trade.get("initial_margin", "0"))))
                state.initial_quantity = max(Decimal("0"), Decimal(str(raw_trade.get("initial_quantity", "0"))))
                state.partial_take_1_done = bool(raw_trade.get("partial_take_1_done", False))
                state.partial_take_2_done = bool(raw_trade.get("partial_take_2_done", False))
                state.best_price = max(Decimal("0"), Decimal(str(raw_trade.get("best_price", "0"))))
                state.stop_price = max(Decimal("0"), Decimal(str(raw_trade.get("stop_price", "0"))))
                state.stop_order_id = int(raw_trade["stop_order_id"]) if raw_trade.get("stop_order_id") else None
                state.stop_client_id = raw_trade.get("stop_client_id") or None
                state.stop_reason = str(raw_trade.get("stop_reason", "stop_loss"))
                state.breakeven_done = bool(raw_trade.get("breakeven_done", False))
                state.trailing_active = bool(raw_trade.get("trailing_active", False))
            paper_positions = data.get("paper_positions", {})
            if not isinstance(paper_positions, dict):
                raise ValueError("paper_positions must be an object")
            for symbol, raw_position in paper_positions.items():
                if not isinstance(raw_position, dict):
                    continue
                if symbol not in self.states:
                    self.states[symbol] = PositionState(symbol=symbol)
                if symbol not in self.managed_symbols:
                    self.managed_symbols.append(symbol)
                self.paper_positions[symbol] = PaperPosition(
                    symbol=symbol,
                    side=str(raw_position["side"]),
                    entry_price=Decimal(str(raw_position["entry_price"])),
                    quantity=Decimal(str(raw_position["quantity"])),
                    remaining_quantity=Decimal(str(raw_position["remaining_quantity"])),
                    best_price=Decimal(str(raw_position["best_price"])),
                    stop_price=Decimal(str(raw_position["stop_price"])),
                    stop_reason=str(raw_position.get("stop_reason", "stop_loss")),
                    opened_at_ms=int(raw_position["opened_at_ms"]),
                    initial_margin=Decimal(str(raw_position["initial_margin"])),
                    realized_gross_pnl=Decimal(str(raw_position.get("realized_gross_pnl", "0"))),
                    commission=Decimal(str(raw_position.get("commission", "0"))),
                    partial_take_1_done=bool(raw_position.get("partial_take_1_done", False)),
                    partial_take_2_done=bool(raw_position.get("partial_take_2_done", False)),
                    breakeven_done=bool(raw_position.get("breakeven_done", False)),
                    trailing_active=bool(raw_position.get("trailing_active", False)),
                )
            logging.info(
                "Restored daily stop counts for %s: per_symbol=%s global=%s",
                today,
                counts,
                self.global_daily_stop_count,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logging.warning("Could not load runtime state from %s: %s", self.state_path, exc)

    def save_runtime_state(self) -> None:
        today = datetime.now(self.tz).strftime("%Y-%m-%d")
        data = {
            "day": today,
            "global_daily_stop_count": self.global_daily_stop_count,
            "global_daily_net_pnl": format_decimal(self.global_daily_net_pnl),
            "global_limit_notified": self.global_limit_notified,
            "global_net_loss_limit_notified": self.global_net_loss_limit_notified,
            "daily_stop_counts": {
                symbol: state.daily_stop_count
                for symbol, state in self.states.items()
                if state.daily_stop_day == today and state.daily_stop_count > 0
            },
            "cooldown_until": {
                symbol: state.cooldown_until
                for symbol, state in self.states.items()
                if state.cooldown_until > time.time()
            },
            "active_trades": {
                symbol: {
                    "opened_at_ms": state.opened_at_ms,
                    "initial_margin": format_decimal(state.initial_margin),
                    "initial_quantity": format_decimal(state.initial_quantity),
                    "partial_take_1_done": state.partial_take_1_done,
                    "partial_take_2_done": state.partial_take_2_done,
                    "best_price": format_decimal(state.best_price),
                    "stop_price": format_decimal(state.stop_price),
                    "stop_order_id": state.stop_order_id,
                    "stop_client_id": state.stop_client_id,
                    "stop_reason": state.stop_reason,
                    "breakeven_done": state.breakeven_done,
                    "trailing_active": state.trailing_active,
                }
                for symbol, state in self.states.items()
                if state.opened_at_ms > 0
            },
            "paper_positions": {
                symbol: {
                    "side": position.side,
                    "entry_price": format_decimal(position.entry_price),
                    "quantity": format_decimal(position.quantity),
                    "remaining_quantity": format_decimal(position.remaining_quantity),
                    "best_price": format_decimal(position.best_price),
                    "stop_price": format_decimal(position.stop_price),
                    "stop_reason": position.stop_reason,
                    "opened_at_ms": position.opened_at_ms,
                    "initial_margin": format_decimal(position.initial_margin),
                    "realized_gross_pnl": format_decimal(position.realized_gross_pnl),
                    "commission": format_decimal(position.commission),
                    "partial_take_1_done": position.partial_take_1_done,
                    "partial_take_2_done": position.partial_take_2_done,
                    "breakeven_done": position.breakeven_done,
                    "trailing_active": position.trailing_active,
                }
                for symbol, position in self.paper_positions.items()
            },
        }
        temp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
        try:
            temp_path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
            temp_path.replace(self.state_path)
        except OSError as exc:
            logging.error("Could not save runtime state to %s: %s", self.state_path, exc)


def select_active_universe(
    exchange_symbols: list[dict[str, Any]],
    tickers: list[dict[str, Any]],
    books: list[dict[str, Any]],
    limit: int,
    min_quote_volume: Decimal,
    max_spread_pct: Decimal,
    min_listing_days: int,
    min_24h_range_pct: Decimal,
    now_ms: int,
) -> tuple[list[str], dict[str, int]]:
    ticker_by_symbol = {str(item.get("symbol", "")): item for item in tickers}
    book_by_symbol = {str(item.get("symbol", "")): item for item in books}
    min_age_ms = min_listing_days * 86_400_000
    candidates = [
        item
        for item in exchange_symbols
        if item.get("status") == "TRADING"
        and item.get("contractType") in {"PERPETUAL", "TRADIFI_PERPETUAL"}
        and item.get("quoteAsset") == "USDT"
    ]
    ranked: list[tuple[Decimal, Decimal, str]] = []
    for item in candidates:
        symbol = str(item["symbol"])
        onboard_date = int(item.get("onboardDate", 0) or 0)
        if onboard_date <= 0 or now_ms - onboard_date < min_age_ms:
            continue
        ticker = ticker_by_symbol.get(symbol)
        book = book_by_symbol.get(symbol)
        if not ticker or not book:
            continue
        try:
            quote_volume = Decimal(str(ticker["quoteVolume"]))
            high = Decimal(str(ticker["highPrice"]))
            low = Decimal(str(ticker["lowPrice"]))
            bid = Decimal(str(book["bidPrice"]))
            ask = Decimal(str(book["askPrice"]))
        except (KeyError, ArithmeticError, ValueError):
            continue
        if quote_volume < min_quote_volume or low <= 0 or bid <= 0 or ask <= bid:
            continue
        mid = (bid + ask) / Decimal("2")
        spread_pct = (ask - bid) / mid
        range_pct = (high - low) / low
        if spread_pct > max_spread_pct or range_pct < min_24h_range_pct:
            continue
        # Volume rewards executable markets; range rewards symbols that can reach ROI targets.
        ranked.append((quote_volume * range_pct, quote_volume, symbol))
    ranked.sort(reverse=True)
    selected = [symbol for _, _, symbol in ranked[:limit]]
    return selected, {"candidates": len(candidates), "eligible": len(ranked)}


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
    adx_series = adx_values(candles, adx_period)
    adx = adx_series[-1]
    previous_adx = adx_series[-2] if len(adx_series) >= 2 else None
    if (
        fast is None
        or slow is None
        or adx is None
        or previous_adx is None
        or adx <= adx_min
        or adx < previous_adx
        or fast == slow
    ):
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
    return stop_price_from_distance(entry, side, move)


def stop_price_from_distance(entry: Decimal, side: str, distance_pct: Decimal) -> Decimal:
    return entry * (Decimal("1") - distance_pct) if side == "long" else entry * (Decimal("1") + distance_pct)


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


def adverse_pullback_pct(price: Decimal, side: str, signal_price: Decimal) -> Decimal:
    if signal_price <= 0:
        return Decimal("0")
    move = (signal_price - price) / signal_price if side == "long" else (price - signal_price) / signal_price
    return max(Decimal("0"), move)


def pullback_limit_exceeded(price: Decimal, side: str, signal_price: Decimal, limit_pct: Decimal) -> bool:
    return adverse_pullback_pct(price, side, signal_price) > limit_pct


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


def trailing_callback_for_roi(
    best_roi: Decimal,
    activation_roi: Decimal,
    activation_callback: Decimal,
    tier_2_roi: Decimal,
    tier_2_callback: Decimal,
    tier_3_roi: Decimal,
    tier_3_callback: Decimal,
) -> Decimal | None:
    if best_roi >= tier_3_roi:
        return tier_3_callback
    if best_roi >= tier_2_roi:
        return tier_2_callback
    if best_roi >= activation_roi:
        return activation_callback
    return None


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


def capped_atr_stop_distance(base_stop_pct: Decimal, atr_pct: Decimal, atr_multiplier: Decimal) -> Decimal:
    return min(base_stop_pct, atr_pct * atr_multiplier)


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


def adverse_entry_signal_distance(entry_price: Decimal, signal_price: Decimal, side: str) -> Decimal:
    if signal_price <= 0:
        return Decimal("999")
    adverse_distance = (
        entry_price - signal_price if side == "long" else signal_price - entry_price
    )
    return max(Decimal("0"), adverse_distance / signal_price)


def entry_signal_price_allows(
    entry_price: Decimal,
    signal_price: Decimal,
    side: str,
    max_distance_pct: Decimal,
) -> bool:
    still_crossed = entry_price >= signal_price if side == "long" else entry_price <= signal_price
    return still_crossed and adverse_entry_signal_distance(
        entry_price, signal_price, side
    ) <= max_distance_pct


def contract_position_allows(
    side: str,
    global_ratio: Decimal,
    top_ratio: Decimal,
    crowded_short_global_max: Decimal,
    crowded_short_top_min: Decimal,
    crowded_long_global_min: Decimal,
    crowded_long_top_max: Decimal,
    top_long_veto_max: Decimal = Decimal("0"),
    top_short_veto_min: Decimal = Decimal("999"),
) -> bool:
    if side == "long" and top_ratio <= top_long_veto_max:
        return False
    if side == "short" and top_ratio >= top_short_veto_min:
        return False
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


def weighted_exit_fill_price(
    trades: list[dict[str, Any]], position_side: str | None
) -> Decimal | None:
    closing_side = "SELL" if position_side == "long" else "BUY" if position_side == "short" else None
    if closing_side is None:
        return None
    total_quantity = Decimal("0")
    total_notional = Decimal("0")
    for trade in trades:
        if str(trade.get("side", "")).upper() != closing_side:
            continue
        quantity = Decimal(str(trade.get("qty", "0")))
        price = Decimal(str(trade.get("price", "0")))
        if quantity <= 0 or price <= 0:
            continue
        total_quantity += quantity
        total_notional += quantity * price
    return total_notional / total_quantity if total_quantity > 0 else None


def gross_pnl(entry_price: Decimal, exit_price: Decimal, quantity: Decimal, side: str) -> Decimal:
    price_change = exit_price - entry_price if side == "long" else entry_price - exit_price
    return price_change * quantity


def format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")


def client_order_id(symbol: str, label: str) -> str:
    base_symbol = symbol.lower().replace("usdt", "")
    short_symbol = "".join(ch for ch in base_symbol if ch.isascii() and ch.isalnum())
    if not short_symbol:
        short_symbol = hashlib.sha1(symbol.encode("utf-8")).hexdigest()[:8]
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
