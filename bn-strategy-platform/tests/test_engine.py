from decimal import Decimal
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from bn_strategy_platform.core.config import RuntimeConfig, StrategyConfig
from bn_strategy_platform.core.engine import TradingEngine
from bn_strategy_platform.core.execution import ExecutionService
from bn_strategy_platform.core.position_manager import PositionManager
from bn_strategy_platform.core.models import (AccountSnapshot, Candle, ClosedTrade, Instrument,
                                               OrderFill, Position, PositionAction, RunMode, Side,
                                               Signal, StrategyEvent)
from bn_strategy_platform.persistence.memory import MemoryTradeStore


class Market:
    mark = Decimal("1")

    def instruments(self):
        return {"TESTUSDT": Instrument("TESTUSDT", Decimal("0.001"), Decimal("1"), Decimal("1"), Decimal("5"))}

    def market_summaries(self):
        return []

    def candles(self, symbol, interval, limit):
        return [Candle(i, Decimal("1"), Decimal("1.01"), Decimal("0.99"), Decimal("1"), i + 1,
                       Decimal("100")) for i in range(100)]

    def mark_price(self, symbol):
        return self.mark

    def book(self, symbol):
        return {"bid": self.mark, "ask": self.mark}


class Strategy:
    name = "test-strategy"
    version = "1.2.3"
    interval = "5m"
    candle_limit = 100

    def select_universe(self, market):
        return ["TESTUSDT"]

    def evaluate(self, symbol, candles, mark):
        return Signal(self.name, self.version, symbol, Side.LONG, mark, mark - Decimal("0.02"),
                      Decimal("75"), "setup", 100)

    def manage(self, position, candles, mark):
        return [PositionAction("close", "stop_loss", position.remaining_quantity)] if mark <= position.stop_price else []


class RankedStrategy(Strategy):
    def __init__(self, name, scores):
        self.name = name
        self.scores = scores

    def select_universe(self, market):
        return list(self.scores)

    def evaluate(self, symbol, candles, mark):
        return Signal(
            self.name, self.version, symbol, Side.LONG, mark, mark - Decimal("0.02"),
            Decimal(str(self.scores[symbol])), "ranked_setup", 100,
        )


class WideStopStrategy(Strategy):
    def evaluate(self, symbol, candles, mark):
        return Signal(
            self.name, self.version, symbol, Side.LONG, mark, mark - Decimal("0.05"),
            Decimal("75"), "wide_stop_setup", 100,
        )


class EventStrategy(Strategy):
    def poll_events(self, market):
        signal = self.evaluate("TESTUSDT", market.candles("TESTUSDT", "5m", 100), market.mark)
        return [StrategyEvent("event-1", self.name, "open", "TESTUSDT", Side.LONG, 100, signal)]


class EntryGateStrategy(EventStrategy):
    name = "entry-gate"

    def poll_events(self, market):
        return []

    def entry_block_reason(self, strategy_name, symbol, now_ms):
        return "exclusive reversal lock" if symbol == "TESTUSDT" else None


class LossCooldownStrategy(Strategy):
    settings = SimpleNamespace(
        loss_cooldown_minutes=15,
        reentry_breakout_lookback=3,
        max_symbol_stop_losses_per_day=2,
    )


class Exchange:
    fail_stop = False
    available = Decimal("1000")

    def __init__(self):
        self.stop_quantities = []

    def account(self):
        return AccountSnapshot(Decimal("1000"), self.available)

    def set_leverage(self, symbol, leverage):
        pass

    def enter(self, plan, slippage):
        return OrderFill("1", plan.signal.symbol, "BUY", plan.quantity, plan.signal.signal_price, "FILLED")

    def reduce(self, position, quantity, slippage):
        return OrderFill("2", position.symbol, "SELL", quantity, Market.mark, "FILLED")

    def emergency_close(self, position):
        return self.reduce(position, position.remaining_quantity, Decimal("0"))

    def replace_stop(self, position, stop_price):
        if self.fail_stop:
            raise RuntimeError("stop failed")
        self.stop_quantities.append(position.remaining_quantity)
        return "stop-1"

    def cancel_symbol_orders(self, symbol):
        pass

    def position_amount(self, symbol):
        return Decimal("1")

    def open_position_symbols(self):
        return []

    def settlement(self, position, end_ms):
        return None


class MultiMarket(Market):
    def __init__(self, symbols):
        self.symbols = symbols

    def instruments(self):
        return {
            symbol: Instrument(symbol, Decimal("0.001"), Decimal("1"), Decimal("1"), Decimal("5"))
            for symbol in self.symbols
        }


