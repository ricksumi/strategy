"""Run one unified strategy-platform process."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
from pathlib import Path

from ..core.config import RuntimeConfig
from ..core.engine import TradingEngine
from ..core.models import RunMode
from ..exchanges.binance import BinanceUsdM
from ..market_data.cache import CachedMarketData
from ..notifications.hermes import HermesNotifier, LoggingNotifier
from ..persistence.memory import MemoryTradeStore
from ..strategies.registry import build_strategy


def load_env(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified Binance strategy platform")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--confirm-live", action="store_true",
                        help="second explicit gate required for live orders")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(message)s")
    load_env(args.env_file)
    config = RuntimeConfig.from_file(args.config)
    if config.mode is RunMode.LIVE and not args.confirm_live:
        raise RuntimeError("live mode requires --confirm-live in addition to allow_live=true")
    lock_path = Path(config.state_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("another strategy-platform process already owns this config") from exc

    exchange = BinanceUsdM(os.environ.get("BINANCE_API_KEY", ""),
                           os.environ.get("BINANCE_API_SECRET", ""),
                           os.environ.get("BINANCE_BASE_URL", "https://fapi.binance.com"))
    if config.mode is RunMode.LIVE:
        exchange.assert_one_way_mode()
    market = CachedMarketData(exchange)
    if config.database_backend == "mysql":
        from ..persistence.mysql import MySqlTradeStore
        store = MySqlTradeStore.from_environment()
    else:
        store = MemoryTradeStore()
    socket_path = os.environ.get("HERMES_SOCKET_PATH", "")
    notifier = HermesNotifier(socket_path, config.notify_targets) if socket_path else LoggingNotifier()
    plugins = [build_strategy(
        item.name, item.settings, config.strategy_risk(item.name).leverage, store
    )
               for item in config.strategies if item.enabled]
    strategies = [item for item in plugins if not hasattr(item, "poll_events")]
    event_strategies = [item for item in plugins if hasattr(item, "poll_events")]
    engine = TradingEngine(config, market, exchange, store, notifier, strategies, event_strategies)
    try:
        engine.run_once() if args.once else engine.run_forever()
    finally:
        store.close()


if __name__ == "__main__":
    main()
