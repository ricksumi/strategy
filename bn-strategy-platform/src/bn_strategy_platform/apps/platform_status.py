"""Read-only platform position and protection status command."""

from __future__ import annotations

import argparse
from decimal import Decimal

from .run_strategy import load_env
from ..core.config import RuntimeConfig
from ..core.models import RunMode
from ..persistence.mysql import MySqlTradeStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Show persisted platform positions and stop protection")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    load_env(args.env_file)
    config = RuntimeConfig.from_file(args.config)
    store = MySqlTradeStore.from_environment()
    count = 0
    live_risk = Decimal("0")
    long_risk = Decimal("0")
    short_risk = Decimal("0")
    try:
        for item in config.strategies:
            if not item.enabled:
                continue
            mode = config.strategy_mode(item.name)
            for position in store.load_open_positions(item.name, mode.value):
                count += 1
                protected = (("yes" if position.stop_order_id else "NO")
                             if mode is RunMode.LIVE else "not-required")
                stop_distance = (position.entry_price - position.stop_price
                                 if position.side.value == "long"
                                 else position.stop_price - position.entry_price)
                stop_risk = max(Decimal("0"), stop_distance) * position.remaining_quantity
                if mode is RunMode.LIVE:
                    live_risk += stop_risk
                    if position.side.value == "long":
                        long_risk += stop_risk
                    else:
                        short_risk += stop_risk
                print(
                    f"strategy={position.strategy} version={position.strategy_version} mode={mode.value} "
                    f"symbol={position.symbol} side={position.side.value} "
                    f"entry={position.entry_price} quantity={position.remaining_quantity} "
                    f"stop={position.stop_price} stop_order_id={position.stop_order_id or '-'} "
                    f"stop_risk_usdt={stop_risk:.4f} protected={protected}"
                )
    finally:
        store.close()
    if count == 0:
        print("platform_open_positions=none")
    print(
        f"live_open_risk_usdt={live_risk:.4f}/{config.max_open_risk_usdt:.4f} "
        f"long_risk_usdt={long_risk:.4f}/{config.max_directional_open_risk_usdt:.4f} "
        f"short_risk_usdt={short_risk:.4f}/{config.max_directional_open_risk_usdt:.4f}"
    )


if __name__ == "__main__":
    main()
