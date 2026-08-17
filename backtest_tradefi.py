#!/usr/bin/env python3
"""Conservative historical replay for the strategy's current TradFi universe."""

from __future__ import annotations

import argparse
import http.client
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from bn_stra_high_risk_1 import (
    Candle,
    adverse_entry_signal_distance,
    atr_percent,
    capped_atr_stop_distance,
    contract_position_allows,
    dynamic_confirm_pct,
    ema_values,
    entry_near_ema,
    entry_signal_price_allows,
    margin_roi,
    strategy_signal,
    trailing_callback_for_roi,
)

D = Decimal
API = "https://fapi.binance.com"
INTERVAL_MS = 60_000


@dataclass
class Pending:
    side: str
    signal_price: D
    atr_pct: D
    started_ms: int
    expires_ms: int
    confirm_pct: D
    pullback_extreme: Optional[D] = None
    confirmed_ms: Optional[int] = None


@dataclass
class Position:
    symbol: str
    side: str
    opened_ms: int
    entry: D
    quantity: D
    remaining: D
    stop: D
    best: D
    margin: D
    commission: D
    realized: D = D("0")
    partial_1: bool = False
    partial_2: bool = False
    reason: str = "stop_loss"


@dataclass
class Trade:
    symbol: str
    side: str
    opened_ms: int
    closed_ms: int
    entry: D
    exit: D
    net: D
    roi_pct: D
    reason: str


