from decimal import Decimal
import unittest

from bn_strategy_platform.core.models import AccountSnapshot, Instrument, Side, Signal
from bn_strategy_platform.core.risk import RiskPolicy


def signal(stop: str = "0.98") -> Signal:
    return Signal("test", "1", "TESTUSDT", Side.LONG, Decimal("1"), Decimal(stop),
                  Decimal("80"), "test", 1)


class RiskPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RiskPolicy(3, Decimal("200"), Decimal("20"), Decimal("60"), 5)
        self.instrument = Instrument("TESTUSDT", Decimal("0.001"), Decimal("1"), Decimal("1"), Decimal("5"))

    def test_position_size_uses_fixed_configured_margin(self) -> None:
        plan = self.policy.build_plan(signal(), self.instrument, AccountSnapshot(1000, 1000), Decimal("0"))
        self.assertEqual(plan.quantity, Decimal("600"))
        self.assertEqual(plan.estimated_margin, Decimal("200"))

    def test_daily_loss_and_margin_are_hard_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "daily net loss"):
            self.policy.build_plan(signal(), self.instrument, AccountSnapshot(1000, 1000), Decimal("-60"))
        with self.assertRaisesRegex(ValueError, "insufficient available margin"):
            self.policy.build_plan(signal(), self.instrument, AccountSnapshot(1000, 199), Decimal("0"))

    def test_strategy_requested_margin_does_not_override_global_margin(self) -> None:
        copied = Signal("copy-lead", "2", "TESTUSDT", Side.LONG, Decimal("1"), Decimal("0.9"),
                        Decimal("80"), "lead", 1, requested_margin=Decimal("50"))
        with self.assertRaisesRegex(ValueError, "exceeds per-trade risk"):
            self.policy.build_plan(copied, self.instrument, AccountSnapshot(1000, 1000), Decimal("0"))

        safe_copy = Signal("copy-lead", "2", "TESTUSDT", Side.LONG, Decimal("1"), Decimal("0.98"),
                           Decimal("80"), "lead", 1, requested_margin=Decimal("50"))
        plan = self.policy.build_plan(safe_copy, self.instrument, AccountSnapshot(1000, 1000), Decimal("0"))
        self.assertEqual(plan.estimated_margin, Decimal("200"))
