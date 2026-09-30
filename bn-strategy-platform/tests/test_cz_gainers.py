from __future__ import annotations

import time
import unittest
from decimal import Decimal
from unittest.mock import patch

from bn_strategy_platform.core.models import Candle, Instrument, Position, Side
from bn_strategy_platform.strategies.cz_gainers import CzGainersSettings, CzGainersStrategy


def candle(index: int, open_: str, high: str, low: str, close: str, volume: str = "100") -> Candle:
    return Candle(index, Decimal(open_), Decimal(high), Decimal(low), Decimal(close), index + 1,
                  Decimal(volume))


def live_series(final_volume: str = "150") -> list[Candle]:
    rows = [candle(i, "1", "1.01", "0.99", "1", "100") for i in range(17)]
    rows[-3] = candle(14, "1.04", "1.055", "1.03", "1.05", "100")
    rows[-2] = candle(15, "1.05", "1.065", "1.04", "1.06", "100")
    rows[-1] = candle(16, "1.06", "1.085", "1.055", "1.08", final_volume)
    return rows


def closed_30m() -> list[Candle]:
    rows = [candle(i, "1", "1.2" if i == 10 else "1.11", "0.99", "1.05") for i in range(48)]
    rows[-4] = candle(44, "1.04", "1.1", "1.03", "1.05")
    rows[-3] = candle(45, "1.05", "1.1", "1.04", "1.06")
    rows[-2] = candle(46, "1.06", "1.11", "1.05", "1.07")
    rows[-1] = candle(47, "1.07", "1.11", "1.06", "1.08")
    return rows


class Market:
    def __init__(self, series: dict[str, list[Candle]]) -> None:
        self.series = series
        self.mark = Decimal("1.08")

    def instruments(self):
        return {symbol: Instrument(symbol, Decimal("0.001"), Decimal("1"), Decimal("1"), Decimal("5"))
                for symbol in self.series}

    def market_summaries(self):
        return [{"symbol": symbol, "quoteVolume": "20000000", "priceChangePercent": "10",
                 "lastPrice": "1.08", "bidPrice": "1.0795", "askPrice": "1.0805"}
                for symbol in self.series]

    def live_candles(self, symbol, interval, limit):
        raise AssertionError("CZ v2 ranking must not use an open candle")

    def mark_price(self, symbol):
        return self.mark

    def candles(self, symbol, interval, limit):
        if interval == "30m":
            return closed_30m()
        if interval == "1h":
            return [candle(i, "1", "1.1", "0.99", str(Decimal("1.02") + Decimal(i) / 100))
                    for i in range(4)]
        return self.series[symbol][-limit:]


