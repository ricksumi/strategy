import unittest
from decimal import Decimal

from bn_strategy_platform.core.models import OrderFill, Position, Side
from bn_strategy_platform.exchanges.binance import BinanceError, BinanceUsdM


class BinanceFillTests(unittest.TestCase):
    def test_futures_flow_combines_interest_taker_funding_and_positioning(self) -> None:
        class FlowClient(BinanceUsdM):
            def _request(self, method, path, params=None, signed=False):
                if path.endswith("openInterestHist"):
                    return [
                        {"sumOpenInterestValue": "100"},
                        {"sumOpenInterestValue": "104"},
                    ]
                if path.endswith("takerlongshortRatio"):
                    return [
                        {"buyVol": "60", "sellVol": "50"},
                        {"buyVol": "75", "sellVol": "50"},
                    ]
                if path.endswith("topLongShortPositionRatio"):
                    return [
                        {"longShortRatio": "1.2"},
                        {"longShortRatio": "1.3"},
                    ]
                if path.endswith("premiumIndex"):
                    return {"lastFundingRate": "0.0001"}
                raise AssertionError(path)

        flow = FlowClient().futures_flow("TESTUSDT")

        self.assertEqual(flow["open_interest_change_15m"], Decimal("0.04"))
        self.assertEqual(flow["taker_buy_sell_ratio_15m"], Decimal("1.35"))
        self.assertEqual(flow["funding_rate"], Decimal("0.0001"))
        self.assertEqual(flow["top_position_ratio"], Decimal("1.3"))
        self.assertEqual(flow["top_position_ratio_change"], Decimal("0.1"))

    def test_instruments_include_crypto_and_tradifi_perpetuals(self) -> None:
        class ExchangeInfoClient(BinanceUsdM):
            def _request(self, *args, **kwargs):
                def symbol(name, contract_type):
                    return {
                        "symbol": name, "status": "TRADING", "quoteAsset": "USDT",
                        "contractType": contract_type,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }

                return {"symbols": [
                    symbol("BTCUSDT", "PERPETUAL"),
                    symbol("CLUSDT", "TRADIFI_PERPETUAL"),
                    symbol("BTCUSDT_260925", "CURRENT_QUARTER"),
                ]}

        instruments = ExchangeInfoClient().instruments()

        self.assertEqual(set(instruments), {"BTCUSDT", "CLUSDT"})

    def test_zero_average_uses_cumulative_quote(self) -> None:
        client = BinanceUsdM()
        row = {"avgPrice": "0", "executedQty": "25", "cumQuote": "10", "orderId": 1}
        self.assertEqual(client._average_price(row, "TESTUSDT", Decimal("0.5")), Decimal("0.4"))

    def test_zero_average_has_safe_price_fallback(self) -> None:
        class OfflineClient(BinanceUsdM):
            def _request(self, *args, **kwargs):
                raise BinanceError("offline")

        client = OfflineClient()
        row = {"avgPrice": "0", "executedQty": "25", "cumQuote": "0", "orderId": 1}
        self.assertEqual(client._average_price(row, "TESTUSDT", Decimal("0.5")), Decimal("0.5"))

    def test_client_order_id_identifies_strategy_and_is_binance_safe(self) -> None:
        client_id = BinanceUsdM._client_order_id(
            "bn-stra-top-gainers-exhaustion-short-1", "s",
        )

        self.assertTrue(client_id.startswith("bsp_pullback_s_"))
        self.assertLessEqual(len(client_id), 36)
        self.assertTrue(client_id.isascii())
        self.assertRegex(client_id, r"^[.A-Z:/a-z0-9_-]{1,36}$")

    def test_partial_reduce_only_ioc_uses_market_for_the_remainder(self) -> None:
        class PartialExitClient(BinanceUsdM):
            def book(self, symbol):
                return {"bid": Decimal("100"), "ask": Decimal("100.1")}

            def _ioc(self, *args, **kwargs):
                return OrderFill("limit", "TESTUSDT", "SELL", Decimal("4"),
                                 Decimal("99"), "EXPIRED", {"type": "LIMIT"})

            def _market_reduce(self, position, quantity, action):
                self.market_quantity = quantity
                return OrderFill("market", position.symbol, "SELL", quantity,
                                 Decimal("98"), "FILLED", {"type": "MARKET"})

        client = PartialExitClient()
        position = Position(
            "trade", "strategy", "1.0.0", "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("10"), Decimal("20"), 5,
            Decimal("98"), 1, Decimal("100"),
        )

        fill = client.reduce(position, Decimal("10"), Decimal("0.002"))

        self.assertEqual(client.market_quantity, Decimal("6"))
        self.assertEqual(fill.quantity, Decimal("10"))
        self.assertEqual(fill.average_price, Decimal("98.4"))
        self.assertIn("market_fallback", fill.raw)

    def test_zero_fill_reduce_only_ioc_uses_market_for_the_full_quantity(self) -> None:
        class EmptyExitClient(BinanceUsdM):
            def book(self, symbol):
                return {"bid": Decimal("100"), "ask": Decimal("100.1")}

            def _ioc(self, *args, **kwargs):
                return OrderFill("limit", "TESTUSDT", "SELL", Decimal("0"),
                                 Decimal("99"), "EXPIRED")

            def _market_reduce(self, position, quantity, action):
                self.market_quantity = quantity
                return OrderFill("market", position.symbol, "SELL", quantity,
                                 Decimal("98"), "FILLED")

        client = EmptyExitClient()
        position = Position(
            "trade", "strategy", "1.0.0", "TESTUSDT", Side.LONG,
            Decimal("100"), Decimal("10"), Decimal("10"), Decimal("20"), 5,
            Decimal("98"), 1, Decimal("100"),
        )

        fill = client.reduce(position, Decimal("10"), Decimal("0.002"))

        self.assertEqual(client.market_quantity, Decimal("10"))
        self.assertEqual(fill.quantity, Decimal("10"))
        self.assertEqual(fill.average_price, Decimal("98"))
