import os
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bn_stra_high_risk_1 import (
    BinanceClient,
    BnStraHighRisk1,
    BotConfig,
    Candle,
    PositionState,
    SymbolRules,
    adx_values,
    atr_percent,
    atr_size_factor,
    breakeven_stop_price,
    capped_atr_stop_distance,
    ema_values,
    improve_stop,
    initial_stop_price,
    client_order_id,
    contract_position_allows,
    dynamic_confirm_pct,
    entry_signal_price_allows,
    entry_near_ema,
    load_env_file,
    margin_roi,
    pullback_limit_exceeded,
    pullback_confirmation_price,
    pullback_entry_allowed,
    pullback_reversal_confirmed,
    pullback_target_price,
    profit_trigger_price,
    round_stop_price,
    select_active_universe,
    strategy_signal,
    stop_improved_by,
    trailing_callback_for_roi,
)


class BnStraHighRisk1Tests(unittest.TestCase):
    def test_active_universe_filters_and_ranks_markets(self):
        now_ms = 2_000_000_000_000
        old = now_ms - 60 * 86_400_000
        exchange_symbols = [
            {"symbol": "FASTUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "onboardDate": old},
            {"symbol": "DEEPUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "onboardDate": old},
            {"symbol": "WIDEUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "onboardDate": old},
            {"symbol": "NEWUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "onboardDate": now_ms - 5 * 86_400_000},
            {"symbol": "TRADUSDT", "status": "TRADING", "contractType": "TRADIFI_PERPETUAL", "quoteAsset": "USDT", "onboardDate": old},
            {"symbol": "DELIVERYUSDT", "status": "TRADING", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT", "onboardDate": old},
        ]
        tickers = [
            {"symbol": "FASTUSDT", "quoteVolume": "50000000", "highPrice": "120", "lowPrice": "100"},
            {"symbol": "DEEPUSDT", "quoteVolume": "100000000", "highPrice": "110", "lowPrice": "100"},
            {"symbol": "WIDEUSDT", "quoteVolume": "100000000", "highPrice": "120", "lowPrice": "100"},
            {"symbol": "NEWUSDT", "quoteVolume": "100000000", "highPrice": "130", "lowPrice": "100"},
            {"symbol": "TRADUSDT", "quoteVolume": "60000000", "highPrice": "115", "lowPrice": "100"},
        ]
        books = [
            {"symbol": "FASTUSDT", "bidPrice": "100", "askPrice": "100.10"},
            {"symbol": "DEEPUSDT", "bidPrice": "100", "askPrice": "100.05"},
            {"symbol": "WIDEUSDT", "bidPrice": "100", "askPrice": "101"},
            {"symbol": "NEWUSDT", "bidPrice": "100", "askPrice": "100.05"},
            {"symbol": "TRADUSDT", "bidPrice": "100", "askPrice": "100.05"},
        ]

        selected, stats = select_active_universe(
            exchange_symbols,
            tickers,
            books,
            2,
            Decimal("20000000"),
            Decimal("0.0015"),
            30,
            Decimal("0.03"),
            now_ms,
        )

        self.assertEqual(selected, ["DEEPUSDT", "FASTUSDT"])
        self.assertEqual(stats, {"candidates": 5, "eligible": 3})

    def test_initial_stop_price(self):
        self.assertEqual(initial_stop_price(Decimal("100"), "long", Decimal("0.10"), 5), Decimal("98.00"))
        self.assertEqual(initial_stop_price(Decimal("100"), "short", Decimal("0.10"), 5), Decimal("102.00"))

    def test_profit_trigger_price(self):
        self.assertEqual(profit_trigger_price(Decimal("100"), "long", Decimal("0.20"), 5), Decimal("104.00"))
        self.assertEqual(profit_trigger_price(Decimal("100"), "short", Decimal("0.20"), 5), Decimal("96.00"))

    def test_trailing_callback_tightens_by_best_roi(self):
        args = (
            Decimal("0.25"), Decimal("0.04"), Decimal("0.50"), Decimal("0.03"), Decimal("0.80"), Decimal("0.02")
        )
        self.assertIsNone(trailing_callback_for_roi(Decimal("0.24"), *args))
        self.assertEqual(trailing_callback_for_roi(Decimal("0.25"), *args), Decimal("0.04"))
        self.assertEqual(trailing_callback_for_roi(Decimal("0.50"), *args), Decimal("0.03"))
        self.assertEqual(trailing_callback_for_roi(Decimal("0.80"), *args), Decimal("0.02"))

    def test_breakeven_stop_price_includes_round_trip_fee_buffer(self):
        self.assertEqual(breakeven_stop_price(Decimal("100"), "long", Decimal("0.0004")), Decimal("100.0800"))
        self.assertEqual(breakeven_stop_price(Decimal("100"), "short", Decimal("0.0004")), Decimal("99.9200"))

    def test_margin_roi(self):
        self.assertEqual(margin_roi(Decimal("104"), Decimal("100"), "long", 5), Decimal("0.20"))
        self.assertEqual(margin_roi(Decimal("96"), Decimal("100"), "short", 5), (Decimal("100") / Decimal("96") - 1) * 5)

    def test_improve_stop(self):
        self.assertEqual(improve_stop(Decimal("98"), Decimal("100"), "long"), Decimal("100"))
        self.assertEqual(improve_stop(Decimal("102"), Decimal("100"), "short"), Decimal("100"))

    def test_round_stop_price(self):
        self.assertEqual(round_stop_price(Decimal("100.129"), Decimal("0.01"), "long"), Decimal("100.12"))
        self.assertEqual(round_stop_price(Decimal("100.121"), Decimal("0.01"), "short"), Decimal("100.13"))

    def test_stop_update_requires_minimum_improvement(self):
        self.assertFalse(stop_improved_by(Decimal("100.10"), Decimal("100"), "long", Decimal("0.002")))
        self.assertTrue(stop_improved_by(Decimal("100.20"), Decimal("100"), "long", Decimal("0.002")))
        self.assertFalse(stop_improved_by(Decimal("99.90"), Decimal("100"), "short", Decimal("0.002")))
        self.assertTrue(stop_improved_by(Decimal("99.80"), Decimal("100"), "short", Decimal("0.002")))

    def test_atr_size_factor_uses_volatility_tiers(self):
        args = (Decimal("0.03"), Decimal("0.04"), Decimal("0.70"), Decimal("0.40"))
        self.assertEqual(atr_size_factor(Decimal("0.03"), *args), Decimal("1"))
        self.assertEqual(atr_size_factor(Decimal("0.035"), *args), Decimal("0.70"))
        self.assertEqual(atr_size_factor(Decimal("0.05"), *args), Decimal("0.40"))

    def test_dynamic_confirmation_uses_larger_atr_threshold(self):
        self.assertEqual(dynamic_confirm_pct(Decimal("0.004"), Decimal("0.02"), Decimal("0.15")), Decimal("0.004"))
        self.assertEqual(dynamic_confirm_pct(Decimal("0.004"), Decimal("0.05"), Decimal("0.15")), Decimal("0.0075"))

    def test_atr_stop_distance_never_exceeds_configured_roi_cap(self):
        cap = Decimal("0.10") / Decimal("5")
        self.assertEqual(capped_atr_stop_distance(cap, Decimal("0.005"), Decimal("1.5")), Decimal("0.0075"))
        self.assertEqual(capped_atr_stop_distance(cap, Decimal("0.04"), Decimal("1.5")), cap)

    def test_contract_position_filter_blocks_squeeze_risk(self):
        args = (Decimal("0.65"), Decimal("1.20"), Decimal("1.55"), Decimal("0.83"))
        self.assertFalse(contract_position_allows("short", Decimal("0.60"), Decimal("1.40"), *args))
        self.assertFalse(contract_position_allows("long", Decimal("1.60"), Decimal("0.80"), *args))
        self.assertTrue(contract_position_allows("long", Decimal("0.60"), Decimal("1.40"), *args))
        self.assertTrue(contract_position_allows("short", Decimal("1.60"), Decimal("0.80"), *args))
        veto_args = (*args, Decimal("0.80"), Decimal("1.25"))
        self.assertFalse(contract_position_allows("long", Decimal("1.00"), Decimal("0.75"), *veto_args))
        self.assertFalse(contract_position_allows("short", Decimal("1.00"), Decimal("1.30"), *veto_args))

    def test_entry_distance_from_ema_is_limited_by_atr(self):
        candles = [
            Candle(i, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), i)
            for i in range(20)
        ]
        candles[-1] = Candle(19, Decimal("100"), Decimal("103"), Decimal("99"), Decimal("102"), 19)
        self.assertTrue(entry_near_ema(candles, 20, Decimal("0.02"), Decimal("1.5")))
        self.assertFalse(entry_near_ema(candles, 20, Decimal("0.005"), Decimal("1.5")))

    def test_pullback_entry_price(self):
        self.assertEqual(pullback_target_price(Decimal("100"), "long", Decimal("0.004")), Decimal("99.600"))
        self.assertEqual(pullback_target_price(Decimal("100"), "short", Decimal("0.004")), Decimal("100.400"))
        self.assertTrue(pullback_entry_allowed(Decimal("99.6"), "long", Decimal("100"), Decimal("0.004")))
        self.assertFalse(pullback_entry_allowed(Decimal("99.7"), "long", Decimal("100"), Decimal("0.004")))
        self.assertTrue(pullback_entry_allowed(Decimal("100.4"), "short", Decimal("100"), Decimal("0.004")))
        self.assertFalse(pullback_entry_allowed(Decimal("100.3"), "short", Decimal("100"), Decimal("0.004")))

    def test_pullback_reversal_confirmation(self):
        self.assertEqual(pullback_confirmation_price(Decimal("99.5"), "long", Decimal("0.002")), Decimal("99.6990"))
        self.assertTrue(pullback_reversal_confirmed(Decimal("99.7"), "long", Decimal("99.5"), Decimal("0.002")))
        self.assertFalse(pullback_reversal_confirmed(Decimal("99.6"), "long", Decimal("99.5"), Decimal("0.002")))
        self.assertEqual(pullback_confirmation_price(Decimal("100.5"), "short", Decimal("0.002")), Decimal("100.2990"))
        self.assertTrue(pullback_reversal_confirmed(Decimal("100.29"), "short", Decimal("100.5"), Decimal("0.002")))
        self.assertFalse(pullback_reversal_confirmed(Decimal("100.4"), "short", Decimal("100.5"), Decimal("0.002")))

    def test_pullback_atr_limit_blocks_adverse_overshoot(self):
        self.assertTrue(pullback_limit_exceeded(Decimal("94"), "long", Decimal("100"), Decimal("0.05")))
        self.assertFalse(pullback_limit_exceeded(Decimal("95"), "long", Decimal("100"), Decimal("0.05")))
        self.assertTrue(pullback_limit_exceeded(Decimal("106"), "short", Decimal("100"), Decimal("0.05")))
        self.assertFalse(pullback_limit_exceeded(Decimal("105"), "short", Decimal("100"), Decimal("0.05")))

    def test_pullback_overshoot_rejects_current_signal(self):
        config = replace(
            test_config(False),
            pullback_entry_pct=Decimal("0.004"),
            pullback_confirm_pct=Decimal("0.004"),
            pullback_signal_wait_seconds=300,
        )
        bot = BnStraHighRisk1(config, FakeClient([]))
        state = bot.states["ETHUSDT"]

        with patch.object(bot, "get_mark_price", return_value=Decimal("94")), patch(
            "bn_stra_high_risk_1.time.time", return_value=100
        ):
            ready = bot.pullback_entry_ready(
                state, "long", Decimal("100"), Decimal("0.004"), Decimal("0.05")
            )

        self.assertFalse(ready)
        self.assertEqual(state.rejected_signal_side, "long")
        self.assertEqual(state.rejected_signal_price, Decimal("100"))
        self.assertIsNone(state.pending_signal_side)

    def test_pullback_requires_closed_one_minute_signal_recross(self):
        config = replace(
            test_config(False),
            pullback_entry_pct=Decimal("0.004"),
            pullback_confirm_pct=Decimal("0.002"),
            pullback_signal_wait_seconds=300,
            require_signal_recross=True,
            signal_recross_wait_seconds=180,
        )
        bot = BnStraHighRisk1(config, FakeClient([]))
        state = bot.states["ETHUSDT"]
        state.pending_signal_side = "long"
        state.pending_signal_price = Decimal("100")
        state.pending_reversal_confirmed = True
        state.pending_recross_candle_open_after_ms = 120000
        eligible = Candle(
            open_time=120000,
            open=Decimal("99.8"),
            high=Decimal("100.3"),
            low=Decimal("99.7"),
            close=Decimal("100.1"),
            close_time=179999,
        )

        with patch.object(bot, "fetch_closed_candles", return_value=[]):
            self.assertFalse(bot.signal_recross_ready(state, "long", Decimal("100.2")))
        with patch.object(bot, "fetch_closed_candles", return_value=[eligible]):
            self.assertTrue(bot.signal_recross_ready(state, "long", Decimal("100.2")))
            self.assertFalse(bot.signal_recross_ready(state, "long", Decimal("99.9")))

    def test_entry_signal_distance_blocks_chasing(self):
        limit = Decimal("0.003")
        self.assertTrue(entry_signal_price_allows(Decimal("100.3"), Decimal("100"), "long", limit))
        self.assertFalse(entry_signal_price_allows(Decimal("100.31"), Decimal("100"), "long", limit))
        self.assertFalse(entry_signal_price_allows(Decimal("99.9"), Decimal("100"), "long", limit))
        self.assertTrue(entry_signal_price_allows(Decimal("99.7"), Decimal("100"), "short", limit))
        self.assertFalse(entry_signal_price_allows(Decimal("99.69"), Decimal("100"), "short", limit))

    def test_client_order_id_is_short_enough(self):
        self.assertLessEqual(len(client_order_id("SNDKUSDT", "managed")), 36)
        unicode_id = client_order_id("龙虾USDT", "initial")
        self.assertTrue(unicode_id.isascii())
        self.assertLessEqual(len(unicode_id), 36)

    def test_ema_and_adx_signal(self):
        candles = [
            Candle(
                open_time=i,
                open=Decimal(100 + i),
                high=Decimal(102 + i),
                low=Decimal(99 + i),
                close=Decimal(101 + i),
                close_time=i,
            )
            for i in range(100)
        ]
        self.assertIsNotNone(ema_values([c.close for c in candles], 20)[-1])
        self.assertIsNotNone(adx_values(candles, 14)[-1])
        self.assertEqual(strategy_signal(candles, 20, 60, 14, Decimal("20")), "long")

    def test_atr_filter_blocks_too_quiet_or_too_volatile(self):
        quiet = [
            Candle(
                open_time=i,
                open=Decimal("100") + Decimal(i) / Decimal("100"),
                high=Decimal("100.05") + Decimal(i) / Decimal("100"),
                low=Decimal("99.95") + Decimal(i) / Decimal("100"),
                close=Decimal("100.03") + Decimal(i) / Decimal("100"),
                close_time=i,
            )
            for i in range(100)
        ]
        volatile = [
            Candle(
                open_time=i,
                open=Decimal(100 + i),
                high=Decimal(115 + i),
                low=Decimal(90 + i),
                close=Decimal(101 + i),
                close_time=i,
            )
            for i in range(100)
        ]

        self.assertLess(atr_percent(quiet, 14), Decimal("0.005"))
        self.assertEqual(strategy_signal(quiet, 20, 60, 14, Decimal("20"), 14, Decimal("0.005"), Decimal("0.04")), "none")
        self.assertGreater(atr_percent(volatile, 14), Decimal("0.04"))
        self.assertEqual(strategy_signal(volatile, 20, 60, 14, Decimal("20"), 14, Decimal("0.005"), Decimal("0.04")), "none")

    def test_get_position_accepts_dict_response(self):
        bot = BnStraHighRisk1(test_config(False), FakeClient({"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0"}))

        position = bot.get_position("ETHUSDT")

        self.assertEqual(position["positionAmt"], "0")

    def test_get_position_empty_symbol_fallback(self):
        bot = BnStraHighRisk1(test_config(False), FakeClient([]))

        position = bot.get_position("ETHUSDT")

        self.assertEqual(position["positionAmt"], "0")

    def test_margin_per_trade_is_fixed(self):
        config = test_config(False, symbols=("BTCUSDT", "SOLUSDT", "ETHUSDT"))
        bot = BnStraHighRisk1(config, FakeClient([]))

        self.assertEqual(bot.margin_per_symbol(Decimal("1500")), Decimal("200"))

    def test_paper_only_mode_hard_blocks_real_entry(self):
        client = EntryExecutionClient(Decimal("10"))
        bot = BnStraHighRisk1(
            replace(test_config(False), paper_trading_only=True),
            client,
        )

        bot.open_position("ETHUSDT", "long")

        self.assertEqual(client.stop_quantities, [])

    def test_insufficient_margin_notifies_without_placing_order(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                client = InsufficientMarginClient()
                bot = BnStraHighRisk1(test_config(False), client)
                notifier = RecordingNotifier()
                bot.notifier = notifier
                bot.open_position("ETHUSDT", "long")

            self.assertEqual(client.market_orders, 0)
            self.assertEqual(len(notifier.messages), 1)
            self.assertIn("[INSUFFICIENT MARGIN] ETHUSDT LONG", notifier.messages[0])

    def test_non_entry_managed_symbol_does_not_open_after_close(self):
        with TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ", {"BN_STRA_STATE_FILE": str(Path(tmpdir) / "state.json")}
        ):
            bot = BnStraHighRisk1(test_config(False), FakeClient([]))
            bot.states["OLDUSDT"] = PositionState(symbol="OLDUSDT")
            bot.managed_symbols.append("OLDUSDT")

            with patch.object(bot, "get_position", return_value={"positionAmt": "0"}), patch.object(
                bot, "fetch_candles"
            ) as fetch_candles:
                bot.tick_symbol("OLDUSDT")

        fetch_candles.assert_not_called()

    def test_config_accepts_5m_interval(self):
        config = test_config(False, interval="5m")

        config.validate()

    def test_fetch_candles_excludes_current_unclosed_kline(self):
        rows = [
            [0, "100", "101", "99", "100.5", "0", 999],
            [1000, "100.5", "102", "100", "101", "0", 1999],
        ]
        bot = BnStraHighRisk1(test_config(False), KlineClient(rows))

        with patch("bn_stra_high_risk_1.time.time", return_value=1.5):
            candles = bot.fetch_candles("ETHUSDT")

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].close_time, 999)

    def test_stop_order_uses_algo_endpoint(self):
        client = RecordingClient()
        bot = BnStraHighRisk1(test_config(False), client)

        result = bot.place_stop_order("ETHUSDT", "long", Decimal("0.25"), Decimal("2500"), "initial")

        self.assertEqual(result["algoId"], 123)
        self.assertEqual(client.calls[-1][0], "POST")
        self.assertEqual(client.calls[-1][1], "/fapi/v1/algoOrder")
        self.assertEqual(client.calls[-1][2]["algoType"], "CONDITIONAL")
        self.assertEqual(client.calls[-1][2]["type"], "STOP_MARKET")
        self.assertEqual(client.calls[-1][2]["triggerPrice"], "2500")
        self.assertEqual(client.calls[-1][2]["reduceOnly"], "true")
        self.assertIn("clientAlgoId", client.calls[-1][2])

    def test_entry_order_uses_ioc_limit_with_slippage_cap(self):
        client = RecordingClient()
        bot = BnStraHighRisk1(test_config(False), client)
        bot.rules = {
            "ETHUSDT": SymbolRules(
                tick_size=Decimal("0.01"), step_size=Decimal("0.001"), min_qty=Decimal("0")
            )
        }

        bot.place_entry_order("ETHUSDT", "BUY", Decimal("1"), Decimal("100"), "long")
        buy = client.calls[-1][2]
        bot.place_entry_order("ETHUSDT", "SELL", Decimal("1"), Decimal("100"), "short")
        sell = client.calls[-1][2]

        self.assertEqual(buy["type"], "LIMIT")
        self.assertEqual(buy["timeInForce"], "IOC")
        self.assertEqual(buy["price"], "100.2")
        self.assertEqual(sell["price"], "99.8")

    def test_zero_fill_ioc_entry_does_not_create_position_or_stop(self):
        with TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ", {"BN_STRA_STATE_FILE": str(Path(tmpdir) / "state.json")}
        ):
            client = EntryExecutionClient(Decimal("0"))
            bot = BnStraHighRisk1(test_config(False), client)
            bot.rules = {
                "ETHUSDT": SymbolRules(
                    tick_size=Decimal("0.01"), step_size=Decimal("1"), min_qty=Decimal("1")
                )
            }
            with patch.object(bot, "get_mark_price", return_value=Decimal("100")):
                bot.open_position("ETHUSDT", "long")

        self.assertEqual(bot.states["ETHUSDT"].quantity, Decimal("0"))
        self.assertEqual(client.stop_quantities, [])

    def test_partial_ioc_entry_protects_only_executed_quantity(self):
        with TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ", {"BN_STRA_STATE_FILE": str(Path(tmpdir) / "state.json")}
        ):
            client = EntryExecutionClient(Decimal("5"))
            bot = BnStraHighRisk1(test_config(False), client)
            bot.rules = {
                "ETHUSDT": SymbolRules(
                    tick_size=Decimal("0.01"), step_size=Decimal("1"), min_qty=Decimal("1")
                )
            }
            with patch.object(bot, "get_mark_price", return_value=Decimal("100")):
                bot.open_position("ETHUSDT", "long")

        state = bot.states["ETHUSDT"]
        self.assertEqual(state.quantity, Decimal("5"))
        self.assertEqual(state.initial_quantity, Decimal("5"))
        self.assertEqual(state.initial_margin, Decimal("100.2"))
        self.assertEqual(client.stop_quantities, ["5"])

    def test_profit_lock_trigger_closes_thirty_percent_and_protects_remainder(self):
        with TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ", {"BN_STRA_STATE_FILE": str(Path(tmpdir) / "state.json")}
        ):
            bot = BnStraHighRisk1(
                replace(test_config(True), profit_lock_roi=Decimal("0.05")), FakeClient([])
            )
            bot.rules = {
                "ETHUSDT": SymbolRules(
                    tick_size=Decimal("0.01"), step_size=Decimal("1"), min_qty=Decimal("1")
                )
            }
            state = bot.states["ETHUSDT"]
            state.side = "long"
            state.entry_price = Decimal("100")
            state.quantity = Decimal("10")
            state.initial_quantity = Decimal("10")
            state.initial_margin = Decimal("200")
            state.best_price = Decimal("100")
            state.stop_price = Decimal("98")
            state.stop_order_id = 99
            state.stop_client_id = "initial"
            state.opened_at_ms = 1
            notifier = RecordingNotifier()
            bot.notifier = notifier

            bot.manage_open_position(state, Decimal("102.4"))

        self.assertEqual(state.quantity, Decimal("7"))
        self.assertTrue(state.partial_take_1_done)
        self.assertTrue(state.breakeven_done)
        self.assertEqual(state.stop_price, Decimal("101"))
        self.assertEqual(notifier.messages, [])

    def test_paper_position_tracks_partial_profit_and_sends_only_open_and_close(self):
        with TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ", {"BN_STRA_STATE_FILE": str(Path(tmpdir) / "state.json")}
        ):
            config = replace(
                test_config(False),
                global_daily_stop_limit=5,
                paper_signals_after_global_stop=True,
                profit_lock_roi=Decimal("0.05"),
            )
            bot = BnStraHighRisk1(config, FakeClient([]))
            bot.rules = {
                "ETHUSDT": SymbolRules(
                    tick_size=Decimal("0.01"), step_size=Decimal("1"), min_qty=Decimal("1")
                )
            }
            notifier = RecordingNotifier()
            bot.notifier = notifier
            bot.get_mark_price = lambda symbol: Decimal("100")

            bot.open_paper_position("ETHUSDT", "long", Decimal("0.02"))
            self.assertEqual(len(notifier.messages), 1)
            self.assertIn("[PAPER OPEN - NO REAL ORDER]", notifier.messages[0])
            self.assertEqual(bot.paper_positions["ETHUSDT"].quantity, Decimal("10"))

            bot.manage_paper_position(bot.paper_positions["ETHUSDT"], Decimal("102.4"))
            self.assertEqual(len(notifier.messages), 1)
            self.assertEqual(bot.paper_positions["ETHUSDT"].remaining_quantity, Decimal("7"))
            self.assertEqual(bot.paper_positions["ETHUSDT"].stop_price, Decimal("101"))

            bot.manage_paper_position(bot.paper_positions["ETHUSDT"], Decimal("100.5"))
            self.assertNotIn("ETHUSDT", bot.paper_positions)
            self.assertEqual(len(notifier.messages), 2)
            self.assertIn("[PAPER CLOSED - NO REAL ORDER]", notifier.messages[1])
            self.assertIn("Estimated net PnL: +13.3943 USDT", notifier.messages[1])

    def test_paper_position_survives_restart(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            config = replace(test_config(False), paper_signals_after_global_stop=True)
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                first = BnStraHighRisk1(config, FakeClient([]))
                first.rules = {
                    "ETHUSDT": SymbolRules(
                        tick_size=Decimal("0.01"), step_size=Decimal("1"), min_qty=Decimal("1")
                    )
                }
                first.get_mark_price = lambda symbol: Decimal("100")
                first.open_paper_position("ETHUSDT", "short", Decimal("0.02"))
                second = BnStraHighRisk1(config, FakeClient([]))

            restored = second.paper_positions["ETHUSDT"]
            self.assertEqual(restored.side, "short")
            self.assertEqual(restored.entry_price, Decimal("100"))
            self.assertEqual(restored.stop_price, Decimal("102"))

    def test_replace_stop_order_places_new_protection_before_canceling_old(self):
        client = RecordingClient()
        bot = BnStraHighRisk1(test_config(False), client)
        bot.rules = {"ETHUSDT": SymbolRules(tick_size=Decimal("0.01"), step_size=Decimal("0.001"), min_qty=Decimal("0"))}
        state = PositionState(
            symbol="ETHUSDT",
            side="long",
            quantity=Decimal("0.25"),
            stop_order_id=99,
            stop_client_id="old",
        )

        bot.replace_stop_order(state, Decimal("2510"))

        self.assertEqual(client.calls[0][0:2], ("POST", "/fapi/v1/algoOrder"))
        self.assertEqual(client.calls[1], ("DELETE", "/fapi/v1/algoOrder", {"symbol": "ETHUSDT", "algoId": 99}))
        self.assertEqual(state.stop_order_id, 123)

    def test_replace_stop_order_keeps_old_protection_when_new_order_fails(self):
        client = FailingStopClient()
        bot = BnStraHighRisk1(test_config(False), client)
        state = PositionState(
            symbol="ETHUSDT",
            side="long",
            quantity=Decimal("0.25"),
            stop_price=Decimal("2500"),
            stop_order_id=99,
            stop_client_id="old",
        )

        with self.assertRaises(RuntimeError):
            bot.replace_stop_order(state, Decimal("2510"))

        self.assertEqual(client.calls, [("POST", "/fapi/v1/algoOrder")])
        self.assertEqual(state.stop_order_id, 99)
        self.assertEqual(state.stop_price, Decimal("2500"))

    def test_zero_order_avg_price_is_resolved_from_trade_fills(self):
        client = FillPriceClient()
        bot = BnStraHighRisk1(test_config(False), client)

        price = bot.resolve_order_fill_price(
            "BMTUSDT", {"orderId": 42, "avgPrice": "0"}, Decimal("0.03430")
        )

        self.assertEqual(price, Decimal("0.03435"))
        self.assertEqual(
            client.calls,
            [
                ("GET", "/fapi/v1/order", {"symbol": "BMTUSDT", "orderId": 42}),
                ("GET", "/fapi/v1/userTrades", {"symbol": "BMTUSDT", "orderId": 42, "limit": 1000}),
            ],
        )

    def test_trade_fills_are_queried_when_order_lookup_fails(self):
        client = FillPriceClient(order_lookup_fails=True)
        bot = BnStraHighRisk1(test_config(False), client)

        price = bot.resolve_order_fill_price(
            "龙虾USDT", {"orderId": 42, "avgPrice": "0"}, Decimal("0.02934")
        )

        self.assertEqual(price, Decimal("0.03435"))
        self.assertEqual(client.calls[-1][1], "/fapi/v1/userTrades")

    def test_was_stop_order_filled_queries_algo_order(self):
        client = RecordingClient(algo_order_response={"algoStatus": "FINISHED"})
        bot = BnStraHighRisk1(test_config(False), client)
        state = PositionState(symbol="ETHUSDT", stop_client_id="cid", stop_reason="stop_loss")

        self.assertTrue(bot.was_stop_order_filled(state))
        self.assertEqual(client.calls[-1], ("GET", "/fapi/v1/algoOrder", {"symbol": "ETHUSDT", "clientAlgoId": "cid"}))

    def test_cancel_algo_open_orders_uses_batch_cancel_endpoint(self):
        client = RecordingClient()
        bot = BnStraHighRisk1(test_config(False), client)

        bot.cancel_algo_open_orders("ETHUSDT")

        self.assertEqual(client.calls[-1], ("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": "ETHUSDT"}))

    def test_unconfirmed_stop_loss_does_not_increment_counts(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            client = RecordingClient(algo_order_response={"algoStatus": "NEW"})
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                bot = BnStraHighRisk1(test_config(False), client)
                state = PositionState(
                    symbol="ETHUSDT",
                    side="long",
                    entry_price=Decimal("100"),
                    quantity=Decimal("1"),
                    stop_price=Decimal("98"),
                    stop_client_id="cid",
                    stop_reason="stop_loss",
                )

                bot.on_position_closed(state)

        self.assertEqual(state.daily_stop_count, 0)
        self.assertEqual(bot.global_daily_stop_count, 0)

    def test_confirmed_initial_stop_increments_both_counts(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            client = RecordingClient(algo_order_response={"algoStatus": "FINISHED"})
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                bot = BnStraHighRisk1(test_config(False), client)
                state = PositionState(
                    symbol="ETHUSDT",
                    side="long",
                    entry_price=Decimal("100"),
                    quantity=Decimal("1"),
                    stop_price=Decimal("98"),
                    stop_client_id="cid",
                    stop_reason="stop_loss",
                )

                bot.on_position_closed(state)

        self.assertEqual(state.daily_stop_count, 1)
        self.assertEqual(bot.global_daily_stop_count, 1)

    def test_profit_stop_does_not_increment_daily_stop_count(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            client = RecordingClient(algo_order_response={"algoStatus": "FINISHED"})
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                bot = BnStraHighRisk1(test_config(False), client)
                state = PositionState(
                    symbol="ETHUSDT",
                    side="long",
                    entry_price=Decimal("100"),
                    quantity=Decimal("1"),
                    stop_price=Decimal("101"),
                    stop_client_id="cid",
                    stop_reason="trailing_stop",
                )
                bot.on_position_closed(state)

        self.assertEqual(state.daily_stop_count, 0)

    def test_load_env_file_sets_missing_values(self):
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                'BINANCE_API_KEY="key-from-file"\nBINANCE_API_SECRET=secret-from-file\n',
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                load_env_file(env_file)

                self.assertEqual(os.environ["BINANCE_API_KEY"], "key-from-file")
                self.assertEqual(os.environ["BINANCE_API_SECRET"], "secret-from-file")

    def test_load_env_file_does_not_override_existing_values(self):
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("BINANCE_API_KEY=file-key\n", encoding="utf-8")

            with patch.dict("os.environ", {"BINANCE_API_KEY": "shell-key"}, clear=True):
                load_env_file(env_file)

                self.assertEqual(os.environ["BINANCE_API_KEY"], "shell-key")

    def test_daily_stop_counts_survive_restart(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                first = BnStraHighRisk1(test_config(False), FakeClient([]))
                first.states["ETHUSDT"].daily_stop_count = 2
                first.save_runtime_state()

                restarted = BnStraHighRisk1(test_config(False), FakeClient([]))

            self.assertEqual(restarted.states["ETHUSDT"].daily_stop_count, 2)

    def test_old_state_reconstructs_global_daily_stop_count(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
            state_file.write_text(
                '{"day":"' + today + '","daily_stop_counts":{"ETHUSDT":2,"OLDUSDT":3}}\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                bot = BnStraHighRisk1(test_config(False), FakeClient([]))

            self.assertEqual(bot.global_daily_stop_count, 5)

    def test_global_daily_stop_limit_blocks_new_entries(self):
        with TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ", {"BN_STRA_STATE_FILE": str(Path(tmpdir) / "state.json")}
        ):
            config = replace(test_config(False), global_daily_stop_limit=5)
            bot = BnStraHighRisk1(config, FakeClient([]))
            bot.global_daily_stop_count = 6

            self.assertFalse(bot.entry_limits_allow(bot.states["ETHUSDT"]))

    def test_global_daily_stop_limit_allows_fifth_stop_count(self):
        config = replace(test_config(False), global_daily_stop_limit=5)
        bot = BnStraHighRisk1(config, FakeClient([]))
        bot.global_daily_stop_count = 5

        self.assertTrue(bot.entry_limits_allow(bot.states["ETHUSDT"]))

    def test_global_daily_stop_notification_is_sent_once(self):
        with TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ", {"BN_STRA_STATE_FILE": str(Path(tmpdir) / "state.json")}
        ):
            config = replace(test_config(False), global_daily_stop_limit=5)
            bot = BnStraHighRisk1(config, FakeClient([]))
            notifier = RecordingNotifier()
            bot.notifier = notifier
            bot.global_daily_stop_count = 6

            self.assertFalse(bot.entry_limits_allow(bot.states["ETHUSDT"]))
            self.assertFalse(bot.entry_limits_allow(bot.states["ETHUSDT"]))

        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("[GLOBAL DAILY STOP LIMIT EXCEEDED]", notifier.messages[0])

    def test_concurrent_position_limit_blocks_new_entries(self):
        config = replace(
            test_config(False, symbols=("ETHUSDT", "BTCUSDT", "SOLUSDT", "BNBUSDT")),
            max_concurrent_positions=3,
        )
        bot = BnStraHighRisk1(config, FakeClient([]))
        for symbol in ("ETHUSDT", "BTCUSDT", "SOLUSDT"):
            bot.states[symbol].quantity = Decimal("1")

        self.assertFalse(bot.entry_limits_allow(bot.states["BNBUSDT"]))

    def test_cooldown_survives_restart(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}), patch(
                "bn_stra_high_risk_1.time.time", return_value=1000
            ):
                first = BnStraHighRisk1(test_config(False), FakeClient([]))
                first.states["ETHUSDT"].cooldown_until = 1600
                first.save_runtime_state()
                restarted = BnStraHighRisk1(test_config(False), FakeClient([]))

            self.assertEqual(restarted.states["ETHUSDT"].cooldown_until, 1600)

    def test_stale_daily_stop_counts_are_ignored(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(
                '{"day":"2000-01-01","daily_stop_counts":{"ETHUSDT":2}}\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                bot = BnStraHighRisk1(test_config(False), FakeClient([]))

            self.assertEqual(bot.states["ETHUSDT"].daily_stop_count, 0)

    def test_active_trade_metadata_survives_restart_and_day_change(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(
                '{"day":"2000-01-01","daily_stop_counts":{},"active_trades":'
                '{"ETHUSDT":{"opened_at_ms":123456,"initial_margin":"250.5"}}}\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                bot = BnStraHighRisk1(test_config(False), FakeClient([]))

            self.assertEqual(bot.states["ETHUSDT"].opened_at_ms, 123456)
            self.assertEqual(bot.states["ETHUSDT"].initial_margin, Decimal("250.5"))

    def test_active_trade_metadata_restores_non_entry_symbol(self):
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(
                '{"day":"2000-01-01","daily_stop_counts":{},"active_trades":'
                '{"OLDUSDT":{"opened_at_ms":123456,"initial_margin":"100"}}}\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"BN_STRA_STATE_FILE": str(state_file)}):
                bot = BnStraHighRisk1(test_config(False), FakeClient([]))

            self.assertEqual(bot.states["OLDUSDT"].opened_at_ms, 123456)


class FakeClient(BinanceClient):
    def __init__(self, position_response):
        self.position_response = position_response

    def signed_request(self, method, path, params=None):
        if path == "/fapi/v2/positionRisk":
            return self.position_response
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        raise AssertionError(path)


class RecordingClient(BinanceClient):
    def __init__(self, algo_order_response=None):
        self.calls = []
        self.algo_order_response = algo_order_response or {"status": "NEW"}

    def signed_request(self, method, path, params=None):
        params = params or {}
        self.calls.append((method, path, params))
        if method == "POST" and path == "/fapi/v1/algoOrder":
            return {"algoId": 123, "clientAlgoId": params["clientAlgoId"]}
        if method == "POST" and path == "/fapi/v1/order":
            return {
                "orderId": 456,
                "avgPrice": params.get("price", "0"),
                "executedQty": params.get("quantity", "0"),
                "status": "FILLED",
            }
        if method == "DELETE" and path == "/fapi/v1/algoOrder":
            return {}
        if method == "GET" and path == "/fapi/v1/algoOrder":
            return self.algo_order_response
        if method == "DELETE" and path == "/fapi/v1/allOpenOrders":
            return {}
        if method == "DELETE" and path == "/fapi/v1/algoOpenOrders":
            return {}
        raise AssertionError((method, path, params))


class InsufficientMarginClient(BinanceClient):
    def __init__(self):
        self.market_orders = 0

    def signed_request(self, method, path, params=None):
        if path == "/fapi/v2/account":
            return {"totalMarginBalance": "1000", "availableBalance": "10"}
        if method == "POST" and path == "/fapi/v1/order":
            self.market_orders += 1
        raise AssertionError((method, path, params))


class EntryExecutionClient(BinanceClient):
    def __init__(self, executed_quantity):
        self.executed_quantity = executed_quantity
        self.stop_quantities = []

    def signed_request(self, method, path, params=None):
        params = params or {}
        if method == "GET" and path == "/fapi/v2/account":
            return {"totalMarginBalance": "1000", "availableBalance": "1000"}
        if method == "POST" and path == "/fapi/v1/order":
            return {
                "orderId": 456,
                "avgPrice": params["price"] if self.executed_quantity > 0 else "0",
                "executedQty": str(self.executed_quantity),
                "status": "EXPIRED" if self.executed_quantity < Decimal("10") else "FILLED",
            }
        if method == "POST" and path == "/fapi/v1/algoOrder":
            self.stop_quantities.append(params["quantity"])
            return {"algoId": 123, "clientAlgoId": params["clientAlgoId"]}
        raise AssertionError((method, path, params))


class FailingStopClient(BinanceClient):
    def __init__(self):
        self.calls = []

    def signed_request(self, method, path, params=None):
        self.calls.append((method, path))
        if method == "POST" and path == "/fapi/v1/algoOrder":
            raise RuntimeError("simulated new stop failure")
        raise AssertionError((method, path, params))


class KlineClient(BinanceClient):
    def __init__(self, rows):
        self.rows = rows

    def public_request(self, method, path, params=None):
        if method == "GET" and path == "/fapi/v1/klines":
            return self.rows
        raise AssertionError((method, path, params))


class FillPriceClient(BinanceClient):
    def __init__(self, order_lookup_fails=False):
        self.calls = []
        self.order_lookup_fails = order_lookup_fails

    def signed_request(self, method, path, params=None):
        params = params or {}
        self.calls.append((method, path, params))
        if method == "GET" and path == "/fapi/v1/order":
            if self.order_lookup_fails:
                raise RuntimeError("Order does not exist")
            return {"orderId": 42, "avgPrice": "0"}
        if method == "GET" and path == "/fapi/v1/userTrades":
            return [
                {"price": "0.03430", "qty": "100"},
                {"price": "0.03440", "qty": "100"},
            ]
        raise AssertionError((method, path, params))


class RecordingNotifier:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def test_config(dry_run=True, symbols=("ETHUSDT",), interval="5m"):
    return BotConfig(
        symbols=symbols,
        strategy_mode="core",
        interval=interval,
        leverage=5,
        allocation_fraction=Decimal("0.2"),
        margin_per_trade=Decimal("200"),
        entry_max_slippage_pct=Decimal("0.002"),
        stop_loss_roi=Decimal("0.10"),
        breakeven_roi=Decimal("0.10"),
        profit_lock_roi=Decimal("0.03"),
        fee_rate=Decimal("0.0004"),
        trailing_activation_roi=Decimal("0.20"),
        trailing_callback=Decimal("0.015"),
        trailing_tier_2_roi=Decimal("0.50"),
        trailing_tier_2_callback=Decimal("0.012"),
        trailing_tier_3_roi=Decimal("0.80"),
        trailing_tier_3_callback=Decimal("0.01"),
        partial_take_1_roi=Decimal("0.12"),
        partial_take_1_fraction=Decimal("0.30"),
        partial_take_2_roi=Decimal("0.40"),
        partial_take_2_fraction=Decimal("0.25"),
        pullback_entry_pct=Decimal("0"),
        pullback_confirm_pct=Decimal("0"),
        pullback_signal_wait_seconds=0,
        require_signal_recross=False,
        signal_recross_wait_seconds=180,
        signal_recross_confirm_polls=2,
        ema_fast=20,
        ema_slow=60,
        adx_period=14,
        adx_min=Decimal("20"),
        atr_period=14,
        atr_min_pct=Decimal("0"),
        atr_max_pct=Decimal("0"),
        atr_full_size_max_pct=Decimal("0"),
        atr_reduced_size_max_pct=Decimal("0"),
        atr_reduced_size_factor=Decimal("0.70"),
        atr_high_size_factor=Decimal("0.40"),
        atr_confirm_factor=Decimal("0.15"),
        atr_stop_multiplier=Decimal("1.5"),
        max_ema_atr_distance=Decimal("1.5"),
        max_pullback_atr_distance=Decimal("1.5"),
        contract_position_filter=True,
        crowded_short_global_max=Decimal("0.65"),
        crowded_short_top_min=Decimal("1.20"),
        crowded_long_global_min=Decimal("1.55"),
        crowded_long_top_max=Decimal("0.83"),
        top_long_veto_max=Decimal("0.80"),
        top_short_veto_min=Decimal("1.25"),
        stop_update_min_pct=Decimal("0.002"),
        daily_stop_limit=3,
        global_daily_stop_limit=0,
        max_concurrent_positions=0,
        cooldown_seconds=600,
        poll_seconds=15,
        kline_limit=200,
        working_type="MARK_PRICE",
        position_side="BOTH",
        dry_run=dry_run,
        testnet=False,
        recv_window=5000,
    )


if __name__ == "__main__":
    unittest.main()