def request(path: str, params: dict[str, object]) -> object:
    query = urllib.parse.urlencode(params)
    url = f"{API}{path}?{query}"
    last_error: Optional[Exception] = None
    for attempt in range(5):
        req = urllib.request.Request(url, headers={"User-Agent": "bn-stra-backtest/1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except (OSError, json.JSONDecodeError, http.client.IncompleteRead) as exc:
            last_error = exc
            time.sleep(0.5 * (2**attempt))
    assert last_error is not None
    raise last_error


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list[object]]:
    rows: list[list[object]] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = request(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1m", "startTime": cursor, "endTime": end_ms - 1, "limit": 1500},
        )
        if not batch:
            break
        rows.extend(batch)  # type: ignore[arg-type]
        cursor = int(batch[-1][0]) + INTERVAL_MS  # type: ignore[index]
        time.sleep(0.03)
    return rows


def fetch_ratios(symbol: str, path: str, start_ms: int, end_ms: int) -> dict[int, D]:
    values: dict[int, D] = {}
    cursor = start_ms
    while cursor < end_ms:
        window_end = min(end_ms - 1, cursor + 500 * 300_000 - 1)
        batch = request(
            path,
            {"symbol": symbol, "period": "5m", "startTime": cursor, "endTime": window_end, "limit": 500},
        )
        if not batch:
            cursor = window_end + 1
            continue
        for row in batch:  # type: ignore[union-attr]
            values[int(row["timestamp"])] = D(str(row["longShortRatio"]))
        next_cursor = int(batch[-1]["timestamp"]) + 300_000  # type: ignore[index]
        if next_cursor <= cursor:
            break
        cursor = max(next_cursor, window_end + 1)
        time.sleep(0.03)
    return values


def minute_candle(row: list[object]) -> Candle:
    return Candle(
        open_time=int(row[0]),
        open=D(str(row[1])),
        high=D(str(row[2])),
        low=D(str(row[3])),
        close=D(str(row[4])),
        close_time=int(row[6]),
    )


def aggregate_5m(rows: list[list[object]]) -> tuple[list[Candle], dict[int, int]]:
    result: list[Candle] = []
    close_to_index: dict[int, int] = {}
    groups: dict[int, list[Candle]] = defaultdict(list)
    for row in rows:
        candle = minute_candle(row)
        groups[(candle.open_time // 300_000) * 300_000].append(candle)
    for open_time in sorted(groups):
        group = groups[open_time]
        if len(group) != 5:
            continue
        candle = Candle(
            open_time=open_time,
            open=group[0].open,
            high=max(item.high for item in group),
            low=min(item.low for item in group),
            close=group[-1].close,
            close_time=group[-1].close_time,
        )
        result.append(candle)
        close_to_index[candle.close_time] = len(result) - 1
    return result, close_to_index


def historical_eligibility(
    rows: list[list[object]], onboard_ms: int, cfg: dict[str, object]
) -> dict[int, bool]:
    eligible: dict[int, bool] = {}
    min_age_ms = int(cfg["universe_min_listing_days"]) * 86_400_000
    min_volume = D(str(cfg["universe_min_quote_volume"]))
    min_range = D(str(cfg["universe_min_24h_range_pct"]))
    for index, current_row in enumerate(rows):
        if (int(current_row[0]) // 60_000) % 5 != 4:
            continue
        close_ms = int(current_row[6])
        if close_ms - onboard_ms < min_age_ms:
            eligible[close_ms] = False
            continue
        window_start = max(0, index - 1439)
        window = rows[window_start : index + 1]
        if len(window) < 1440:
            eligible[close_ms] = False
            continue
        quote_volume = sum((D(str(row[7])) for row in window), D("0"))
        high = max(D(str(row[2])) for row in window)
        low = min(D(str(row[3])) for row in window)
        range_pct = (high - low) / low if low > 0 else D("0")
        eligible[close_ms] = quote_volume >= min_volume and range_pct >= min_range
    return eligible


def nearest_ratio(values: dict[int, D], timestamp_ms: int) -> Optional[D]:
    bucket = (timestamp_ms // 300_000) * 300_000
    for offset in (0, -300_000, -600_000):
        if bucket + offset in values:
            return values[bucket + offset]
    return None


def gross(entry: D, exit_price: D, quantity: D, side: str) -> D:
    return (exit_price - entry) * quantity if side == "long" else (entry - exit_price) * quantity


def close_trade(position: Position, price: D, timestamp_ms: int, fee_rate: D, reason: str) -> Trade:
    position.realized += gross(position.entry, price, position.remaining, position.side)
    position.commission += price * position.remaining * fee_rate
    net = position.realized - position.commission
    return Trade(
        symbol=position.symbol,
        side=position.side,
        opened_ms=position.opened_ms,
        closed_ms=timestamp_ms,
        entry=position.entry,
        exit=price,
        net=net,
        roi_pct=net / position.margin * D("100"),
        reason=reason,
    )


def partial(position: Position, price: D, fraction: D, fee_rate: D) -> None:
    quantity = min(position.quantity * fraction, position.remaining)
    position.realized += gross(position.entry, price, quantity, position.side)
    position.commission += price * quantity * fee_rate
    position.remaining -= quantity


def position_stop_hit(position: Position, candle: Candle) -> bool:
    return candle.low <= position.stop if position.side == "long" else candle.high >= position.stop


def favorable_price(position: Position, candle: Candle) -> D:
    return candle.high if position.side == "long" else candle.low


def improve_stop(current: D, candidate: D, side: str) -> D:
    return max(current, candidate) if side == "long" else min(current, candidate)


def trigger_price(entry: D, side: str, roi: D, leverage: int) -> D:
    move = roi / D(leverage)
    return entry * (D("1") + move) if side == "long" else entry * (D("1") - move)


def manage_position(position: Position, candle: Candle, cfg: dict[str, object]) -> Optional[Trade]:
    fee_rate = D(str(cfg["fee_rate"]))
    if position_stop_hit(position, candle):
        return close_trade(position, position.stop, candle.close_time, fee_rate, position.reason)

    favorable = favorable_price(position, candle)
    position.best = max(position.best, favorable) if position.side == "long" else min(position.best, favorable)
    roi = margin_roi(favorable, position.entry, position.side, int(cfg["leverage"]))

    if roi >= D(str(cfg["partial_take_1_roi"])) and not position.partial_1:
        price = trigger_price(position.entry, position.side, D(str(cfg["partial_take_1_roi"])), int(cfg["leverage"]))
        partial(position, price, D(str(cfg["partial_take_1_fraction"])), fee_rate)
        position.partial_1 = True
    if roi >= D(str(cfg["partial_take_2_roi"])) and not position.partial_2:
        price = trigger_price(position.entry, position.side, D(str(cfg["partial_take_2_roi"])), int(cfg["leverage"]))
        partial(position, price, D(str(cfg["partial_take_2_fraction"])), fee_rate)
        position.partial_2 = True

    new_stop = position.stop
    if roi >= D(str(cfg["breakeven_roi"])):
        new_stop = improve_stop(
            new_stop,
            trigger_price(position.entry, position.side, D(str(cfg["profit_lock_roi"])), int(cfg["leverage"])),
            position.side,
        )
        position.reason = "break_even"
    callback = trailing_callback_for_roi(
        margin_roi(position.best, position.entry, position.side, int(cfg["leverage"])),
        D(str(cfg["trailing_activation_roi"])), D(str(cfg["trailing_callback"])),
        D(str(cfg["trailing_tier_2_roi"])), D(str(cfg["trailing_tier_2_callback"])),
        D(str(cfg["trailing_tier_3_roi"])), D(str(cfg["trailing_tier_3_callback"])),
    )
    if callback is not None:
        trail = position.best * (D("1") - callback) if position.side == "long" else position.best * (D("1") + callback)
        new_stop = improve_stop(new_stop, trail, position.side)
        position.reason = "trailing_stop"
    improvement = (new_stop - position.stop) / position.stop if position.side == "long" else (position.stop - new_stop) / position.stop
    if improvement >= D(str(cfg["stop_update_min_pct"])):
        position.stop = new_stop
    return None


def signal_for(candles: list[Candle], index: int, cfg: dict[str, object]) -> tuple[str, Optional[D]]:
    history = candles[: index + 1]
    side = strategy_signal(
        history, int(cfg["ema_fast"]), int(cfg["ema_slow"]), int(cfg["adx_period"]),
        D(str(cfg["adx_min"])), int(cfg["atr_period"]), D(str(cfg["atr_min_pct"])), D(str(cfg["atr_max_pct"])),
    )
    atr = atr_percent(history, int(cfg["atr_period"]))
    if side == "none" or atr is None or not entry_near_ema(history, int(cfg["ema_fast"]), atr, D(str(cfg["max_ema_atr_distance"]))):
        return "none", atr
    return side, atr


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.high-risk.json")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--margin", type=D, default=D("100"))
    parser.add_argument("--symbols", nargs="+", default=["SNDKUSDT", "SNXXUSDT", "ZHIPUUSDT", "ANTHROPICUSDT"])
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    cfg["margin_per_trade"] = str(args.margin)

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    warmup = start - timedelta(hours=30)
    start_ms, end_ms, warmup_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000), int(warmup.timestamp() * 1000)

    minute_by_symbol: dict[str, list[Candle]] = {}
    five_by_symbol: dict[str, list[Candle]] = {}
    five_index: dict[str, dict[int, int]] = {}
    ratios: dict[str, tuple[dict[int, D], dict[int, D]]] = {}
    exchange_info = request("/fapi/v1/exchangeInfo", {})
    onboard_by_symbol = {
        str(item["symbol"]): int(item.get("onboardDate", 0))
        for item in exchange_info["symbols"]  # type: ignore[index]
    }
    eligibility: dict[str, dict[int, bool]] = {}
    diagnostics: dict[str, dict[str, int]] = {symbol: defaultdict(int) for symbol in args.symbols}
    for symbol in args.symbols:
        print(f"Downloading {symbol}...", flush=True)
        rows = fetch_klines(symbol, warmup_ms, end_ms)
        minute_by_symbol[symbol] = [minute_candle(row) for row in rows if int(row[0]) >= start_ms]
        five_by_symbol[symbol], five_index[symbol] = aggregate_5m(rows)
        eligibility[symbol] = historical_eligibility(rows, onboard_by_symbol[symbol], cfg)
        ratios[symbol] = (
            fetch_ratios(symbol, "/futures/data/globalLongShortAccountRatio", start_ms, end_ms),
            fetch_ratios(symbol, "/futures/data/topLongShortPositionRatio", start_ms, end_ms),
        )
        for index, candle in enumerate(five_by_symbol[symbol]):
            if candle.close_time < start_ms:
                continue
            diagnostics[symbol]["five_minute_bars"] += 1
            if not eligibility[symbol].get(candle.close_time, False):
                diagnostics[symbol]["universe_blocked"] += 1
                continue
            diagnostics[symbol]["universe_eligible"] += 1
            history = five_by_symbol[symbol][: index + 1]
            raw_signal = strategy_signal(
                history, int(cfg["ema_fast"]), int(cfg["ema_slow"]), int(cfg["adx_period"]),
                D(str(cfg["adx_min"])), int(cfg["atr_period"]), D(str(cfg["atr_min_pct"])), D(str(cfg["atr_max_pct"])),
            )
            if raw_signal == "none":
                diagnostics[symbol]["no_indicator_signal"] += 1
                continue
            diagnostics[symbol]["indicator_signal"] += 1
            atr = atr_percent(history, int(cfg["atr_period"]))
            if not entry_near_ema(history, int(cfg["ema_fast"]), atr, D(str(cfg["max_ema_atr_distance"]))):
                diagnostics[symbol]["ema_distance_blocked"] += 1
            else:
                diagnostics[symbol]["signal_near_ema"] += 1

    events = sorted(
        (candle.close_time, symbol, candle)
        for symbol, candles in minute_by_symbol.items()
        for candle in candles
    )
    pending: dict[str, Pending] = {}
    positions: dict[str, Position] = {}
    cooldown_until: dict[str, int] = defaultdict(int)
    symbol_stops: dict[tuple[str, str], int] = defaultdict(int)
    global_stops: dict[str, int] = defaultdict(int)
    trades: list[Trade] = []

    for timestamp_ms, symbol, candle in events:
        day = datetime.fromtimestamp(timestamp_ms / 1000, timezone(timedelta(hours=8))).date().isoformat()
        if symbol in positions:
            trade = manage_position(positions[symbol], candle, cfg)
            if trade:
                trades.append(trade)
                if trade.reason == "stop_loss":
                    symbol_stops[(day, symbol)] += 1
                    global_stops[day] += 1
                del positions[symbol]
                cooldown_until[symbol] = timestamp_ms + int(cfg["cooldown_seconds"]) * 1000
            continue
        if timestamp_ms < cooldown_until[symbol] or len(positions) >= int(cfg["max_concurrent_positions"]):
            pending.pop(symbol, None)
            continue
        if symbol_stops[(day, symbol)] >= int(cfg["daily_stop_limit"]) or global_stops[day] > int(cfg["global_daily_stop_limit"]):
            pending.pop(symbol, None)
            continue

        idx = five_index[symbol].get(timestamp_ms)
        is_eligible = eligibility[symbol].get(timestamp_ms, False)
        if symbol not in pending and not is_eligible:
            continue
        if idx is not None and is_eligible:
            side, atr = signal_for(five_by_symbol[symbol], idx, cfg)
            if side == "none" or atr is None:
                pending.pop(symbol, None)
            else:
                global_ratio = nearest_ratio(ratios[symbol][0], timestamp_ms)
                top_ratio = nearest_ratio(ratios[symbol][1], timestamp_ms)
                allowed = global_ratio is not None and top_ratio is not None and contract_position_allows(
                    side, global_ratio, top_ratio,
                    D(str(cfg["crowded_short_global_max"])), D(str(cfg["crowded_short_top_min"])),
                    D(str(cfg["crowded_long_global_min"])), D(str(cfg["crowded_long_top_max"])),
                    D(str(cfg["top_long_veto_max"])), D(str(cfg["top_short_veto_min"])),
                )
                current = pending.get(symbol)
                if not allowed:
                    diagnostics[symbol]["positioning_blocked"] += 1
                    pending.pop(symbol, None)
                elif current is None or current.side != side or timestamp_ms >= current.expires_ms:
                    diagnostics[symbol]["pending_started"] += 1
                    pending[symbol] = Pending(
                        side=side, signal_price=five_by_symbol[symbol][idx].close, atr_pct=atr,
                        started_ms=timestamp_ms, expires_ms=timestamp_ms + int(cfg["pullback_signal_wait_seconds"]) * 1000,
                        confirm_pct=dynamic_confirm_pct(D(str(cfg["pullback_confirm_pct"])), atr, D(str(cfg["atr_confirm_factor"]))),
                    )

        item = pending.get(symbol)
        if item is None:
            continue
        if timestamp_ms > item.expires_ms and item.confirmed_ms is None:
            pending.pop(symbol, None)
            continue
        adverse = (item.signal_price - candle.low) / item.signal_price if item.side == "long" else (candle.high - item.signal_price) / item.signal_price
        if adverse > item.atr_pct * D(str(cfg["max_pullback_atr_distance"])):
            diagnostics[symbol]["pullback_too_deep"] += 1
            pending.pop(symbol, None)
            continue
        target = item.signal_price * (D("1") - D(str(cfg["pullback_entry_pct"]))) if item.side == "long" else item.signal_price * (D("1") + D(str(cfg["pullback_entry_pct"])))
        if item.pullback_extreme is None:
            reached = candle.low <= target if item.side == "long" else candle.high >= target
            if reached:
                item.pullback_extreme = candle.low if item.side == "long" else candle.high
                diagnostics[symbol]["pullback_reached"] += 1
            continue
        if item.confirmed_ms is None:
            item.pullback_extreme = min(item.pullback_extreme, candle.low) if item.side == "long" else max(item.pullback_extreme, candle.high)
            confirm = item.pullback_extreme * (D("1") + item.confirm_pct) if item.side == "long" else item.pullback_extreme * (D("1") - item.confirm_pct)
            reversed_now = candle.high >= confirm if item.side == "long" else candle.low <= confirm
            if reversed_now:
                item.confirmed_ms = timestamp_ms
                item.expires_ms = timestamp_ms + int(cfg["signal_recross_wait_seconds"]) * 1000
                diagnostics[symbol]["reversal_confirmed"] += 1
            continue
        if timestamp_ms > item.expires_ms:
            pending.pop(symbol, None)
            continue
        crossed = candle.close >= item.signal_price if item.side == "long" else candle.close <= item.signal_price
        if not crossed:
            diagnostics[symbol]["recross_wait_bar"] += 1
            continue
        limit = min(D(str(cfg["entry_signal_max_distance_pct"])), item.atr_pct * D(str(cfg["entry_signal_max_atr_factor"])))
        if not entry_signal_price_allows(candle.close, item.signal_price, item.side, limit):
            diagnostics[symbol]["entry_distance_blocked"] += 1
            pending.pop(symbol, None)
            continue
        entry = candle.close
        quantity = args.margin * D(int(cfg["leverage"])) / entry
        stop_distance = capped_atr_stop_distance(D(str(cfg["stop_loss_roi"])) / D(int(cfg["leverage"])), item.atr_pct, D(str(cfg["atr_stop_multiplier"])))
        stop = entry * (D("1") - stop_distance) if item.side == "long" else entry * (D("1") + stop_distance)
        positions[symbol] = Position(
            symbol=symbol, side=item.side, opened_ms=timestamp_ms, entry=entry, quantity=quantity,
            remaining=quantity, stop=stop, best=entry, margin=args.margin,
            commission=entry * quantity * D(str(cfg["fee_rate"])),
        )
        diagnostics[symbol]["opened"] += 1
        pending.pop(symbol, None)

    for symbol, position in list(positions.items()):
        last = minute_by_symbol[symbol][-1]
        trades.append(close_trade(position, last.close, last.close_time, D(str(cfg["fee_rate"])), "end_of_test"))

    print(f"\nPeriod UTC: {start.isoformat()} -> {end.isoformat()}")
    print(f"Symbols: {', '.join(args.symbols)} | margin={args.margin} | leverage={cfg['leverage']}x")
    print("\nEntry diagnostics")
    for symbol in args.symbols:
        item = diagnostics[symbol]
        ratio_rows = min(len(ratios[symbol][0]), len(ratios[symbol][1]))
        print(
            f"{symbol}: bars={item['five_minute_bars']} indicator={item['indicator_signal']} "
            f"universe_eligible={item['universe_eligible']} universe_block={item['universe_blocked']} "
            f"ema_block={item['ema_distance_blocked']} near_ema={item['signal_near_ema']} "
            f"ratio_rows={ratio_rows} positioning_block={item['positioning_blocked']} "
            f"pending={item['pending_started']} pullback={item['pullback_reached']} "
            f"reversal={item['reversal_confirmed']} recross_wait={item['recross_wait_bar']} "
            f"entry_distance_block={item['entry_distance_blocked']} opened={item['opened']}"
        )
    print("opened_at(CST)       symbol         side   net_pnl   roi       reason")
    print("-" * 82)
    for trade in trades:
        opened = datetime.fromtimestamp(trade.opened_ms / 1000, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
        print(f"{opened:<20}{trade.symbol:<15}{trade.side:<7}{trade.net:>9.4f}  {trade.roi_pct:>7.2f}%  {trade.reason}")
    total = sum((trade.net for trade in trades), D("0"))
    wins = sum(1 for trade in trades if trade.net > 0)
    gross_win = sum((trade.net for trade in trades if trade.net > 0), D("0"))
    gross_loss = -sum((trade.net for trade in trades if trade.net < 0), D("0"))
    print("\nSummary")
    print(f"trades={len(trades)} wins={wins} losses={len(trades)-wins} win_rate={(D(wins)/D(len(trades))*100 if trades else D(0)):.2f}%")
    print(f"net_pnl={total:.4f} USDT return_on_1000={(total/D(1000)*100):.2f}% profit_factor={(gross_win/gross_loss if gross_loss else D(0)):.3f}")


if __name__ == "__main__":
    main()
