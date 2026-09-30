import json
import unittest
from decimal import Decimal

from bn_strategy_platform.core.config import RuntimeConfig


class ConfigTests(unittest.TestCase):
    def test_legacy_single_strategy_config_remains_readable(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "strategy": "momentum-long",
                "strategy_config": {"min_adx": "22"},
                "paper_on_position_limit": True,
            }))
            config = RuntimeConfig.from_file(str(path))
        self.assertEqual(config.strategies[0].name, "momentum-long")
        self.assertEqual(config.strategies[0].settings["min_adx"], "22")
        self.assertTrue(config.paper_on_position_limit)

    def test_live_mode_requires_configuration_gate(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"mode": "live", "strategy": "momentum-long"}))
            with self.assertRaisesRegex(ValueError, "allow_live"):
                RuntimeConfig.from_file(str(path))

    def test_strategy_risk_overrides_are_isolated_and_defaults_are_inherited(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "leverage": 3,
                "margin_per_trade_usdt": "200",
                "strategies": [
                    {"name": "momentum-long", "enabled": True, "settings": {}},
                    {
                        "name": "cz-gainers-long",
                        "enabled": True,
                        "mode": "paper",
                        "settings": {},
                        "risk": {
                            "leverage": 8,
                            "margin_per_trade_usdt": "25",
                            "risk_per_trade_usdt": "5",
                            "base_positions": 1,
                            "max_positions": 2,
                        },
                    },
                ],
            }))
            config = RuntimeConfig.from_file(str(path))

        legacy = config.strategy_risk("momentum-long")
        cz = config.strategy_risk("cz-gainers-long")
        self.assertEqual(legacy.leverage, 3)
        self.assertEqual(legacy.margin_per_trade_usdt, Decimal("200"))
        self.assertEqual(cz.leverage, 8)
        self.assertEqual(cz.margin_per_trade_usdt, Decimal("25"))
        self.assertEqual(cz.risk_per_trade_usdt, Decimal("5"))
        self.assertEqual(cz.base_positions, 1)
        self.assertEqual(cz.max_positions, 2)
        self.assertEqual(config.strategy_mode("cz-gainers-long").value, "paper")
        self.assertEqual(config.strategy_mode("momentum-long").value, "shadow")

    def test_hard_position_limit_cannot_be_below_soft_limit(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "max_positions": 5,
                "hard_max_positions": 4,
                "strategies": [{"name": "momentum-long", "enabled": True, "settings": {}}],
            }))
            with self.assertRaisesRegex(ValueError, "hard_max_positions"):
                RuntimeConfig.from_file(str(path))
