import unittest
from decimal import Decimal

from bn_strategy_platform.core.models import Candle, Side, Signal
from bn_strategy_platform.strategies.registry import build_strategy


def candle(index, *, open_price="1", high="1.01", low="0.99", close="1", volume="100"):
    return Candle(
        index, Decimal(open_price), Decimal(high), Decimal(low), Decimal(close),
        index + 1, Decimal(volume),
    )


class TopGainersConfirmationTests(unittest.TestCase):
    def test_pullback_long_requires_higher_low_reclaim_and_volume(self) -> None:
        strategy = build_strategy("bn-stra-top-gainers-exhaustion-short-1", {})
        source = Signal(
            strategy.name, strategy.version, "TESTUSDT", Side.SHORT, Decimal("1"),
            Decimal("1.03"), Decimal("70"), "exhaustion", 1,
            {"model": "exhaustion_short", "momentum": "-0.03"},
        )
        armed = [candle(index, low="0.98", close="0.99") for index in range(20)]
        strategy._arm_entry(source, armed)
        rows = [candle(index, open_price="1", close="1") for index in range(18)]
        rows.extend([
            candle(20, high="1.01", low="0.98", close="0.99"),
            candle(21, open_price="0.99", high="1.03", low="0.99", close="1.02", volume="150"),
        ])

        result = strategy._confirm_entry(
            "TESTUSDT", rows, Decimal("1.02"), Decimal("0.01"),
            Decimal("25"), Decimal("1"), Decimal("1.5"),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.side, Side.LONG)
        self.assertEqual(result.reason, "exhaustion_reclaim_confirmed_long")
        self.assertEqual(result.strategy_version, "4.10.0")
        self.assertEqual(result.metrics["score_model_version"], "pullback-long-v3")

    def test_pullback_continuation_lower_invalidates_setup(self) -> None:
        strategy = build_strategy("bn-stra-top-gainers-exhaustion-short-1", {})
        source = Signal(
            strategy.name, strategy.version, "TESTUSDT", Side.SHORT, Decimal("1"),
            Decimal("1.03"), Decimal("70"), "exhaustion", 1,
            {"momentum": "-0.03"},
        )
        strategy._arm_entry(source, [candle(index, low="0.98") for index in range(20)])
        rows = [candle(index) for index in range(19)]
        rows.append(candle(21, low="0.974", close="0.98"))

        result = strategy._confirm_entry(
            "TESTUSDT", rows, Decimal("0.98"), Decimal("0.01"),
            Decimal("25"), Decimal("1"), Decimal("1"),
        )

        self.assertIsNone(result)
        self.assertNotIn("TESTUSDT", strategy._entry_setups)

    def test_momentum_short_waits_for_lower_high_breakdown(self) -> None:
        strategy = build_strategy("bn-stra-top-gainers-1", {})
        source = Signal(
            strategy.name, strategy.version, "TESTUSDT", Side.LONG, Decimal("1"),
            Decimal("0.97"), Decimal("70"), "momentum_continuation", 1,
            {"model": "continuation_short", "momentum": "0.05"},
        )
        armed = [
            candle(index, open_price="1.02", high="1.05", low="1", close="1.03")
            for index in range(20)
        ]
        strategy._arm_entry(source, armed)
        rows = [
            candle(index, open_price="1.02", high="1.04", low="1", close="1.02")
            for index in range(18)
        ]
        rows.extend([
            candle(20, open_price="1.02", high="1.04", low="1.01", close="1.02"),
            candle(21, open_price="1.02", high="1.03", low="0.98", close="0.99", volume="120"),
        ])

        result = strategy._confirm_entry(
            "TESTUSDT", rows, Decimal("0.99"), Decimal("0.01"),
            Decimal("40"), Decimal("1"), Decimal("2"),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.side, Side.SHORT)
        self.assertEqual(result.reason, "momentum_exhaustion_confirmed_short")
        self.assertGreater(result.stop_price, result.signal_price)
        self.assertEqual(result.metrics["score_model_version"], "momentum-short-v5")


if __name__ == "__main__":
    unittest.main()