class ReentryMarket(Market):
    def __init__(self, breakout: bool) -> None:
        latest_close = Decimal("1.02") if breakout else Decimal("1")
        self.rows = [
            Candle(index * 300_000, Decimal("1"), Decimal("1.01"), Decimal("0.99"),
                   latest_close if index == 4 else Decimal("1"),
                   (index + 1) * 300_000 - 1, Decimal("100"))
            for index in range(5)
        ]

    def candles(self, symbol, interval, limit):
        return self.rows[-limit:]


class Notifier:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def config(mode=RunMode.PAPER):
    return RuntimeConfig(
        strategies=(StrategyConfig("test-strategy", True, {}),), mode=mode, poll_seconds=15,
        universe_refresh_seconds=300, max_positions=5, leverage=5,
        margin_per_trade_usdt=Decimal("200"),
        risk_per_trade_usdt=Decimal("20"), max_daily_loss_usdt=Decimal("60"),
        max_open_risk_usdt=Decimal("60"),
        max_directional_open_risk_usdt=Decimal("45"),
        entry_slippage=Decimal("0.002"), exit_slippage=Decimal("0.003"),
        database_backend="memory", state_path="runtime/test.json", notify_targets=(),
        allow_live=mode is RunMode.LIVE, fee_rate=Decimal("0.0005"), paper_equity_usdt=Decimal("1000"),
    )


