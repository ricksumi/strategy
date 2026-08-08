import os
import unittest
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from bn_stra_high_risk_1 import (
    BinanceClient,
    BnStraHighRisk1,
    BotConfig,
    Candle,
    PositionState,
    SymbolRules,
    adx_values,
    atr_percent,
    breakeven_stop_price,
    ema_values,
    improve_stop,
    initial_stop_price,
    client_order_id,
    load_env_file,
    margin_roi,
    pullback_confirmation_price,
    pullback_entry_allowed,
    pullback_reversal_confirmed,
    pullback_target_price,
    profit_trigger_price,
    round_stop_price,
    strategy_signal,
)


class BnStraHighRisk1Tests(unittest.TestCase):
    def test_initial_stop_price(self):
        self.assertEqual(initial_stop_price(Decimal("100"), "long", Decimal("0.10"), 5), Decimal("98.00"))
        self.assertEqual(initial_stop_price(Decimal("100"), "short", Decimal("0.10"), 5), Decimal("102.00"))

    def test_profit_trigger_price(self):
        self.assertEqual(profit_trigger_price(Decimal("100"), "long", Decimal("0.20"), 5), Decimal("104.00"))
        self.assertEqual(profit_trigger_price(Decimal("100"), "short", Decimal("0.20"), 5), Decimal("96.00"))

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

    def test_client_order_id_is_short_enough(self):
        self.assertLessEqual(len(client_order_id("SNDKUSDT", "managed")), 36)

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

    def test_margin_is_split_by_symbol_count(self):
        config = test_config(False, symbols=("BTCUSDT", "SOLUSDT", "ETHUSDT"))
        bot = BnStraHighRisk1(config, FakeClient([]))

        self.assertEqual(bot.margin_per_symbol(Decimal("1500")), Decimal("500"))

    def test_config_accepts_5m_interval(self):
        config = test_config(False, interval="5m")

        config.validate()

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

    def test_replace_stop_order_cancels_algo_order_with_symbol(self):
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

        self.assertEqual(client.calls[0], ("DELETE", "/fapi/v1/algoOrder", {"symbol": "ETHUSDT", "algoId": 99}))
        self.assertEqual(client.calls[1][1], "/fapi/v1/algoOrder")
        self.assertEqual(state.stop_order_id, 123)

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

    def test_stop_loss_count_uses_conservative_fallback(self):
        client = RecordingClient(algo_order_response={"algoStatus": "NEW"})
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
        if method == "DELETE" and path == "/fapi/v1/algoOrder":
            return {}
        if method == "GET" and path == "/fapi/v1/algoOrder":
            return self.algo_order_response
        if method == "DELETE" and path == "/fapi/v1/allOpenOrders":
            return {}
        if method == "DELETE" and path == "/fapi/v1/algoOpenOrders":
            return {}
        raise AssertionError((method, path, params))


def test_config(dry_run=True, symbols=("ETHUSDT",), interval="5m"):
    return BotConfig(
        symbols=symbols,
        strategy_mode="core",
        interval=interval,
        leverage=5,
        allocation_fraction=Decimal("0.2"),
        stop_loss_roi=Decimal("0.10"),
        breakeven_roi=Decimal("0.10"),
        fee_rate=Decimal("0.0004"),
        trailing_activation_roi=Decimal("0.20"),
        trailing_callback=Decimal("0.015"),
        pullback_entry_pct=Decimal("0"),
        pullback_confirm_pct=Decimal("0"),
        pullback_signal_wait_seconds=0,
        ema_fast=20,
        ema_slow=60,
        adx_period=14,
        adx_min=Decimal("20"),
        atr_period=14,
        atr_min_pct=Decimal("0"),
        atr_max_pct=Decimal("0"),
        daily_stop_limit=3,
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
