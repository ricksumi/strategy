from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from bn_strategy_platform.core.models import Candle, Instrument, Position, Side
from bn_strategy_platform.strategies.momentum_ignition_long import (
    MomentumIgnitionLongStrategy,
    MomentumIgnitionSettings,
)


def candle(index: int, open_: Decimal, high: Decimal, low: Decimal,
           close: Decimal, volume: str = "100") -> Candle:
    opened = index * 300_000
    return Candle(opened, open_, high, low, close, opened + 299_999, Decimal(volume))


def rising_series() -> list[Candle]:
    rows: list[Candle] = []
    for index in range(200):
        if index < 152:
            close = Decimal("1")
        elif index < 188:
            close = Decimal("1") + Decimal(index - 152) / Decimal("35") * Decimal("0.025")
        else:
            close = Decimal("1.025") + Decimal(index - 188) / Decimal("11") * Decimal("0.036")
        opened = close - Decimal("0.001")
        rows.append(candle(
            index, opened, close + Decimal("0.002"), close - Decimal("0.003"),
            close, "100",
        ))
    rows[-6] = candle(194, Decimal("1.039"), Decimal("1.044"), Decimal("1.036"), Decimal("1.042"))
    rows[-5] = candle(195, Decimal("1.041"), Decimal("1.047"), Decimal("1.039"), Decimal("1.045"))
    rows[-4] = candle(196, Decimal("1.044"), Decimal("1.050"), Decimal("1.042"), Decimal("1.048"))
    rows[-3] = candle(197, Decimal("1.047"), Decimal("1.052"), Decimal("1.045"), Decimal("1.050"), "220")
    rows[-2] = candle(198, Decimal("1.049"), Decimal("1.056"), Decimal("1.047"), Decimal("1.054"), "220")
    rows[-1] = candle(199, Decimal("1.053"), Decimal("1.063"), Decimal("1.051"), Decimal("1.061"), "220")
    return rows


def flat_btc() -> list[Candle]:
    return [candle(i, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"))
            for i in range(200)]


class Market:
    def __init__(self) -> None:
        self.series = {"TESTUSDT": rising_series(), "BTCUSDT": flat_btc()}
        self.mark = Decimal("1.061")
        self.flow = {
            "open_interest_change_15m": Decimal("0.04"),
            "taker_buy_sell_ratio_15m": Decimal("1.35"),
            "funding_rate": Decimal("0.0001"),
            "top_position_ratio": Decimal("1.3"),
            "top_position_ratio_change": Decimal("0.1"),
        }

    def instruments(self):
        return {
            symbol: Instrument(symbol, Decimal("0.0001"), Decimal("1"),
                               Decimal("1"), Decimal("5"))
            for symbol in self.series
        }

    def market_summaries(self):
        return [{
            "symbol": "TESTUSDT", "quoteVolume": "50000000",
            "priceChangePercent": "8", "lastPrice": "1.061",
            "bidPrice": "1.0605", "askPrice": "1.0615",
        }]

    def candles(self, symbol, interval, limit):
        return self.series[symbol][-limit:]

    def mark_price(self, symbol):
        return self.mark

    def futures_flow(self, symbol):
        return self.flow


class MomentumIgnitionLongTests(unittest.TestCase):
    @staticmethod
    def strategy() -> MomentumIgnitionLongStrategy:
        return MomentumIgnitionLongStrategy(MomentumIgnitionSettings(
            max_ema_distance_atr=Decimal("4"),
        ))

    def test_registers_only_after_technical_and_flow_confirmation(self) -> None:
        strategy = self.strategy()
        market = Market()

        with patch(
            "bn_strategy_platform.strategies.momentum_ignition_long.time.time",
            return_value=60_100,
        ):
            universe = strategy.select_universe(market)

        self.assertEqual(universe, ["TESTUSDT"])
        snapshot = strategy._snapshots["TESTUSDT"]
        self.assertGreaterEqual(snapshot.score, Decimal("60"))
        self.assertEqual(snapshot.metrics["open_interest_change_15m"], "0.04")

    def test_rejects_breakout_when_open_interest_does_not_expand(self) -> None:
        strategy = self.strategy()
        market = Market()
        market.flow["open_interest_change_15m"] = Decimal("0.01")

        self.assertEqual(strategy.select_universe(market), [])

    def test_waits_for_two_closed_bars_after_the_first_pullback(self) -> None:
        strategy = self.strategy()
        market = Market()
        with patch(
            "bn_strategy_platform.strategies.momentum_ignition_long.time.time",
            return_value=60_100,
        ):
            strategy.select_universe(market)
            snapshot = strategy._snapshots["TESTUSDT"]
            touch_price = snapshot.breakout_close - snapshot.atr_price * Decimal("0.6")
            self.assertIsNone(strategy.evaluate("TESTUSDT", market.series["TESTUSDT"], touch_price))

        first = candle(
            201, snapshot.breakout_level, snapshot.breakout_level + Decimal("0.006"),
            snapshot.breakout_level - Decimal("0.001"),
            snapshot.breakout_level + Decimal("0.003"), "150",
        )
        with patch(
            "bn_strategy_platform.strategies.momentum_ignition_long.time.time",
            return_value=60_500,
        ):
            self.assertIsNone(strategy.evaluate(
                "TESTUSDT", [*market.series["TESTUSDT"], first], first.close,
            ))
        second = candle(
            202, first.close, first.close + Decimal("0.005"),
            snapshot.breakout_level, first.close + Decimal("0.002"), "150",
        )
        with patch(
            "bn_strategy_platform.strategies.momentum_ignition_long.time.time",
            return_value=60_800,
        ):
            signal = strategy.evaluate(
                "TESTUSDT", [*market.series["TESTUSDT"], first, second], second.close,
            )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, Side.LONG)
        self.assertEqual(signal.reason, "flow_confirmed_breakout_pullback")
        self.assertLess(signal.stop_price, signal.signal_price)

    def test_manage_reduces_risk_then_partially_takes_profit(self) -> None:
        strategy = self.strategy()
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("10"), Decimal("100"), 3,
            Decimal("98"), 1, Decimal("101.2"), initial_stop_price=Decimal("98"),
        )

        protection = list(strategy.manage(position, [], Decimal("101.2")))
        self.assertEqual(protection[0].reason, "risk_reduction")
        self.assertEqual(protection[0].stop_price, Decimal("99.50"))

        position.stop_price = Decimal("100.2")
        position.best_price = Decimal("104")
        target = list(strategy.manage(position, [], Decimal("104")))
        self.assertEqual(target[0].reason, "take_profit_1")
        self.assertEqual(target[0].quantity, Decimal("3.0"))


if __name__ == "__main__":
    unittest.main()
