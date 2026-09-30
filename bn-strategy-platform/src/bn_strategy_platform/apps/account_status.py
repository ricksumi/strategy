"""Read-only Binance account status command."""

from __future__ import annotations

import argparse
import os

from .run_strategy import load_env
from ..exchanges.binance import BinanceUsdM


def main() -> None:
    parser = argparse.ArgumentParser(description="Show Binance Futures balance and open positions")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    load_env(args.env_file)
    exchange = BinanceUsdM(os.environ.get("BINANCE_API_KEY", ""),
                           os.environ.get("BINANCE_API_SECRET", ""),
                           os.environ.get("BINANCE_BASE_URL", "https://fapi.binance.com"))
    account = exchange.account()
    print(f"equity_usdt={account.equity_usdt}")
    print(f"available_usdt={account.available_usdt}")
    symbols = exchange.open_position_symbols()
    if not symbols:
        print("open_positions=none")
        return
    for symbol in symbols:
        amount = exchange.position_amount(symbol)
        side = "long" if amount > 0 else "short"
        print(f"position symbol={symbol} side={side} quantity={abs(amount)}")


if __name__ == "__main__":
    main()