class EngineTests(unittest.TestCase):
    def test_paper_switch_keeps_existing_live_position_managed(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE),
            strategies=(StrategyConfig("test-strategy", True, {}, mode=RunMode.PAPER),),
        )
        existing = self.existing_live_position()
        existing.quantity = existing.remaining_quantity = Decimal("1")
        existing.stop_price = Decimal("0.98")
        store = MemoryTradeStore()
        store.save_open(existing, RunMode.LIVE.value, Decimal("75"), "existing")
        market = MultiMarket({"TESTUSDT", "OTHERUSDT"})
        strategy = RankedStrategy("test-strategy", {"TESTUSDT": 80, "OTHERUSDT": 90})
        engine = TradingEngine(
            runtime_config, market, Exchange(), store, Notifier(), [strategy],
        )

        engine.run_once()

        self.assertIn(("test-strategy", "OTHERUSDT"), engine.positions)
        self.assertEqual({position.symbol for position in engine.paper_positions.values()},
                         {"TESTUSDT"})
        market.mark = Decimal("0.97")
        engine.run_once()
        self.assertNotIn(("test-strategy", "OTHERUSDT"), engine.positions)
        self.assertIn("stop_loss", {item[3] for item in store.closed})

    def test_external_position_skips_its_symbol_without_stopping_other_live_entries(self) -> None:
        class ExternalExchange(Exchange):
            def open_position_symbols(self):
                return ["TESTUSDT"]

        strategy = RankedStrategy("test-strategy", {"TESTUSDT": 90, "OTHERUSDT": 80})
        store, notifier = MemoryTradeStore(), Notifier()
        engine = TradingEngine(
            config(RunMode.LIVE), MultiMarket({"TESTUSDT", "OTHERUSDT"}),
            ExternalExchange(), store, notifier, [strategy],
        )

        engine.run_once()

        self.assertEqual({position.symbol for position in engine.positions.values()}, {"OTHERUSDT"})
        self.assertTrue(any("TESTUSDT" in message for message in notifier.messages))
        self.assertNotIn("TESTUSDT", {position.symbol for position in store.positions.values()})

    def test_external_position_counts_toward_hard_cap_and_cannot_be_netted(self) -> None:
        class ExternalExchange(Exchange):
            def open_position_symbols(self):
                return ["TESTUSDT"]

        exchange = ExternalExchange()
        execution = ExecutionService(
            replace(config(RunMode.LIVE), max_positions=1, hard_max_positions=1),
            MultiMarket({"TESTUSDT", "OTHERUSDT"}), exchange,
            MemoryTradeStore(), Notifier(), {},
        )
        strategy = RankedStrategy("test-strategy", {"TESTUSDT": 90, "OTHERUSDT": 80})

        with self.assertRaisesRegex(ValueError, "already has Binance position"):
            execution.open(strategy.evaluate("TESTUSDT", [], Decimal("1")))
        with self.assertRaisesRegex(ValueError, "hard maximum concurrent positions"):
            execution.open(strategy.evaluate("OTHERUSDT", [], Decimal("1")))

    def test_planned_risk_rejection_is_treated_as_a_paper_allocation_limit(self) -> None:
        self.assertTrue(TradingEngine._is_allocation_limit(
            ValueError("planned loss 24.5 exceeds per-trade risk limit")
        ))

    def test_planned_risk_rejection_opens_position_limit_paper_trade(self) -> None:
        store = MemoryTradeStore()
        runtime_config = replace(
            config(RunMode.LIVE), paper_on_position_limit=True,
        )
        engine = TradingEngine(
            runtime_config, Market(), Exchange(), store, Notifier(),
            [WideStopStrategy()],
        )

        engine.run_once()

        self.assertFalse(engine.positions)
        self.assertEqual(len(engine.position_limit_paper_positions), 1)
        position = next(iter(engine.position_limit_paper_positions.values()))
        self.assertEqual(store.position_contexts[position.trade_id], "position_limit")
        self.assertEqual(position.initial_stop_price, Decimal("0.95190"))

    @staticmethod
    def existing_live_position() -> Position:
        return Position(
            trade_id="existing-live", strategy="test-strategy", strategy_version="1.2.3",
            symbol="OTHERUSDT", side=Side.LONG, entry_price=Decimal("1"),
            quantity=Decimal("100"), remaining_quantity=Decimal("100"),
            margin=Decimal("20"), leverage=5, stop_price=Decimal("0.8"),
            opened_at_ms=1, best_price=Decimal("1"), stop_order_id="stop-existing",
        )

    def test_paper_trade_is_not_duplicated_and_closes_at_stop(self) -> None:
        market, store = Market(), MemoryTradeStore()
        engine = TradingEngine(config(), market, Exchange(), store, Notifier(), [Strategy()])
        engine.run_once()
        self.assertEqual(len(store.positions), 1)
        opened = next(iter(store.positions.values()))
        self.assertEqual(opened.entry_price, Decimal("1.002"))
        engine.run_once()
        self.assertEqual(len(store.positions), 1)
        market.mark = Decimal("0.97")
        engine.run_once()
        self.assertFalse(store.positions)
        self.assertEqual(store.closed[0][3], "stop_loss")
        self.assertEqual(store.closed[0][1], Decimal("0.96709"))

    def test_position_manager_tracks_adverse_excursion(self) -> None:
        market, store, exchange, notifier = Market(), MemoryTradeStore(), Exchange(), Notifier()
        market.mark = Decimal("0.99")
        position = Position(
            "mae", "test-strategy", "1.2.3", "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("100"), Decimal("100"), Decimal("20"), 5,
            Decimal("0.98"), 1, Decimal("1"), initial_stop_price=Decimal("0.98"),
        )
        positions = {(position.strategy, position.symbol): position}
        store.save_open(position, "paper", Decimal("75"), "test")
        execution = ExecutionService(
            config(), market, exchange, store, notifier, positions,
        )
        manager = PositionManager(
            config(), market, exchange, store, execution, positions,
        )

        manager.manage_all({"test-strategy": Strategy()})

        self.assertEqual(position.best_price, Decimal("1"))
        self.assertEqual(position.worst_price, Decimal("0.99"))

    def test_paper_notifications_are_explicitly_labeled(self) -> None:
        market, store, notifier = Market(), MemoryTradeStore(), Notifier()
        engine = TradingEngine(config(), market, Exchange(), store, notifier, [Strategy()])

        engine.run_once()
        self.assertTrue(notifier.messages[0].startswith("[PAPER OPEN]"))
        market.mark = Decimal("0.97")
        engine.run_once()
        self.assertTrue(notifier.messages[-1].startswith("[PAPER CLOSED]"))

    def test_live_stop_failure_immediately_closes_the_fill(self) -> None:
        market, store, exchange = Market(), MemoryTradeStore(), Exchange()
        exchange.fail_stop = True
        engine = TradingEngine(config(RunMode.LIVE), market, exchange, store, Notifier(), [Strategy()])
        engine.run_once()
        self.assertFalse(store.positions)
        self.assertEqual(store.closed[0][3], "protective_stop_failed")

    def test_live_store_failure_after_fill_immediately_closes_the_fill(self) -> None:
        class FailingOpenStore(MemoryTradeStore):
            def save_open(self, *args, **kwargs):
                raise RuntimeError("database write failed")

        market, store, exchange = Market(), FailingOpenStore(), Exchange()
        engine = TradingEngine(
            config(RunMode.LIVE), market, exchange, store, Notifier(), [Strategy()],
        )

        engine.run_once()

        self.assertEqual(exchange.stop_quantities, [Decimal("1000")])
        self.assertFalse(store.positions)
        self.assertEqual(store.closed[0][3], "protective_stop_failed")

    def test_partial_live_exit_replaces_stop_for_remaining_quantity(self) -> None:
        market, store, exchange, notifier = Market(), MemoryTradeStore(), Exchange(), Notifier()
        position = Position(
            "partial", "test-strategy", "1.2.3", "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("100"), Decimal("100"), Decimal("20"), 5,
            Decimal("0.98"), 1, Decimal("1.04"),
            initial_stop_price=Decimal("0.98"), stop_order_id="old-stop",
        )
        positions = {(position.strategy, position.symbol): position}
        store.save_open(position, "real", Decimal("75"), "test")
        execution = ExecutionService(
            config(RunMode.LIVE), market, exchange, store, notifier, positions,
        )

        execution.close(position, Decimal("50"), "take_profit_1", Decimal("1.04"))

        self.assertEqual(position.remaining_quantity, Decimal("50"))
        self.assertEqual(exchange.stop_quantities[-1], Decimal("50"))
        self.assertIn((position.strategy, position.symbol), positions)

    def test_position_manager_immediately_protects_partial_runner(self) -> None:
        class PartialStrategy(Strategy):
            def manage(self, position, candles, mark):
                if 1 not in position.partial_tiers_done:
                    return [PositionAction("close", "take_profit_1", Decimal("50"), tier=1)]
                return [PositionAction(
                    "move_stop", "post_tp1_lock", stop_price=Decimal("1.02"),
                )]

        market, store, exchange, notifier = Market(), MemoryTradeStore(), Exchange(), Notifier()
        market.mark = Decimal("1.04")
        position = Position(
            "partial", "test-strategy", "1.2.3", "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("100"), Decimal("100"), Decimal("20"), 5,
            Decimal("0.98"), 1, Decimal("1.04"),
            initial_stop_price=Decimal("0.98"), stop_order_id="old-stop",
        )
        positions = {(position.strategy, position.symbol): position}
        store.save_open(position, "real", Decimal("75"), "test")
        execution = ExecutionService(
            config(RunMode.LIVE), market, exchange, store, notifier, positions,
        )
        manager = PositionManager(
            config(RunMode.LIVE), market, exchange, store, execution, positions,
        )

        manager.manage_all({"test-strategy": PartialStrategy()})

        self.assertEqual(position.remaining_quantity, Decimal("50"))
        self.assertEqual(position.partial_tiers_done, (1,))
        self.assertEqual(position.stop_price, Decimal("1.02"))
        self.assertEqual(position.stop_reason, "post_tp1_lock")
        self.assertEqual(exchange.stop_quantities, [Decimal("50"), Decimal("50")])

    def test_scale_in_updates_average_and_reprotects_full_quantity(self) -> None:
        class ScaleInStrategy(Strategy):
            def manage(self, position, candles, mark):
                if position.scale_in_done:
                    return []
                return [
                    PositionAction("move_stop", "breakeven", stop_price=Decimal("1.006")),
                    PositionAction("add", "confirmed_profit_scale_in", margin=Decimal("75")),
                ]

        market, store, exchange, notifier = Market(), MemoryTradeStore(), Exchange(), Notifier()
        market.mark = Decimal("1.035")
        position = Position(
            "scale", "test-strategy", "1.2.3", "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("450"), Decimal("450"), Decimal("90"), 5,
            Decimal("0.97"), 1, Decimal("1.035"),
            initial_stop_price=Decimal("0.97"), stop_order_id="old-stop",
        )
        positions = {(position.strategy, position.symbol): position}
        store.save_open(position, "real", Decimal("75"), "test")
        execution = ExecutionService(
            config(RunMode.LIVE), market, exchange, store, notifier, positions,
        )
        manager = PositionManager(
            config(RunMode.LIVE), market, exchange, store, execution, positions,
        )

        manager.manage_all({"test-strategy": ScaleInStrategy()})

        self.assertTrue(position.scale_in_done)
        self.assertEqual(position.scale_in_price, Decimal("1.035"))
        self.assertEqual(position.initial_entry_price, Decimal("1"))
        self.assertEqual(position.initial_risk_distance, Decimal("0.03"))
        self.assertGreater(position.quantity, Decimal("450"))
        self.assertEqual(position.remaining_quantity, position.quantity)
        self.assertGreater(position.entry_price, Decimal("1"))
        self.assertEqual(exchange.stop_quantities[-1], position.quantity)
        self.assertTrue(any(message.startswith("[ADD]") for message in notifier.messages))

    def test_paper_switch_prevents_adding_to_existing_live_position(self) -> None:
        class ScaleInStrategy(Strategy):
            def manage(self, position, candles, mark):
                return [
                    PositionAction("move_stop", "breakeven", stop_price=Decimal("1.01")),
                    PositionAction("add", "confirmed_profit_scale_in", margin=Decimal("75")),
                ]

        runtime_config = replace(
            config(RunMode.LIVE),
            strategies=(StrategyConfig("test-strategy", True, {}, mode=RunMode.PAPER),),
        )
        market, store, exchange, notifier = Market(), MemoryTradeStore(), Exchange(), Notifier()
        position = self.existing_live_position()
        position.stop_price = Decimal("0.98")
        positions = {(position.strategy, position.symbol): position}
        execution = ExecutionService(
            runtime_config, market, exchange, store, notifier, positions,
        )
        manager = PositionManager(
            runtime_config, market, exchange, store, execution, positions,
        )

        with patch.object(execution, "add") as add:
            manager.manage_all({"test-strategy": ScaleInStrategy()})

        add.assert_not_called()
        self.assertEqual(position.stop_price, Decimal("1.01"))
        self.assertFalse(position.scale_in_done)

    def test_insufficient_margin_is_reported_without_opening(self) -> None:
        market, store, exchange, notifier = Market(), MemoryTradeStore(), Exchange(), Notifier()
        exchange.available = Decimal("100")
        engine = TradingEngine(config(RunMode.LIVE), market, exchange, store, notifier, [Strategy()])

        engine.run_once()

        self.assertFalse(store.positions)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("insufficient available margin", notifier.messages[0])

    def test_strategy_specific_risk_controls_position_size_and_leverage(self) -> None:
        runtime_config = replace(
            config(),
            strategies=(StrategyConfig(
                "test-strategy", True, {},
                {
                    "leverage": 8,
                    "margin_per_trade_usdt": "25",
                    "risk_per_trade_usdt": "5",
                    "base_positions": 1,
                    "max_positions": 2,
                    "max_daily_loss_usdt": "20",
                },
            ),),
        )
        store = MemoryTradeStore()
        engine = TradingEngine(runtime_config, Market(), Exchange(), store, Notifier(), [Strategy()])

        engine.run_once()

        position = next(iter(store.positions.values()))
        self.assertEqual(position.leverage, 8)
        self.assertEqual(position.margin, Decimal("25.050"))
        self.assertEqual(position.quantity, Decimal("200"))

    def test_strategy_can_override_universe_refresh_interval(self) -> None:
        strategy = Strategy()
        strategy.universe_refresh_seconds = 60
        engine = TradingEngine(
            config(), Market(), Exchange(), MemoryTradeStore(), Notifier(), [strategy],
        )

        self.assertEqual(engine._universe_refresh_seconds(engine.runtimes[0]), 60)

    def test_reserved_slots_are_filled_before_score_competition(self) -> None:
        strategies = [
            RankedStrategy("strategy-a", {"A1USDT": 100, "A2USDT": 90}),
            RankedStrategy("strategy-b", {"B1USDT": 10, "B2USDT": 80}),
        ]
        strategy_configs = tuple(
            StrategyConfig(
                strategy.name, True, {},
                {
                    "leverage": 1, "margin_per_trade_usdt": "10",
                    "risk_per_trade_usdt": "1", "base_positions": 1,
                    "max_positions": 2,
                },
            )
            for strategy in strategies
        )
        runtime_config = replace(
            config(RunMode.LIVE), strategies=strategy_configs, max_positions=3,
            max_open_risk_usdt=Decimal("10"),
            max_directional_open_risk_usdt=Decimal("10"),
        )
        store = MemoryTradeStore()
        engine = TradingEngine(
            runtime_config, MultiMarket({"A1USDT", "A2USDT", "B1USDT", "B2USDT"}),
            Exchange(), store, Notifier(), strategies,
        )

        engine.run_once()

        opened = {(position.strategy, position.symbol) for position in store.positions.values()}
        self.assertEqual(opened, {
            ("strategy-a", "A1USDT"),
            ("strategy-a", "A2USDT"),
            ("strategy-b", "B2USDT"),
        })

    def test_protected_stop_releases_global_open_risk_budget(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE), max_open_risk_usdt=Decimal("20.1"),
            max_directional_open_risk_usdt=Decimal("20.1"),
        )
        existing = self.existing_live_position()
        existing.quantity = existing.remaining_quantity = Decimal("1")
        existing.stop_price = Decimal("0.8")
        positions = {(existing.strategy, existing.symbol): existing}
        execution = ExecutionService(
            runtime_config, Market(), Exchange(), MemoryTradeStore(), Notifier(), positions,
        )
        signal = Strategy().evaluate("TESTUSDT", [], Decimal("1"))

        with self.assertRaisesRegex(ValueError, "global open risk limit"):
            execution.open(signal)

        existing.stop_price = Decimal("1.001")
        opened = execution.open(signal)
        self.assertEqual(opened.symbol, "TESTUSDT")

    def test_protected_position_does_not_consume_soft_position_capacity(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE), max_positions=1, hard_max_positions=2,
            strategies=(StrategyConfig(
                "test-strategy", True, {},
                {"max_positions": 2, "risk_per_trade_usdt": "20"},
            ),),
        )
        existing = self.existing_live_position()
        existing.stop_price = Decimal("1.001")
        store = MemoryTradeStore()
        store.save_open(existing, "live", Decimal("70"), "existing")
        market = MultiMarket({"TESTUSDT", "OTHERUSDT"})
        market.mark = Decimal("1.01")
        engine = TradingEngine(
            runtime_config, market,
            Exchange(), store, Notifier(), [Strategy()],
        )

        engine.run_once()

        self.assertEqual(len(engine.positions), 2)
        self.assertIn(("test-strategy", "TESTUSDT"), engine.positions)

    def test_confirmed_signal_rotates_stale_unprotected_position_without_score_gate(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE), max_positions=1, hard_max_positions=1,
            position_rotation_enabled=True, rotation_cooldown_minutes=0,
            strategies=(StrategyConfig(
                "test-strategy", True, {},
                {"max_positions": 2, "risk_per_trade_usdt": "20"},
            ),),
        )
        existing = self.existing_live_position()
        existing.opened_at_ms = 1
        existing.best_price = Decimal("1.005")
        existing.initial_stop_price = Decimal("0.98")
        existing.stop_price = Decimal("0.98")
        existing.entry_score = Decimal("99")
        store, notifier = MemoryTradeStore(), Notifier()
        store.save_open(existing, "live", existing.entry_score, "existing")
        strategy = RankedStrategy("test-strategy", {"TESTUSDT": 10})
        engine = TradingEngine(
            runtime_config, MultiMarket({"TESTUSDT", "OTHERUSDT"}),
            Exchange(), store, notifier, [strategy],
        )

        with patch("bn_strategy_platform.core.engine.time.time", return_value=10_000):
            engine.run_once()

        self.assertNotIn(("test-strategy", "OTHERUSDT"), engine.positions)
        self.assertIn(("test-strategy", "TESTUSDT"), engine.positions)
        self.assertEqual(store.closed[0][3], "capital_rotation")
        self.assertTrue(any("CAPITAL ROTATION" in message for message in notifier.messages))

    def test_cross_strategy_rotation_preserves_reserved_base_position(self) -> None:
        strategy_a = RankedStrategy("strategy-a", {})
        strategy_b = RankedStrategy("strategy-b", {})
        runtime_config = replace(
            config(RunMode.LIVE), position_rotation_enabled=True,
            rotation_min_hold_minutes=0,
            strategies=(
                StrategyConfig("strategy-a", True, {}, {
                    "base_positions": 1, "max_positions": 2,
                }),
                StrategyConfig("strategy-b", True, {}, {
                    "base_positions": 0, "max_positions": 2,
                }),
            ),
        )
        engine = TradingEngine(
            runtime_config, MultiMarket({"AUSDT", "BUSDT"}), Exchange(),
            MemoryTradeStore(), Notifier(), [strategy_a, strategy_b],
        )
        reserved = Position(
            "reserved", "strategy-a", "1", "AUSDT", Side.LONG,
            Decimal("1"), Decimal("10"), Decimal("10"), Decimal("10"), 1,
            Decimal("0.98"), 1, Decimal("1"), initial_stop_price=Decimal("0.98"),
        )
        engine.positions[(reserved.strategy, reserved.symbol)] = reserved
        signal = Signal(
            "strategy-b", "1", "BUSDT", Side.LONG, Decimal("1"),
            Decimal("0.98"), Decimal("1"), "confirmed", 1,
        )

        candidate = engine._rotation_candidate(
            signal, "global hard maximum concurrent positions reached", 10_000,
        )

        self.assertIsNone(candidate)

    def test_event_strategy_can_exclusively_block_normal_live_entry(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE),
            strategies=(
                StrategyConfig("test-strategy", True, {}),
                StrategyConfig("entry-gate", True, {}, mode=RunMode.PAPER),
            ),
        )
        store, exchange = MemoryTradeStore(), Exchange()
        engine = TradingEngine(
            runtime_config, Market(), exchange, store, Notifier(),
            [Strategy()], [EntryGateStrategy()],
        )

        engine.run_once()

        self.assertFalse(store.positions)
        self.assertEqual(len(store.signals), 1)
        self.assertEqual(exchange.stop_quantities, [])

    def test_same_direction_loss_requires_cooldown_and_fresh_5m_breakout(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE),
            strategies=(StrategyConfig("test-strategy", True, {}),),
        )
        store = MemoryTradeStore()
        store.closed_trades.append(ClosedTrade(
            "loss", "test-strategy", "1.2.3", "live", "normal", "TESTUSDT",
            Side.LONG, 100_000, 500_000, Decimal("1"), Decimal("0.98"),
            Decimal("0.98"), Decimal("0.98"), Decimal("-2"), 400, "stop_loss",
        ))
        strategy = LossCooldownStrategy()
        signal = strategy.evaluate("TESTUSDT", [], Decimal("1"))

        cooling = TradingEngine(
            runtime_config, ReentryMarket(False), Exchange(), store, Notifier(), [strategy],
        )
        with patch("bn_strategy_platform.core.engine.time.time", return_value=1_000):
            reason = cooling._entry_block_reason(cooling.runtimes[0], signal)
        self.assertIn("cooldown", reason)

        without_breakout = TradingEngine(
            runtime_config, ReentryMarket(False), Exchange(), store, Notifier(), [strategy],
        )
        with patch("bn_strategy_platform.core.engine.time.time", return_value=1_500):
            reason = without_breakout._entry_block_reason(without_breakout.runtimes[0], signal)
        self.assertIn("fresh 5m structure breakout", reason)

        with_breakout = TradingEngine(
            runtime_config, ReentryMarket(True), Exchange(), store, Notifier(), [strategy],
        )
        with patch("bn_strategy_platform.core.engine.time.time", return_value=1_500):
            reason = with_breakout._entry_block_reason(with_breakout.runtimes[0], signal)
        self.assertIsNone(reason)

    def test_reconciled_exchange_stop_preserves_protection_reason(self) -> None:
        runtime_config = config(RunMode.LIVE)
        store, exchange, notifier = MemoryTradeStore(), Exchange(), Notifier()
        execution = ExecutionService(
            runtime_config, Market(), exchange, store, notifier, {},
        )
        manager = PositionManager(
            runtime_config, Market(), exchange, store, execution, {},
        )
        position = Position(
            "trade", "test-strategy", "1.2.3", "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("100"), Decimal("100"), Decimal("20"), 5,
            Decimal("1.01"), 1, Decimal("1.05"), stop_reason="trailing",
        )

        self.assertEqual(manager._reconciled_close_reason(position, Decimal("1.009")),
                         "trailing_stop")
        position.stop_reason = "breakeven"
        self.assertEqual(manager._reconciled_close_reason(position, Decimal("1.009")),
                         "break_even")
        position.stop_reason = "initial_stop"
        position.stop_price = Decimal("0.98")
        self.assertEqual(manager._reconciled_close_reason(position, Decimal("0.979")),
                         "stop_loss")
        self.assertEqual(manager._reconciled_close_reason(position, Decimal("1.05")),
                         "exchange_close_reconciled")

    def test_two_pure_stop_losses_disable_same_direction_for_the_day(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE),
            strategies=(StrategyConfig("test-strategy", True, {}),),
        )
        store = MemoryTradeStore()
        for index, closed_at in enumerate((400_000, 500_000)):
            store.closed_trades.append(ClosedTrade(
                f"loss-{index}", "test-strategy", "1.2.3", "live", "normal",
                "TESTUSDT", Side.LONG, 100_000, closed_at, Decimal("1"),
                Decimal("0.98"), Decimal("0.98"), Decimal("0.98"), Decimal("-2"),
                300, "stop_loss",
            ))
        strategy = LossCooldownStrategy()
        engine = TradingEngine(
            runtime_config, ReentryMarket(True), Exchange(), store, Notifier(), [strategy],
        )
        signal = strategy.evaluate("TESTUSDT", [], Decimal("1"))

        with patch("bn_strategy_platform.core.engine.time.time", return_value=1_500):
            reason = engine._entry_block_reason(engine.runtimes[0], signal)

        self.assertEqual(reason, "daily symbol stop-loss limit reached (2/2)")

    def test_live_position_limit_opens_manages_and_notifies_a_paper_trade(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE), max_positions=1, paper_on_position_limit=True
        )
        market, store, notifier = Market(), MemoryTradeStore(), Notifier()
        existing = self.existing_live_position()
        store.save_open(existing, "live", Decimal("75"), "existing")
        engine = TradingEngine(runtime_config, market, Exchange(), store, notifier, [Strategy()])

        engine.run_once()

        paper = next(position for position in store.positions.values()
                     if store.position_modes[position.trade_id] == "paper")
        self.assertEqual(paper.symbol, "TESTUSDT")
        self.assertEqual(store.position_contexts[paper.trade_id], "position_limit")
        self.assertEqual(paper.margin, Decimal("200.400"))
        self.assertEqual(len(notifier.messages), 1)
        self.assertTrue(notifier.messages[0].startswith("[PAPER OPEN]"))

        market.mark = Decimal("0.97")
        engine.run_once()

        self.assertNotIn(paper.trade_id, store.positions)
        self.assertEqual(store.closed[-1][3], "stop_loss")
        self.assertEqual(len(notifier.messages), 2)
        self.assertTrue(notifier.messages[-1].startswith("[PAPER CLOSED]"))

    def test_position_limit_paper_trade_is_restored_without_duplicate_open(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE), max_positions=1, paper_on_position_limit=True
        )
        market, store = Market(), MemoryTradeStore()
        existing = self.existing_live_position()
        store.save_open(existing, "live", Decimal("75"), "existing")
        first = TradingEngine(runtime_config, market, Exchange(), store, Notifier(), [Strategy()])
        first.run_once()
        paper_ids = {trade_id for trade_id, mode in store.position_modes.items() if mode == "paper"}

        restarted = TradingEngine(runtime_config, market, Exchange(), store, Notifier(), [Strategy()])
        restarted.run_once()

        self.assertEqual(
            {trade_id for trade_id, mode in store.position_modes.items() if mode == "paper"},
            paper_ids,
        )

    def test_position_limit_event_entry_also_opens_a_paper_trade(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE), max_positions=1, paper_on_position_limit=True
        )
        market, store = Market(), MemoryTradeStore()
        store.save_open(self.existing_live_position(), "live", Decimal("75"), "existing")
        strategy = EventStrategy()
        engine = TradingEngine(
            runtime_config, market, Exchange(), store, Notifier(), [], [strategy]
        )

        engine.run_once()

        paper = [position for position in store.positions.values()
                 if store.position_modes[position.trade_id] == "paper"]
        self.assertEqual(len(paper), 1)
        self.assertEqual(paper[0].symbol, "TESTUSDT")
        self.assertEqual(
            store.events[(strategy.name, "live", "event-1")], "processed"
        )

    def test_paper_strategy_can_run_inside_a_live_platform(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE),
            strategies=(StrategyConfig(
                "test-strategy", True, {}, mode=RunMode.PAPER
            ),),
        )
        store = MemoryTradeStore()
        engine = TradingEngine(
            runtime_config, Market(), Exchange(), store, Notifier(), [Strategy()]
        )

        engine.run_once()

        self.assertEqual(len(store.positions), 1)
        position = next(iter(store.positions.values()))
        self.assertEqual(store.position_modes[position.trade_id], "paper")
        self.assertIsNone(position.stop_order_id)

    def test_paper_strategy_ignores_another_paper_strategys_daily_loss(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE),
            strategies=(StrategyConfig(
                "test-strategy", True, {}, mode=RunMode.PAPER
            ),),
        )
        store = MemoryTradeStore()
        other = replace(self.existing_live_position(), trade_id="other-loss",
                        strategy="other-paper-strategy")
        store.save_open(other, "paper", Decimal("70"), "test")
        store.save_close(other, Decimal("0.8"), Decimal("-100"), "stop_loss")

        engine = TradingEngine(
            runtime_config, Market(), Exchange(), store, Notifier(), [Strategy()]
        )
        engine.run_once()

        current = list(store.positions.values())
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0].strategy, "test-strategy")

    def test_paper_strategy_still_enforces_its_own_daily_loss(self) -> None:
        runtime_config = replace(
            config(RunMode.LIVE),
            strategies=(StrategyConfig(
                "test-strategy", True, {}, mode=RunMode.PAPER
            ),),
        )
        store = MemoryTradeStore()
        prior = replace(self.existing_live_position(), trade_id="strategy-loss",
                        strategy="test-strategy")
        store.save_open(prior, "paper", Decimal("70"), "test")
        store.save_close(prior, Decimal("0.8"), Decimal("-60"), "stop_loss")

        engine = TradingEngine(
            runtime_config, Market(), Exchange(), store, Notifier(), [Strategy()]
        )
        engine.run_once()

        self.assertFalse(store.positions)
