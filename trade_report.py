#!/usr/bin/env python3
"""Read persisted bn-stra-high-risk-1 trades without calling Binance."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".bn-stra-high-risk-1-trades.sqlite3")
    parser.add_argument("--date", help="Opening date in Asia/Shanghai, for example 2026-08-14")
    parser.add_argument("--status", choices=("open", "closed"))
    parser.add_argument("--symbol")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    clauses: list[str] = []
    values: list[object] = []
    if args.date:
        start = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
        clauses.append("opened_at_ms BETWEEN ? AND ?")
        values.extend((int(start.timestamp() * 1000), int(end.timestamp() * 1000)))
    if args.status:
        clauses.append("status = ?")
        values.append(args.status)
    if args.symbol:
        clauses.append("symbol = ?")
        values.append(args.symbol.upper())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(max(1, args.limit))
    rows = connection.execute(
        f"""
        SELECT * FROM trades
        {where}
        ORDER BY opened_at_ms DESC
        LIMIT ?
        """,
        values,
    ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["opened_at"] = format_time(item["opened_at_ms"])
        item["closed_at"] = format_time(item["closed_at_ms"])
    if args.as_json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return

    print("opened_at           mode  symbol          side   status  entry          exit           net_pnl     roi       reason")
    print("-" * 116)
    for item in items:
        print(
            f"{item['opened_at']:<19} {item['mode']:<5} {item['symbol']:<15} "
            f"{item['side']:<6} {item['status']:<7} {item['entry_price']:<14} "
            f"{(item['exit_price'] or '-'):<14} {(item['net_pnl'] or '-'):<11} "
            f"{(item['margin_roi'] or '-'):<9} {item['close_reason'] or '-'}"
        )
    closed = [item for item in items if item["status"] == "closed" and item["net_pnl"] is not None]
    net = sum((Decimal(item["net_pnl"]) for item in closed), Decimal("0"))
    wins = sum(1 for item in closed if Decimal(item["net_pnl"]) > 0)
    print(f"\nrows={len(items)} closed={len(closed)} wins={wins} net_pnl={net:+.4f} USDT")


def format_time(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return "-"
    return datetime.fromtimestamp(timestamp_ms / 1000, TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    main()
