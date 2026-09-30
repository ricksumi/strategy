import unittest
from decimal import Decimal

from bn_strategy_platform.core.models import Candle, Position, Side, Signal
from bn_strategy_platform.persistence.memory import MemoryTradeStore
from bn_strategy_platform.strategies.registry import build_strategy


class RegistryTests(unittest.TestCase):
    def test_long_and_short_are_explicit_plugins(self) -> None:
        long_strategy = build_strategy("momentum-long", {})
        short_strategy = build_strategy("exhaustion-short", {})
        self.assertEqual(long_strategy.name, "momentum-long")
        self.assertEqual(long_strategy.settings.model, "continuation_long")
        self.assertEqual(short_strategy.name, "exhaustion-short")
        self.assertEqual(short_strategy.settings.model, "exhaustion_short")

        reversed_strategy = build_strategy("momentum-short", {})
        self.assertEqual(reversed_strategy.settings.model, "continuation_short")

    def test_copy_lead_is_an_event_plugin(self) -> None:
        strategy = build_strategy(
            "copy-lead",
            {"portfolio_id": "123", "margin_per_trade": "50", "event_not_before_ms": 1234},
            3,
        )
        self.assertEqual(strategy.name, "copy-lead")
        self.assertEqual(strategy.settings.leverage, 3)
        self.assertEqual(strategy.settings.margin_per_trade, 50)
        self.assertEqual(strategy.settings.event_not_before_ms, 1234)

    def test_cz_gainers_is_an_explicit_plugin(self) -> None:
        strategy = build_strategy("cz-gainers-long", {"momentum_4h_min": "0.06"}, 8)
        self.assertEqual(strategy.name, "cz-gainers-long")
        self.assertEqual(strategy.version, "2.3.0")
        self.assertEqual(strategy.settings.momentum_4h_min, Decimal("0.06"))

    def test_fast_stop_reversal_is_an_event_plugin(self) -> None:
        store = MemoryTradeStore()
        strategy = build_strategy(
            "bn-stra-fast-stop-reversal-1", {"min_volume_ratio": "1.8"}, 3, store
        )
        self.assertEqual(strategy.version, "1.3.0")
        self.assertEqual(strategy.settings.min_volume_ratio, Decimal("1.8"))
        self.assertIs(strategy.store, store)

    def test_momentum_ignition_long_is_an_explicit_plugin(self) -> None:
        strategy = build_strategy(
            "bn-stra-momentum-ignition-long-1",
            {"min_open_interest_change_15m": "0.04"},
        )
        self.assertEqual(strategy.version, "1.0.0")
        self.assertEqual(
            strategy.settings.min_open_interest_change_15m, Decimal("0.04"),
        )

    def test_legacy_names_preserve_database_ownership(self) -> None:
        main = build_strategy("bn-stra-top-gainers-1", {})
        pullback = build_strategy("bn-stra-top-gainers-exhaustion-short-1", {})
        copy = build_strategy("bn-stra-copy-lead-1", {"portfolio_id": "123"})
        self.assertEqual(main.name, "bn-stra-top-gainers-1")
        self.assertEqual(main.settings.model, "continuation_short")
        self.assertEqual(main.version, "3.12.0")
        self.assertEqual(pullback.name, "bn-stra-top-gainers-exhaustion-short-1")
        self.assertEqual(pullback.settings.model, "exhaustion_pullback_long")
        self.assertEqual(pullback.version, "4.10.0")
        self.assertEqual(copy.name, "bn-stra-copy-lead-1")
        self.assertEqual(copy.version, "2.4.0")

    def test_pullback_model_arms_before_confirming_long(self) -> None:
        strategy = build_strategy("bn-stra-top-gainers-exhaustion-short-1", {})
        source = Signal(
            strategy.name, "3.3.0", "TESTUSDT", Side.SHORT, Decimal("1"),
            Decimal("1.03"), Decimal("70"), "exhaustion", 1,
            {"model": "exhaustion_short", "momentum": "-0.03"},
        )
        candles = [
            Candle(index, Decimal("1"), Decimal("1.01"), Decimal("0.98"),
                   Decimal("0.99"), index + 1, Decimal("100"))
            for index in range(20)
        ]

        strategy._arm_entry(source, candles)

        self.assertEqual(strategy._entry_setups["TESTUSDT"].source, source)

    def test_top_gainers_scores_use_direction_specific_models_without_saturation(self) -> None:
        short = build_strategy("bn-stra-top-gainers-1", {})
        pullback = build_strategy("bn-stra-top-gainers-exhaustion-short-1", {})

        short_score, short_breakdown, short_model = short._score(
            Decimal("40"), Decimal("0.02"), Decimal("1.8"), Decimal("0.05"),
        )
        pullback_score, pullback_breakdown, pullback_model = pullback._score(
            Decimal("22"), Decimal("0.02"), Decimal("1.3"), Decimal("-0.02"),
        )

        self.assertEqual(short_model, "momentum-short-v4")
        self.assertEqual(pullback_model, "pullback-long-v2")
        self.assertLess(short_score, Decimal("100"))
        self.assertLess(pullback_score, Decimal("100"))
        self.assertIn("trend_strength", short_breakdown)
        self.assertIn("pullback_depth_quality", pullback_breakdown)

    def test_exhaustion_stop_enforces_configured_distance_range(self) -> None:
        strategy = build_strategy("exhaustion-short", {
            "min_stop_distance": "0.024", "max_stop_distance": "0.032",
        })

        close_structure = strategy._exhaustion_stop(
            Decimal("1"), Decimal("1.005"), Decimal("0.032"),
        )
        distant_structure = strategy._exhaustion_stop(
            Decimal("1"), Decimal("1.05"), Decimal("0.032"),
        )

        self.assertEqual(close_structure, Decimal("1.024"))
        self.assertEqual(distant_structure, Decimal("1.032"))

    def test_short_risk_management_reduces_risk_takes_half_and_trails(self) -> None:
        strategy = build_strategy("momentum-short", {}, 3)
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.SHORT,
            Decimal("1"), Decimal("600"), Decimal("600"), Decimal("200"), 3,
            Decimal("1.03"), 1, Decimal("1"), initial_stop_price=Decimal("1.03"),
        )

        position.best_price = Decimal("0.982")
        reduced = list(strategy.manage(position, [], Decimal("0.982")))
        self.assertEqual(reduced[0].reason, "risk_reduction")
        self.assertEqual(reduced[0].stop_price, Decimal("1.0075"))

        position.best_price = Decimal("0.97")
        protected = list(strategy.manage(position, [], Decimal("0.98")))
        self.assertEqual(protected[0].reason, "breakeven")
        self.assertEqual(protected[0].stop_price, Decimal("0.994"))

        position.best_price = Decimal("0.94")
        first_target = list(strategy.manage(position, [], Decimal("0.94")))
        self.assertEqual(first_target[0].reason, "take_profit_1")
        self.assertEqual(first_target[0].quantity, Decimal("300.0"))
        self.assertEqual(first_target[0].tier, 1)

        position.remaining_quantity = Decimal("300")
        position.partial_tiers_done = (1,)
        position.best_price = Decimal("0.90")
        trailing = list(strategy.manage(position, [], Decimal("0.92")))
        self.assertEqual(trailing[0].reason, "trailing")
        self.assertEqual(trailing[0].stop_price, Decimal("0.93"))

    def test_partial_runner_immediately_locks_one_r(self) -> None:
        strategy = build_strategy("bn-stra-top-gainers-exhaustion-short-1", {}, 3)
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("600"), Decimal("360"), Decimal("200"), 3,
            Decimal("1.0225"), 1, Decimal("1.06"), initial_stop_price=Decimal("0.97"),
            partial_tiers_done=(1,),
        )

        actions = list(strategy.manage(position, [], Decimal("1.06")))

        self.assertEqual(actions[0].reason, "post_tp1_lock")
        self.assertEqual(actions[0].stop_price, Decimal("1.03"))

    def test_long_first_target_closes_thirty_percent(self) -> None:
        strategy = build_strategy("bn-stra-top-gainers-exhaustion-short-1", {}, 3)
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("600"), Decimal("600"), Decimal("200"), 3,
            Decimal("0.97"), 1, Decimal("1.06"), initial_stop_price=Decimal("0.97"),
        )

        first_target = list(strategy.manage(position, [], Decimal("1.06")))

        self.assertEqual(first_target[0].reason, "take_profit_1")
        self.assertEqual(first_target[0].quantity, Decimal("180.0"))

    def test_pullback_long_adds_once_after_protected_higher_low_breakout(self) -> None:
        strategy = build_strategy(
            "bn-stra-top-gainers-exhaustion-short-1",
            {"scale_in_enabled": True, "scale_in_margin_usdt": "75"}, 3,
        )
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("450"), Decimal("450"), Decimal("150"), 3,
            Decimal("0.97"), 1, Decimal("1.035"),
            initial_stop_price=Decimal("0.97"),
        )
        candles = [
            Candle(1, Decimal("1.01"), Decimal("1.025"), Decimal("1.005"),
                   Decimal("1.02"), 2, Decimal("100")),
            Candle(3, Decimal("1.02"), Decimal("1.03"), Decimal("1.01"),
                   Decimal("1.025"), 4, Decimal("100")),
            Candle(5, Decimal("1.025"), Decimal("1.04"), Decimal("1.015"),
                   Decimal("1.035"), 6, Decimal("120")),
        ]

        actions = list(strategy.manage(position, candles, Decimal("1.035")))

        self.assertEqual([item.kind for item in actions], ["move_stop", "add"])
        self.assertEqual(actions[0].reason, "breakeven")
        self.assertEqual(actions[1].margin, Decimal("75"))

        position.scale_in_done = True
        self.assertNotIn("add", [
            item.kind for item in strategy.manage(position, candles, Decimal("1.035"))
        ])

    def test_runner_exits_when_price_has_already_crossed_new_trailing_stop(self) -> None:
        strategy = build_strategy("bn-stra-top-gainers-exhaustion-short-1", {}, 3)
        position = Position(
            "trade", strategy.name, strategy.version, "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("600"), Decimal("360"), Decimal("200"), 3,
            Decimal("1.0015"), 1, Decimal("1.12"),
            initial_stop_price=Decimal("0.97"), partial_tiers_done=(1,),
        )

        actions = list(strategy.manage(position, [], Decimal("1.05")))

        self.assertEqual(actions[0].kind, "close")
        self.assertEqual(actions[0].reason, "trailing_stop")
