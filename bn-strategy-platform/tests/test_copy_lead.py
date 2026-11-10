import unittest
import io
import json
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
            "executedQty": "2000", "avgPrice": "1",
        }

        self.assertEqual(strategy._event(row, NoMarketCalls()).kind, "skip_before_cutover")

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
            "executedQty": "2000", "avgPrice": "1",
        }

        with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=302):
            self.assertEqual(strategy._event(row, NoMarketCalls()).kind, "skip_stale")

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

    @staticmethod
    def order(observed=46_000, **changes):
        return {"symbol": "TESTUSDT", "positionSide": "LONG", "side": "BUY",
                "orderTime": observed, "orderUpdateTime": observed,
                "executedQty": "2000", "avgPrice": "1", **changes}

    @staticmethod
    def market():
        class Market:
            def mark_price(self, symbol):
                return Decimal("1")

            def candles(self, symbol, interval, limit):
                return [Candle(i, Decimal("1"), Decimal("1.01"), Decimal("0.99"),
                               Decimal("1"), i + 1, Decimal("100")) for i in range(20)]
        return Market()

    def test_observed_delay_passes_300_seconds_but_not_120(self) -> None:
        for limit, expected in ((120, "skip_stale"), (300, "open")):
            with self.subTest(limit=limit):
                store = MemoryTradeStore()
                strategy = CopyLeadStrategy(CopyLeadSettings(
                    portfolio_id="123", max_entry_delay_seconds=limit,
                ), store)
                with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=200):
                    event = strategy._event(self.order(), self.market())
                self.assertEqual(event.kind, expected)
                audit = next(iter(store.lead_orders.values()))
                self.assertEqual(audit["first_seen_delay_ms"], 154_000)
                self.assertEqual(audit["raw_order"], self.order())

    def test_stale_order_is_audited_without_market_calls_and_first_seen_is_preserved(self) -> None:
        store = MemoryTradeStore()
        strategy = CopyLeadStrategy(CopyLeadSettings(
            portfolio_id="123", max_entry_delay_seconds=300,
        ), store)
        with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=400):
            event = strategy._event(self.order(), object())
        self.assertEqual(event.kind, "skip_stale")
        store.save_event(event, "processed", "paper", strategy.version)
        strategy.record_event_decision(event, "skipped", "stale")
        with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=500):
            self.assertIsNone(strategy._event(self.order(), object()))
        audit = next(iter(store.lead_orders.values()))
        self.assertEqual(audit["first_seen_at_ms"], 400_000)
        self.assertEqual(audit["last_seen_at_ms"], 500_000)
        self.assertEqual(audit["metrics"]["order_age_ms"], 354_000)
        self.assertEqual(audit["reason"], "stale")

    def test_audit_retains_different_filter_reasons(self) -> None:
        cases = [
            ({"executedQty": "1"}, {}, "skip_notional"),
            ({"executedQty": "NaN"}, {}, "skip_invalid_order"),
            ({}, {"blocked_symbols": ("TESTUSDT",)}, "skip_blocked_symbol"),
            ({}, {"event_not_before_ms": 100_000}, "skip_before_cutover"),
            ({"avgPrice": "0.9"}, {}, "skip_price_deviation"),
        ]
        for row_changes, settings_changes, expected in cases:
            with self.subTest(reason=expected):
                store = MemoryTradeStore()
                strategy = CopyLeadStrategy(CopyLeadSettings(
                    portfolio_id="123", max_entry_delay_seconds=300, **settings_changes,
                ), store)
                with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=200):
                    event = strategy._event(self.order(**row_changes), self.market())
                self.assertEqual(event.kind, expected)
                self.assertEqual(next(iter(store.lead_orders.values()))["reason"],
                                 expected.removeprefix("skip_"))

    def test_event_that_expires_while_waiting_to_execute_is_blocked(self) -> None:
        strategy = CopyLeadStrategy(CopyLeadSettings(
            portfolio_id="123", max_entry_delay_seconds=300,
        ))
        with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=200):
            event = strategy._event(self.order(), self.market())
        with patch("bn_strategy_platform.strategies.copy_lead.time.time", return_value=400):
            self.assertIn("entry delay", strategy.event_entry_block_reason(event, "paper"))

    def test_fetch_reads_all_pages_and_keeps_a_stable_time_window(self) -> None:
        strategy = CopyLeadStrategy(CopyLeadSettings(portfolio_id="123", page_size=2))
        pages = [
            {"code": "000000", "success": True, "data": {"total": 3, "list": [self.order(), self.order(47_000)]}},
            {"code": "000000", "success": True, "data": {"total": 3, "list": [self.order(48_000)]}},
        ]
        with patch("urllib.request.urlopen", side_effect=[
            io.BytesIO(json.dumps(page).encode()) for page in pages
        ]) as request:
            rows = strategy._fetch(1_000, 200_000)
        self.assertEqual(len(rows), 3)
        bodies = [json.loads(call.args[0].data) for call in request.call_args_list]
        self.assertEqual([body["pageNumber"] for body in bodies], [1, 2])
        self.assertEqual({body["startTime"] for body in bodies}, {1_000})
        self.assertEqual({body["endTime"] for body in bodies}, {200_000})

    def test_busy_response_is_retried_once(self) -> None:
        strategy = CopyLeadStrategy(CopyLeadSettings(portfolio_id="123"))
        payloads = [
            {"code": "11012005", "success": False},
            {"code": "000000", "success": True, "data": {"total": 0, "list": []}},
        ]
        with patch("urllib.request.urlopen", side_effect=[
            io.BytesIO(json.dumps(payload).encode()) for payload in payloads
        ]) as request, patch("time.sleep"):
            self.assertEqual(strategy._fetch(1_000, 200_000), [])
        self.assertEqual(request.call_count, 2)

    def test_later_page_failure_does_not_discard_received_orders(self) -> None:
        strategy = CopyLeadStrategy(CopyLeadSettings(portfolio_id="123", page_size=1))
        page = {"data": {"total": 2, "list": [self.order()]}}
        with patch.object(strategy, "_fetch_page", side_effect=[page, RuntimeError("busy")]):
            with self.assertLogs(level="ERROR"):
                rows = strategy._fetch(1_000, 200_000)
        self.assertEqual(rows, [self.order()])

    def test_repeated_page_stops_pagination(self) -> None:
        strategy = CopyLeadStrategy(CopyLeadSettings(portfolio_id="123", page_size=1))
        page = {"data": {"total": 10, "list": [self.order()]}}
        with patch.object(strategy, "_fetch_page", return_value=page) as fetch:
            with self.assertLogs(level="ERROR"):
                self.assertEqual(strategy._fetch(1_000, 200_000), [self.order()])
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
