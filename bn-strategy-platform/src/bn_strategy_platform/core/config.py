"""Typed platform configuration with environment-only secrets."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Tuple

from .models import RunMode


def decimal(raw: Dict[str, Any], key: str, default: str) -> Decimal:
    return Decimal(str(raw.get(key, default)))


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    enabled: bool
    settings: Dict[str, Any]
    risk: Dict[str, Any] = field(default_factory=dict)
    mode: RunMode | None = None


@dataclass(frozen=True)
class StrategyRiskConfig:
    leverage: int
    margin_per_trade_usdt: Decimal
    risk_per_trade_usdt: Decimal
    base_positions: int
    max_positions: int
    max_daily_loss_usdt: Decimal
    entry_slippage: Decimal
    exit_slippage: Decimal


@dataclass(frozen=True)
class RuntimeConfig:
    strategies: Tuple[StrategyConfig, ...]
    mode: RunMode
    poll_seconds: int
    universe_refresh_seconds: int
    max_positions: int
    leverage: int
    margin_per_trade_usdt: Decimal
    risk_per_trade_usdt: Decimal
    max_daily_loss_usdt: Decimal
    max_open_risk_usdt: Decimal
    max_directional_open_risk_usdt: Decimal
    entry_slippage: Decimal
    exit_slippage: Decimal
    database_backend: str
    state_path: str
    notify_targets: Tuple[str, ...]
    allow_live: bool
    fee_rate: Decimal
    paper_equity_usdt: Decimal
    paper_on_position_limit: bool = False
    hard_max_positions: int = 5
    position_rotation_enabled: bool = False
    rotation_min_new_score: Decimal = Decimal("80")
    rotation_min_score_advantage: Decimal = Decimal("15")
    rotation_min_hold_minutes: int = 90
    rotation_max_best_r: Decimal = Decimal("0.5")
    rotation_max_current_r: Decimal = Decimal("0")
    rotation_max_loss_r: Decimal = Decimal("0.5")
    rotation_cooldown_minutes: int = 60

    @classmethod
    def from_file(cls, path: str) -> "RuntimeConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        mode = RunMode(str(raw.get("mode", "shadow")))
        raw_strategies = raw.get("strategies")
        if raw_strategies is None:
            raw_strategies = [{
                "name": raw.get("strategy", "top-gainers"),
                "enabled": True,
                "settings": raw.get("strategy_config", {}),
            }]
        strategies = tuple(
            StrategyConfig(
                name=str(item["name"]),
                enabled=bool(item.get("enabled", True)),
                settings=dict(item.get("settings", {})),
                risk=dict(item.get("risk", {})),
                mode=(RunMode(str(item["mode"])) if item.get("mode") is not None else None),
            )
            for item in raw_strategies
        )
        config = cls(
            strategies=strategies,
            mode=mode,
            poll_seconds=int(raw.get("poll_seconds", 15)),
            universe_refresh_seconds=int(raw.get("universe_refresh_seconds", 300)),
            max_positions=int(raw.get("max_positions", 5)),
            leverage=int(raw.get("leverage", 5)),
            margin_per_trade_usdt=decimal(raw, "margin_per_trade_usdt", "200"),
            risk_per_trade_usdt=decimal(raw, "risk_per_trade_usdt", "20"),
            max_daily_loss_usdt=decimal(raw, "max_daily_loss_usdt", "60"),
            max_open_risk_usdt=decimal(raw, "max_open_risk_usdt", "60"),
            max_directional_open_risk_usdt=decimal(
                raw, "max_directional_open_risk_usdt", "45"
            ),
            entry_slippage=decimal(raw, "entry_slippage", "0.002"),
            exit_slippage=decimal(raw, "exit_slippage", "0.003"),
            database_backend=str(raw.get("database_backend", "mysql")),
            state_path=str(raw.get("state_path", "runtime/state.json")),
            notify_targets=tuple(dict.fromkeys(raw.get("notify_targets", ["weixin", "telegram"]))),
            allow_live=bool(raw.get("allow_live", False)),
            fee_rate=decimal(raw, "fee_rate", "0.0005"),
            paper_equity_usdt=decimal(raw, "paper_equity_usdt", "1000"),
            paper_on_position_limit=bool(raw.get("paper_on_position_limit", False)),
            hard_max_positions=int(raw.get("hard_max_positions", raw.get("max_positions", 5))),
            position_rotation_enabled=bool(raw.get("position_rotation_enabled", False)),
            rotation_min_new_score=decimal(raw, "rotation_min_new_score", "80"),
            rotation_min_score_advantage=decimal(raw, "rotation_min_score_advantage", "15"),
            rotation_min_hold_minutes=int(raw.get("rotation_min_hold_minutes", 90)),
            rotation_max_best_r=decimal(raw, "rotation_max_best_r", "0.5"),
            rotation_max_current_r=decimal(raw, "rotation_max_current_r", "0"),
            rotation_max_loss_r=decimal(raw, "rotation_max_loss_r", "0.5"),
            rotation_cooldown_minutes=int(raw.get("rotation_cooldown_minutes", 60)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.mode is RunMode.LIVE and not self.allow_live:
            raise ValueError("mode=live requires allow_live=true")
        if self.leverage < 1 or self.leverage > 20:
            raise ValueError("leverage must be between 1 and 20")
        if (self.margin_per_trade_usdt <= 0 or self.risk_per_trade_usdt <= 0
                or self.max_daily_loss_usdt <= 0 or self.max_open_risk_usdt <= 0
                or self.max_directional_open_risk_usdt <= 0):
            raise ValueError("risk limits must be positive")
        if self.poll_seconds < 5:
            raise ValueError("poll_seconds must be at least 5")
        if self.max_positions < 1 or self.hard_max_positions < self.max_positions:
            raise ValueError("hard_max_positions must be at least max_positions")
        if (self.rotation_min_new_score < 0 or self.rotation_min_score_advantage < 0
                or self.rotation_min_hold_minutes < 0 or self.rotation_max_best_r <= 0
                or self.rotation_max_current_r < 0 or self.rotation_max_loss_r <= 0
                or self.rotation_cooldown_minutes < 0):
            raise ValueError("position rotation settings are invalid")
        if self.database_backend not in {"mysql", "memory"}:
            raise ValueError("database_backend must be mysql or memory")
        if self.fee_rate < 0 or self.paper_equity_usdt <= 0:
            raise ValueError("fee_rate must be non-negative and paper equity must be positive")
        if not any(item.enabled for item in self.strategies):
            raise ValueError("at least one strategy must be enabled")
        names = [item.name for item in self.strategies if item.enabled]
        if len(names) != len(set(names)):
            raise ValueError("enabled strategy names must be unique")
        for item in self.strategies:
            if item.enabled:
                self.strategy_risk(item.name)
                strategy_mode = self.strategy_mode(item.name)
                if strategy_mode is RunMode.LIVE and self.mode is not RunMode.LIVE:
                    raise ValueError(
                        f"{item.name}: mode=live requires the platform mode to be live"
                    )

    def strategy_mode(self, strategy_name: str) -> RunMode:
        item = next((entry for entry in self.strategies if entry.name == strategy_name), None)
        if item is None:
            raise ValueError(f"strategy is not configured: {strategy_name}")
        return item.mode or self.mode

    def strategy_risk(self, strategy_name: str) -> StrategyRiskConfig:
        item = next((entry for entry in self.strategies if entry.name == strategy_name), None)
        if item is None:
            raise ValueError(f"strategy is not configured: {strategy_name}")
        raw = item.risk
        result = StrategyRiskConfig(
            leverage=int(raw.get("leverage", self.leverage)),
            margin_per_trade_usdt=Decimal(str(
                raw.get("margin_per_trade_usdt", self.margin_per_trade_usdt)
            )),
            risk_per_trade_usdt=Decimal(str(
                raw.get("risk_per_trade_usdt", self.risk_per_trade_usdt)
            )),
            base_positions=int(raw.get("base_positions", 0)),
            max_positions=int(raw.get("max_positions", self.max_positions)),
            max_daily_loss_usdt=Decimal(str(
                raw.get("max_daily_loss_usdt", self.max_daily_loss_usdt)
            )),
            entry_slippage=Decimal(str(raw.get("entry_slippage", self.entry_slippage))),
            exit_slippage=Decimal(str(raw.get("exit_slippage", self.exit_slippage))),
        )
        if result.leverage < 1 or result.leverage > 20:
            raise ValueError(f"{strategy_name}: leverage must be between 1 and 20")
        if (result.margin_per_trade_usdt <= 0 or result.risk_per_trade_usdt <= 0
                or result.max_daily_loss_usdt <= 0 or result.max_positions < 1
                or result.base_positions < 0
                or result.base_positions > result.max_positions):
            raise ValueError(f"{strategy_name}: strategy risk limits must be positive")
        if result.entry_slippage < 0 or result.exit_slippage < 0:
            raise ValueError(f"{strategy_name}: slippage must be non-negative")
        return result
