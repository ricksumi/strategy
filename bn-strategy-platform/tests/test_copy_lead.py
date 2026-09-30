import unittest
from decimal import Decimal
from unittest.mock import patch

from bn_strategy_platform.core.models import Candle, ClosedTrade, Position, Side, Signal, StrategyEvent
from bn_strategy_platform.persistence.memory import MemoryTradeStore
from bn_strategy_platform.strategies.copy_lead import CopyLeadSettings, CopyLeadStrategy


class CopyLeadTests(unittest.TestCase):
    def test_events_before_live_cutover_are_ignored_without_market_lookup(self) -> None:
        class NoMarketCalls:
            def mark_price(self, symbol):
                raise AssertionError("historical event must not request market data")

        strategy = CopyLeadStrategy(CopyLeadSettings(
            portfolio_id="123",
            event_not_before_ms=2_000,
        ))
        row = {
            "symbol": "CLUSDT",
            "positionSide": "LONG",
            "side": "BUY",
            "orderTime": 1_000,
            "orderUpdateTime": 1_500,
        }

        self.assertIsNone(strategy._event(row, NoMarketCalls()))

    def test_stale_open_event_is_ignored_without_market_lookup(self) -> None:
        class NoMarketCalls:
            def mark_price(self, symbol):
                raise AssertionError("stale event must not request market data")

        strategy = CopyLeadStrategy(CopyLeadSettings(
            portfolio_id="123",
            max_entry_delay_seconds=300,
        ))
        row = {
            "symbol": "ETHUSDT",
            "positionSide": "LONG",
            "side": "BUY",
            "orderTime": 1_000,
            "orderUpdateTime": 1_000,
        }

        with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=302):
            self.assertIsNone(strategy._event(row, NoMarketCalls()))

    def test_open_is_skipped_when_price_has_moved_too_far_from_the_lead(self) -> None:
        class Market:
            def mark_price(self, symbol):
                return Decimal("1.02")

        strategy = CopyLeadStrategy(CopyLeadSettings(
            portfolio_id="123", max_lead_price_deviation=Decimal("0.005"),
        ))
        row = {
            "symbol": "ETHUSDT", "positionSide": "LONG", "side": "BUY",
            "orderTime": 1_000, "orderUpdateTime": 1_000,
            "executedQty": "2000", "avgPrice": "1",
        }

        with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=1):
            event = strategy._event(row, Market())

        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "skip_price_deviation")

    def test_same_direction_loss_blocks_a_fresh_lead_event_during_cooldown(self) -> None:
        store = MemoryTradeStore()
        strategy = CopyLeadStrategy(CopyLeadSettings(
            portfolio_id="123", loss_cooldown_minutes=60,
        ), store)
        now_ms = 10_000_000
        store.closed_trades.append(ClosedTrade(
            "old", strategy.name, strategy.version, "live", "normal", "BTCUSDT",
            Side.SHORT, now_ms - 600_000, now_ms - 60_000, Decimal("1"),
            Decimal("1.01"), Decimal("1.01"), Decimal("1.01"), Decimal("-1"),
            540, "stop_loss",
        ))
        signal = Signal(
            strategy.name, strategy.version, "BTCUSDT", Side.SHORT, Decimal("1"),
            Decimal("1.01"), Decimal("80"), "lead_open", now_ms,
        )
        event = StrategyEvent(
            "event", strategy.name, "open", "BTCUSDT", Side.SHORT, now_ms, signal,
        )

        with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=now_ms / 1000):
            reason = strategy.event_entry_block_reason(event, "live")

        self.assertIn("same-direction loss cooldown", reason)

    def test_profit_management_reduces_risk_before_one_r(self) -> None:
        strategy = CopyLeadStrategy(CopyLeadSettings(portfolio_id="123"))
        position = Position(
            "trade", strategy.name, strategy.version, "BTCUSDT", Side.SHORT,
            Decimal("100"), Decimal("1"), Decimal("1"), Decimal("33.33"), 3,
            Decimal("103"), 1, Decimal("98.5"), initial_stop_price=Decimal("103"),
        )

        actions = list(strategy.manage(position, [], Decimal("99")))

        self.assertEqual(actions[0].kind, "move_stop")
        self.assertEqual(actions[0].reason, "risk_reduction")
        self.assertEqual(actions[0].stop_price, Decimal("100.6"))


if __name__ == "__main__":
    unittest.main()