class CzGainersTests(unittest.TestCase):
    def test_rank_one_registers_and_enters_only_after_two_percent_pullback(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        market = Market({"FASTUSDT": live_series(), "SLOWUSDT": live_series()})
        market.series["SLOWUSDT"][-1] = candle(16, "1.02", "1.05", "1.01", "1.06", "150")

        with patch("bn_strategy_platform.strategies.cz_gainers.time.time", return_value=2):
            self.assertEqual(
                strategy.select_universe(market), ["FASTUSDT", "SLOWUSDT"],
            )
            self.assertIsNone(strategy.evaluate("FASTUSDT", live_series(), Decimal("1.065")))
            self.assertIsNone(strategy.evaluate("FASTUSDT", live_series(), Decimal("1.0584")))
        reclaimed = [
            *live_series(),
            candle(3_000, "1.058", "1.065", "1.056", "1.060", "160"),
        ]
        with patch("bn_strategy_platform.strategies.cz_gainers.time.time", return_value=4):
            signal = strategy.evaluate("FASTUSDT", reclaimed, Decimal("1.060"))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.stop_price, Decimal("1.03880"))
        self.assertEqual(Decimal(signal.metrics["tp1_price"]), Decimal("1.107700"))
        self.assertEqual(
            Decimal(signal.metrics["runner_trailing_activation_price"]), Decimal("1.16600")
        )
        self.assertEqual(Decimal(signal.metrics["runner_target_price"]), Decimal("1.25080"))
        self.assertEqual(signal.metrics["rank"], 1)
        self.assertEqual(signal.metrics["reclaim_close"], "1.060")
        self.assertLess(signal.score, Decimal("100"))

    def test_touch_does_not_enter_until_a_later_closed_five_minute_reclaim(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        market = Market({"FASTUSDT": live_series()})
        with patch("bn_strategy_platform.strategies.cz_gainers.time.time", return_value=2):
            strategy.select_universe(market)
            self.assertIsNone(strategy.evaluate("FASTUSDT", live_series(), Decimal("1.050")))
            self.assertIsNone(strategy.evaluate("FASTUSDT", live_series(), Decimal("1.060")))

        failed = [
            *live_series(),
            candle(3_000, "1.055", "1.060", "1.050", "1.057", "160"),
        ]
        with patch("bn_strategy_platform.strategies.cz_gainers.time.time", return_value=4):
            self.assertIsNone(strategy.evaluate("FASTUSDT", failed, Decimal("1.060")))

    def test_rank_one_does_not_fall_through_because_of_deprecated_volume_gate(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        too_much_volume = live_series("400")
        second = live_series("150")
        second[-1] = candle(16, "1.05", "1.075", "1.04", "1.07", "150")
        market = Market({"FASTUSDT": too_much_volume, "SECONDUSDT": second})

        self.assertEqual(strategy.select_universe(market)[0], "FASTUSDT")

    def test_filtering_rank_one_promotes_the_next_qualified_candidate(self) -> None:
        class NegativeRankOneMarket(Market):
            def candles(self, symbol, interval, limit):
                if interval == "30m" and symbol == "FASTUSDT":
                    rows = closed_30m()
                    rows[-1] = candle(47, "1", "1.01", "0.8", "0.8")
                    return rows
                return super().candles(symbol, interval, limit)

        fast = live_series()
        second = live_series()
        second[-1] = candle(16, "1.05", "1.075", "1.04", "1.07", "150")
        strategy = CzGainersStrategy(CzGainersSettings())
        market = NegativeRankOneMarket({"FASTUSDT": fast, "SECONDUSDT": second})

        self.assertEqual(strategy.select_universe(market), ["SECONDUSDT"])

    def test_pullback_at_four_percent_is_invalidated_and_cannot_rearm_immediately(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        market = Market({"FASTUSDT": live_series()})
        self.assertEqual(strategy.select_universe(market), ["FASTUSDT"])

        self.assertIsNone(strategy.evaluate("FASTUSDT", live_series(), Decimal("1.0368")))

        self.assertNotIn("FASTUSDT", strategy._snapshots)
        self.assertEqual(strategy.select_universe(market), [])

    def test_three_candidates_track_pullbacks_independently(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings(active_candidate_limit=3))
        fast = live_series()
        medium = live_series()
        slow = live_series()
        medium[-1] = candle(16, "1.05", "1.08", "1.04", "1.07", "150")
        slow[-1] = candle(16, "1.04", "1.07", "1.03", "1.06", "150")
        market = Market({"FASTUSDT": fast, "MEDIUMUSDT": medium, "SLOWUSDT": slow})

        self.assertEqual(
            strategy.select_universe(market),
            ["FASTUSDT", "MEDIUMUSDT", "SLOWUSDT"],
        )
        self.assertIsNone(
            strategy.evaluate("FASTUSDT", fast, Decimal("1.0368")),
        )

        self.assertNotIn("FASTUSDT", strategy._snapshots)
        self.assertIn("MEDIUMUSDT", strategy._snapshots)
        self.assertIn("SLOWUSDT", strategy._snapshots)

    def test_suffix_denylist_keeps_named_exceptions(self) -> None:
        self.assertTrue(CzGainersStrategy._denied("USDCUSDT"))
        self.assertTrue(CzGainersStrategy._denied("ABCUPUSDT"))
        self.assertFalse(CzGainersStrategy._denied("JUPUSDT"))
        self.assertFalse(CzGainersStrategy._denied("SYRUPUSDT"))

    def test_position_management_uses_the_initial_stop_as_one_r(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("10"), Decimal("125"), 8,
            Decimal("98"), int(time.time() * 1000), Decimal("103"),
        )

        breakeven = list(strategy.manage(position, [], Decimal("103")))
        self.assertEqual(breakeven[0].reason, "second_profit_guard")
        self.assertEqual(breakeven[0].stop_price, Decimal("101.00"))

        position.stop_price = Decimal("100.15")
        position.best_price = Decimal("104.5")
        first_target = list(strategy.manage(position, [], Decimal("104.5")))
        self.assertEqual(first_target[0].reason, "take_profit_1")
        self.assertEqual(first_target[0].quantity, Decimal("5.0"))
        self.assertEqual(first_target[0].tier, 1)

        position.remaining_quantity = Decimal("5")
        position.partial_tiers_done = (1,)
        position.best_price = Decimal("116")
        runner = list(strategy.manage(position, [], Decimal("116")))
        self.assertEqual(runner[0].reason, "trailing")
        self.assertEqual(runner[0].stop_price, Decimal("113.0"))

    def test_partial_runner_locks_one_r_before_trailing_activation(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("5"), Decimal("125"), 8,
            Decimal("101.5"), int(time.time() * 1000), Decimal("104.5"),
            initial_stop_price=Decimal("98"), partial_tiers_done=(1,),
        )

        actions = list(strategy.manage(position, [], Decimal("104.5")))

        self.assertEqual(actions[0].reason, "post_tp1_lock")
        self.assertEqual(actions[0].stop_price, Decimal("102"))

    def test_early_profit_guards_lock_margin_roi_before_one_r(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings(early_profit_guards_enabled=True))
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("10"), Decimal("125"), 8,
            Decimal("98"), int(time.time() * 1000), Decimal("101.5"),
        )

        first = list(strategy.manage(position, [], Decimal("101.5")))
        self.assertEqual(first[0].reason, "early_profit_guard")
        self.assertEqual(first[0].stop_price, Decimal("100.2500"))

        position.stop_price = first[0].stop_price
        position.best_price = Decimal("102.5")
        second = list(strategy.manage(position, [], Decimal("102.5")))
        self.assertEqual(second[0].reason, "second_profit_guard")
        self.assertEqual(second[0].stop_price, Decimal("101.00"))

    def test_crossed_early_profit_guard_closes_instead_of_placing_invalid_stop(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings(early_profit_guards_enabled=True))
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("10"), Decimal("125"), 8,
            Decimal("98"), int(time.time() * 1000), Decimal("102.5"),
        )

        actions = list(strategy.manage(position, [], Decimal("100.5")))

        self.assertEqual(actions[0].kind, "close")
        self.assertEqual(actions[0].reason, "protected_stop")

    def test_score_uses_cz_specific_model_and_component_breakdown(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        market = Market({"FASTUSDT": live_series()})
        with patch("bn_strategy_platform.strategies.cz_gainers.time.time", return_value=2):
            strategy.select_universe(market)
            strategy.evaluate("FASTUSDT", live_series(), Decimal("1.0584"))
        reclaimed = [
            *live_series(),
            candle(3_000, "1.058", "1.065", "1.056", "1.060", "160"),
        ]
        with patch("bn_strategy_platform.strategies.cz_gainers.time.time", return_value=4):
            signal = strategy.evaluate("FASTUSDT", reclaimed, Decimal("1.060"))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.metrics["score_model_version"], "cz-gainers-v5")
        self.assertEqual(signal.metrics["rank"], 1)
        self.assertIn("rank_quality", signal.metrics["score_breakdown"])
        self.assertIn("pullback_quality", signal.metrics["score_breakdown"])

    def test_red_rank_bar_is_not_rejected_by_a_deprecated_candle_gate(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        series = live_series()
        series[-1] = candle(16, "1.10", "1.12", "1.05", "1.08", "150")
        market = Market({"BROKENUSDT": series})
        self.assertEqual(strategy.select_universe(market), ["BROKENUSDT"])

    def test_runner_closes_at_nine_actual_r(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("5"), Decimal("125"), 8,
            Decimal("102"), int(time.time() * 1000), Decimal("118"),
            initial_stop_price=Decimal("98"), partial_tiers_done=(1,),
        )

        actions = list(strategy.manage(position, [], Decimal("118")))

        self.assertEqual(actions[0].reason, "take_profit_2")
        self.assertEqual(actions[0].quantity, Decimal("5"))

    def test_open_cooldown_can_be_restored_after_restart(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        now_ms = int(time.time() * 1000)
        strategy.restore_open_times({"FASTUSDT": now_ms})
        self.assertTrue(strategy._cooling_down("FASTUSDT", now_ms + 60_000))

    def test_only_losing_time_stop(self) -> None:
        strategy = CzGainersStrategy(CzGainersSettings())
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("10"), Decimal("125"), 8,
            Decimal("98"), int(time.time() * 1000) - 5 * 3_600_000, Decimal("100"),
        )
        actions = list(strategy.manage(position, [], Decimal("99.5")))
        self.assertEqual(actions[0].reason, "time_stop")


if __name__ == "__main__":
    unittest.main()
