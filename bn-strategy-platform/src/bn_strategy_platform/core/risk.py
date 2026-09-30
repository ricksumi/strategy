"""Account-level fixed-margin sizing with a hard loss cap."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from .models import AccountSnapshot, Instrument, Signal, TradePlan


@dataclass(frozen=True)
class RiskPolicy:
    leverage: int
    margin_per_trade_usdt: Decimal
    risk_per_trade_usdt: Decimal
    max_daily_loss_usdt: Decimal
    max_positions: int

    def build_plan(
        self,
        signal: Signal,
        instrument: Instrument,
        account: AccountSnapshot,
        daily_net_pnl: Decimal,
        *,
        enforce_planned_risk: bool = True,
    ) -> TradePlan:
        if len(account.positions) >= self.max_positions:
            raise ValueError("maximum concurrent positions reached")
        if daily_net_pnl <= -self.max_daily_loss_usdt:
            raise ValueError("daily net loss limit reached")
        distance = abs(signal.signal_price - signal.stop_price)
        if distance <= 0:
            raise ValueError("stop must differ from entry")
        raw_quantity = self.margin_per_trade_usdt * Decimal(self.leverage) / signal.signal_price
        quantity = (raw_quantity / instrument.step_size).to_integral_value(rounding=ROUND_DOWN) * instrument.step_size
        if quantity < instrument.min_quantity:
            raise ValueError("risk-sized quantity is below exchange minimum")
        notional = quantity * signal.signal_price
        if notional < instrument.min_notional:
            raise ValueError("risk-sized notional is below exchange minimum")
        margin = notional / Decimal(self.leverage)
        planned_risk = distance * quantity
        if enforce_planned_risk and planned_risk > self.risk_per_trade_usdt:
            raise ValueError(f"planned loss {planned_risk} exceeds per-trade risk limit")
        if margin > account.available_usdt:
            raise ValueError("insufficient available margin")
        return TradePlan(signal, self.risk_per_trade_usdt, quantity, margin, self.leverage)
