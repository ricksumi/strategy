from decimal import Decimal
import unittest
from unittest.mock import patch

from bn_strategy_platform.core.models import Candle, ClosedTrade, Position, Side
from bn_strategy_platform.persistence.memory import MemoryTradeStore
from bn_strategy_platform.strategies.fast_stop_reversal import (
    FastStopReversalSettings,
    FastStopReversalStrategy,
)


class Market:
    def __init__(self, candles, mark="0.963"):
        self._candles = candles
        self._mark = Decimal(mark)

    def candles(self, symbol, interval, limit):
        return self._candles[-limit:]

    def mark_price(self, symbol):
        return self._mark


def candle(open_time, open_price="1", high="1.01", low="0.99", close="1", volume="100"):
    return Candle(
        open_time, Decimal(open_price), Decimal(high), Decimal(low), Decimal(close),
        open_time + 299_999, Decimal(volume),
    )


def stopped_trade(closed_at=601_000, exit_price="0.975"):
    return ClosedTrade(
        "source-1", "bn-stra-top-gainers-exhaustion-short-1", "4.10.0",
        "real", "normal", "TESTUSDT", Side.LONG, 1_000, closed_at,
        Decimal("1"), Decimal(exit_price), Decimal("0.98"), Decimal("0.98"),
        Decimal("-10"), 600, "exchange_stop_reconciled",
    )


def structured_market(mark="0.963", breakout_volume="200", retest_close="0.964"):
    prior = [
        candle(0, "0.99", "1.01", "0.97", "0.99"),
        candle(300_000, "0.99", "1", "0.975", "0.985"),
        candle(600_000, "0.985", "0.995", "0.98", "0.982"),
    ]
    breakout = candle(900_000, "0.982", "0.988", "0.96", "0.965", breakout_volume)
    retest = candle(1_200_000, "0.968", "0.971", "0.958", retest_close, "120")
    return Market([*prior, breakout, retest], mark)


class FastStopReversalTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryTradeStore()
        self.strategy = FastStopReversalStrategy(FastStopReversalSettings(), self.store)
        self.market = structured_market()

    def test_reverses_only_after_structure_break_and_retest(self):
        event = self.strategy._candidate_event(stopped_trade(), self.market, 1_500_000)

        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "open")
        self.assertEqual(event.side, Side.SHORT)
        self.assertEqual(event.signal.reason, "structured_fast_stop_reversal")
        self.assertEqual(event.signal.metrics["source_loss_r"], "1.25")
        self.assertEqual(event.signal.metrics["breakout_level"], "0.97")
        self.assertEqual(event.signal.metrics["retest_open_time_ms"], 1_200_000)
        self.assertGreater(event.signal.stop_price, event.signal.signal_price)

    def test_does_not_open_on_breakout_before_retest_closes(self):
        market = Market(self.market._candles[:-1], "0.964")

        event = self.strategy._candidate_event(stopped_trade(), market, 1_200_000)

        self.assertIsNone(event)

    def test_symbol_lock_is_rebuilt_for_pullback_source_after_restart(self):
        self.store.fast_stop_candidates.append(stopped_trade())
        with patch(
            "bn_strategy_platform.strategies.fast_stop_reversal.time.time",
            return_value=1_200,
        ):
            self.strategy.poll_events(self.market)

        self.assertIsNotNone(self.strategy.entry_block_reason(
            "bn-stra-top-gainers-exhaustion-short-1", "TESTUSDT", 1_200_000,
        ))

        restarted = FastStopReversalStrategy(FastStopReversalSettings(), self.store)
        with patch(
            "bn_strategy_platform.strategies.fast_stop_reversal.time.time",
            return_value=1_200,
        ):
            restarted.poll_events(self.market)

        self.assertIsNotNone(restarted.entry_block_reason(
            "bn-stra-top-gainers-exhaustion-short-1", "TESTUSDT", 1_200_000,
        ))
        self.assertIsNone(restarted.entry_block_reason(
            "bn-stra-top-gainers-exhaustion-short-1", "TESTUSDT", 2_401_000,
        ))

    def test_rejects_breakout_without_required_volume(self):
        market = structured_market(breakout_volume="120")

        event = self.strategy._candidate_event(stopped_trade(), market, 2_500_000)

        self.assertEqual(event.kind, "skip_volume")
        self.assertIsNone(event.signal)

    def test_rejects_source_that_lost_less_than_point_six_r(self):
        event = self.strategy._candidate_event(
            stopped_trade(exit_price="0.99"), self.market, 1_500_000,
        )

        self.assertEqual(event.kind, "skip_source_loss")

    def test_retest_that_reclaims_original_stop_invalidates_reversal(self):
        market = structured_market(mark="0.985", retest_close="0.985")

        event = self.strategy._candidate_event(stopped_trade(), market, 1_500_000)

        self.assertEqual(event.kind, "skip_retest_invalidated")

    def test_only_one_reversal_is_allowed_per_symbol_per_day(self):
        source = stopped_trade(1_774_000_001_000)
        existing = Position(
            "reversal-win", self.strategy.name, self.strategy.version, "TESTUSDT",
            Side.SHORT, Decimal("1"), Decimal("100"), Decimal("100"), Decimal("75"),
            3, Decimal("1.02"), 1_774_000_100_000, Decimal("1"),
        )
        self.store.save_open(existing, "paper", Decimal("70"), "test")

        event = self.strategy._candidate_event(source, self.market, 1_774_000_200_000)

        self.assertEqual(event.kind, "skip_daily_limit")
        self.assertIsNone(event.signal)

    def test_manages_profit_with_the_shared_staged_stop_shape(self):
        position = Position(
            "reversal", self.strategy.name, self.strategy.version, "TESTUSDT",
            Side.LONG, Decimal("1"), Decimal("600"), Decimal("600"), Decimal("75"),
            3, Decimal("0.97"), 1, Decimal("1.06"),
        )

        actions = list(self.strategy.manage(position, [], Decimal("1.05")))

        self.assertEqual(actions[0].kind, "move_stop")
        self.assertEqual(actions[0].reason, "trailing")
        self.assertGreater(actions[0].stop_price, Decimal("1"))


if __name__ == "__main__":
    unittest.main()
