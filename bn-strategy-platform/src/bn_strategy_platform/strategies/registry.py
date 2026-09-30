"""Strategy plugin registry."""

from __future__ import annotations

from typing import Any, Mapping

from ..core.ports import EventStrategyPort, StrategyPort
from .copy_lead import CopyLeadSettings, CopyLeadStrategy
from .cz_gainers import CzGainersSettings, CzGainersStrategy
from .fast_stop_reversal import FastStopReversalSettings, FastStopReversalStrategy
from .momentum_ignition_long import (
    MomentumIgnitionLongStrategy,
    MomentumIgnitionSettings,
)
from .top_gainers import TopGainersSettings, TopGainersStrategy


def build_strategy(
    name: str, settings: Mapping[str, Any], leverage: int = 5, store: Any = None,
) -> StrategyPort | EventStrategyPort:
    if name == "copy-lead":
        merged = dict(settings)
        merged["leverage"] = leverage
        return CopyLeadStrategy(CopyLeadSettings.from_mapping(merged), store)
    if name == "bn-stra-copy-lead-1":
        merged = dict(settings)
        merged["leverage"] = leverage
        strategy = CopyLeadStrategy(CopyLeadSettings.from_mapping(merged), store)
        strategy.name = name
        return strategy
    if name == "cz-gainers-long":
        return CzGainersStrategy(CzGainersSettings.from_mapping(settings))
    if name == "bn-stra-fast-stop-reversal-1":
        if store is None:
            raise ValueError("fast-stop reversal requires a trade store")
        merged = dict(settings)
        merged["leverage"] = leverage
        return FastStopReversalStrategy(FastStopReversalSettings.from_mapping(merged), store)
    if name == "bn-stra-momentum-ignition-long-1":
        return MomentumIgnitionLongStrategy(MomentumIgnitionSettings.from_mapping(settings))
    aliases = {
        "momentum-long": "continuation_long",
        "momentum-short": "continuation_short",
        "top-gainers-long": "continuation_long",
        "exhaustion-short": "exhaustion_short",
        "top-gainers-exhaustion-short": "exhaustion_short",
        "bn-stra-top-gainers-1": "continuation_short",
        "bn-stra-top-gainers-exhaustion-short-1": "exhaustion_pullback_long",
    }
    if name not in aliases:
        raise ValueError(f"unknown strategy plugin: {name}")
    merged = dict(settings)
    merged["model"] = aliases[name]
    strategy = TopGainersStrategy(TopGainersSettings.from_mapping(merged))
    strategy.name = name
    if name == "bn-stra-top-gainers-1":
        strategy.version = (
            "3.13.0"
            if getattr(strategy.settings, "confirm_pullback_fraction", 0) > 0
            else "3.12.0"
        )
    elif name == "bn-stra-top-gainers-exhaustion-short-1":
        strategy.version = (
            "4.11.0"
            if getattr(strategy.settings, "confirm_pullback_fraction", 0) > 0
            else "4.10.0"
        )
    return strategy
