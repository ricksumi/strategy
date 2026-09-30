"""Binance USD-M Futures REST adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Mapping, Sequence
from uuid import uuid4

from ..core.models import (AccountSnapshot, Candle, Instrument, OrderFill, Position,
                           TradePlan, TradeSettlement)


class BinanceError(RuntimeError):
    pass


class BinanceUsdM:
    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = "https://fapi.binance.com") -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self._instruments: dict[str, Instrument] | None = None

    def _request(self, method: str, path: str, params: Mapping[str, Any] | None = None, signed: bool = False) -> Any:
        payload = {key: str(value) for key, value in (params or {}).items()}
        headers = {"User-Agent": "bn-strategy-platform/1.0"}
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceError("Binance API credentials are required for signed requests")
            payload.update({"timestamp": str(int(time.time() * 1000)), "recvWindow": "5000"})
            query = urllib.parse.urlencode(payload)
            payload["signature"] = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key
        query = urllib.parse.urlencode(payload)
        url = f"{self.base_url}{path}"
        data = None
        if method in {"GET", "DELETE"}:
            url = f"{url}?{query}" if query else url
        else:
            data = query.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise BinanceError(f"Binance API error {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise BinanceError(f"Binance request failed after retries: {last_error}") from last_error

    def assert_one_way_mode(self) -> None:
        result = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        if str(result.get("dualSidePosition", "false")).lower() == "true":
            raise BinanceError("Binance account must use One-way position mode")

    def instruments(self) -> Mapping[str, Instrument]:
        if self._instruments is not None:
            return self._instruments
        result = self._request("GET", "/fapi/v1/exchangeInfo")
        instruments: dict[str, Instrument] = {}
        for row in result.get("symbols", []):
            if row.get("status") != "TRADING" or row.get("quoteAsset") != "USDT":
                continue
            if row.get("contractType") not in {"PERPETUAL", "TRADIFI_PERPETUAL"}:
                continue
            filters = {item["filterType"]: item for item in row.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            notional_filter = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
            instruments[row["symbol"]] = Instrument(
                symbol=row["symbol"],
                tick_size=Decimal(str(price_filter.get("tickSize", "0.00000001"))),
                step_size=Decimal(str(lot_filter.get("stepSize", "0.00000001"))),
                min_quantity=Decimal(str(lot_filter.get("minQty", "0"))),
                min_notional=Decimal(str(notional_filter.get("notional", "5"))),
            )
        self._instruments = instruments
        return instruments

    def market_summaries(self) -> Sequence[Mapping[str, Any]]:
        tickers = self._request("GET", "/fapi/v1/ticker/24hr")
        books = {row["symbol"]: row for row in self._request("GET", "/fapi/v1/ticker/bookTicker")}
        result = []
        supported = self.instruments()
        for row in tickers:
            symbol = row.get("symbol")
            if symbol not in supported or symbol not in books:
                continue
            result.append({**row, **books[symbol]})
        return result

    def candles(self, symbol: str, interval: str, limit: int) -> Sequence[Candle]:
        rows = self._request("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit + 1})
        now = int(time.time() * 1000)
        return [
            Candle(int(row[0]), Decimal(row[1]), Decimal(row[2]), Decimal(row[3]), Decimal(row[4]),
                   int(row[6]), Decimal(row[7]))
            for row in rows if int(row[6]) < now
        ][-limit:]

    def live_candles(self, symbol: str, interval: str, limit: int) -> Sequence[Candle]:
        """Return a point-in-time kline snapshot, including the open final bar."""
        rows = self._request("GET", "/fapi/v1/klines",
                             {"symbol": symbol, "interval": interval, "limit": limit})
        return [
            Candle(int(row[0]), Decimal(row[1]), Decimal(row[2]), Decimal(row[3]),
                   Decimal(row[4]), int(row[6]), Decimal(row[7]))
            for row in rows
        ]

    def mark_price(self, symbol: str) -> Decimal:
        return Decimal(str(self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"]))

    def book(self, symbol: str) -> Mapping[str, Decimal]:
        row = self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return {"bid": Decimal(str(row["bidPrice"])), "ask": Decimal(str(row["askPrice"]))}

    def futures_flow(self, symbol: str) -> Mapping[str, Decimal]:
        """Return a compact 15-minute derivatives-flow snapshot."""
        common = {"symbol": symbol, "period": "5m", "limit": 4}
        interest = self._request("GET", "/futures/data/openInterestHist", common)
        taker = self._request("GET", "/futures/data/takerlongshortRatio", common)
        top = self._request(
            "GET", "/futures/data/topLongShortPositionRatio",
            {"symbol": symbol, "period": "5m", "limit": 2},
        )
        premium = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

        oi_start = Decimal(str(interest[0]["sumOpenInterestValue"])) if interest else Decimal("0")
        oi_end = Decimal(str(interest[-1]["sumOpenInterestValue"])) if interest else Decimal("0")
        oi_change = oi_end / oi_start - Decimal("1") if oi_start > 0 else Decimal("0")
        buy_volume = sum((Decimal(str(row.get("buyVol", "0"))) for row in taker[-3:]), Decimal("0"))
        sell_volume = sum((Decimal(str(row.get("sellVol", "0"))) for row in taker[-3:]), Decimal("0"))
        taker_ratio = buy_volume / sell_volume if sell_volume > 0 else Decimal("0")
        top_ratio = Decimal(str(top[-1].get("longShortRatio", "0"))) if top else Decimal("0")
        top_previous = Decimal(str(top[0].get("longShortRatio", "0"))) if top else Decimal("0")
        return {
            "open_interest_change_15m": oi_change,
            "taker_buy_sell_ratio_15m": taker_ratio,
            "funding_rate": Decimal(str(premium.get("lastFundingRate", "0"))),
            "top_position_ratio": top_ratio,
            "top_position_ratio_change": top_ratio - top_previous,
        }

    def account(self) -> AccountSnapshot:
        row = self._request("GET", "/fapi/v2/account", signed=True)
        return AccountSnapshot(Decimal(str(row["totalWalletBalance"])), Decimal(str(row["availableBalance"])))

    def position_amount(self, symbol: str) -> Decimal:
        rows = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        return Decimal(str(rows[0]["positionAmt"])) if rows else Decimal("0")

    def open_position_symbols(self) -> Sequence[str]:
        rows = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        return [str(row["symbol"]) for row in rows if Decimal(str(row["positionAmt"])) != 0]

    def settlement(self, position: Position, end_ms: int) -> TradeSettlement | None:
        trades = self._request("GET", "/fapi/v1/userTrades",
                               {"symbol": position.symbol, "startTime": position.opened_at_ms,
                                "endTime": end_ms, "limit": 1000}, signed=True)
        if not trades:
            return None
        exits = [row for row in trades if row.get("side") == position.side.exit_order_side]
        if not exits:
            return None
        exit_quantity = sum((Decimal(str(row["qty"])) for row in exits), Decimal("0"))
        exit_notional = sum((Decimal(str(row["price"])) * Decimal(str(row.get("qty", "0"))))
                            for row in exits)
        average = exit_notional / exit_quantity if exit_quantity > 0 else Decimal("0")
        realized = sum((Decimal(str(row.get("realizedPnl", "0"))) for row in trades), Decimal("0"))
        commission = sum((Decimal(str(row.get("commission", "0"))) for row in trades), Decimal("0"))
        income = self._request("GET", "/fapi/v1/income",
                               {"symbol": position.symbol, "incomeType": "FUNDING_FEE",
                                "startTime": position.opened_at_ms, "endTime": end_ms, "limit": 1000}, signed=True)
        funding = sum((Decimal(str(row.get("income", "0"))) for row in income), Decimal("0"))
        return TradeSettlement(average, realized, commission, funding)

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def enter(self, plan: TradePlan, slippage: Decimal) -> OrderFill:
        book = self.book(plan.signal.symbol)
        reference = book["ask"] if plan.signal.side.value == "long" else book["bid"]
        price = reference * (Decimal("1") + slippage if plan.signal.side.value == "long" else Decimal("1") - slippage)
        fill = self._ioc(plan.signal.symbol, plan.signal.side.entry_order_side, plan.quantity, price,
                         False, plan.signal.strategy, "e")
        if fill.quantity <= 0:
            raise BinanceError(f"IOC entry order was not filled for {plan.signal.symbol}")
        return fill

    def reduce(self, position: Position, quantity: Decimal, slippage: Decimal) -> OrderFill:
        book = self.book(position.symbol)
        reference = book["bid"] if position.side.value == "long" else book["ask"]
        price = reference * (Decimal("1") - slippage if position.side.value == "long" else Decimal("1") + slippage)
        try:
            limit_fill = self._ioc(
                position.symbol, position.side.exit_order_side, quantity, price,
                True, position.strategy, "x",
            )
        except BinanceError as exc:
            logging.warning(
                "reduce-only IOC failed; using market fallback symbol=%s quantity=%s error=%s",
                position.symbol, quantity, exc,
            )
            return self._market_reduce(position, quantity, "xm")
        remaining = max(Decimal("0"), quantity - limit_fill.quantity)
        if remaining <= 0:
            return limit_fill
        logging.warning(
            "reduce-only IOC partially filled; using market fallback symbol=%s "
            "requested=%s filled=%s remaining=%s",
            position.symbol, quantity, limit_fill.quantity, remaining,
        )
        market_fill = self._market_reduce(position, remaining, "xm")
        filled = limit_fill.quantity + market_fill.quantity
        notional = (limit_fill.average_price * limit_fill.quantity
                    + market_fill.average_price * market_fill.quantity)
        average = notional / filled if filled > 0 else market_fill.average_price
        return OrderFill(
            f"{limit_fill.order_id}+{market_fill.order_id}", position.symbol,
            position.side.exit_order_side, filled, average, market_fill.status,
            {"limit": limit_fill.raw, "market_fallback": market_fill.raw},
        )

    def emergency_close(self, position: Position) -> OrderFill:
        return self._market_reduce(position, position.remaining_quantity, "em")

    def _market_reduce(self, position: Position, quantity: Decimal, action: str) -> OrderFill:
        params = {"symbol": position.symbol, "side": position.side.exit_order_side, "type": "MARKET",
                  "quantity": format(quantity, "f"), "reduceOnly": "true",
                  "newOrderRespType": "RESULT",
                  "newClientOrderId": self._client_order_id(position.strategy, action)}
        row = self._request("POST", "/fapi/v1/order", params, signed=True)
        filled = Decimal(str(row.get("executedQty", "0")))
        if filled <= 0:
            raise BinanceError(f"market reduce was not filled for {position.symbol}")
        average = self._average_price(row, position.symbol, self.mark_price(position.symbol))
        return OrderFill(str(row["orderId"]), position.symbol, position.side.exit_order_side,
                         filled, average, str(row.get("status", "")), row)

    def _ioc(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal, reduce_only: bool,
        strategy: str, action: str,
    ) -> OrderFill:
        instrument = self.instruments()[symbol]
        rounding = ROUND_UP if side == "BUY" else ROUND_DOWN
        rounded_price = (price / instrument.tick_size).to_integral_value(rounding=rounding) * instrument.tick_size
        params = {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "IOC",
                  "quantity": format(quantity, "f"), "price": format(rounded_price, "f"),
                  "newOrderRespType": "RESULT",
                  "newClientOrderId": self._client_order_id(strategy, action)}
        if reduce_only:
            params["reduceOnly"] = "true"
        row = self._request("POST", "/fapi/v1/order", params, signed=True)
        filled = Decimal(str(row.get("executedQty", "0")))
        average = self._average_price(row, symbol, rounded_price)
        return OrderFill(str(row["orderId"]), symbol, side, filled, average, str(row.get("status", "")), row)

    def _average_price(self, row: Mapping[str, Any], symbol: str, fallback: Decimal) -> Decimal:
        average = Decimal(str(row.get("avgPrice", "0")))
        filled = Decimal(str(row.get("executedQty", "0")))
        quote = Decimal(str(row.get("cumQuote", "0")))
        if average <= 0 and filled > 0 and quote > 0:
            average = quote / filled
        if average <= 0 and row.get("orderId") is not None:
            try:
                detail = self._request("GET", "/fapi/v1/order",
                                       {"symbol": symbol, "orderId": row["orderId"]}, signed=True)
                average = Decimal(str(detail.get("avgPrice", "0")))
                detail_quote = Decimal(str(detail.get("cumQuote", "0")))
                if average <= 0 and filled > 0 and detail_quote > 0:
                    average = detail_quote / filled
            except BinanceError:
                pass
        return average if average > 0 else fallback

    def replace_stop(self, position: Position, stop_price: Decimal) -> str:
        if stop_price <= 0:
            raise BinanceError(f"refusing invalid stop price for {position.symbol}: {stop_price}")
        instrument = self.instruments()[position.symbol]
        rounding = ROUND_DOWN if position.side.value == "long" else ROUND_UP
        trigger = (stop_price / instrument.tick_size).to_integral_value(rounding=rounding) * instrument.tick_size
        params = {"algoType": "CONDITIONAL", "symbol": position.symbol,
                  "side": position.side.exit_order_side, "type": "STOP_MARKET",
                  "quantity": format(position.remaining_quantity, "f"), "triggerPrice": format(trigger, "f"),
                  "reduceOnly": "true", "workingType": "MARK_PRICE", "priceProtect": "false",
                  "clientAlgoId": self._client_order_id(position.strategy, "s")}
        row = self._request("POST", "/fapi/v1/algoOrder", params, signed=True)
        new_id = str(row["algoId"])
        if position.stop_order_id:
            try:
                self._request("DELETE", "/fapi/v1/algoOrder",
                              {"symbol": position.symbol, "algoId": position.stop_order_id}, signed=True)
            except BinanceError:
                pass
        return new_id

    @staticmethod
    def _client_order_id(strategy: str, action: str) -> str:
        codes = {
            "bn-stra-top-gainers-1": "momentum",
            "bn-stra-top-gainers-exhaustion-short-1": "pullback",
            "bn-stra-copy-lead-1": "copy",
        }
        code = codes.get(strategy, hashlib.sha1(strategy.encode("utf-8")).hexdigest()[:4])
        return f"bsp_{code}_{action}_{uuid4().hex[:20]}"

    def cancel_symbol_orders(self, symbol: str) -> None:
        for path in ("/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"):
            try:
                self._request("DELETE", path, {"symbol": symbol}, signed=True)
            except BinanceError:
                pass
