# Strategy Reference

This directory documents the strategy plugins currently supported by the unified
Binance USD-M Futures runtime.

| Strategy ID | Direction | Type | Code version |
| --- | --- | --- | --- |
| [`bn-stra-top-gainers-1`](bn-stra-top-gainers-1.md) | Short | Candle-driven momentum exhaustion | `3.12.0`, or `3.13.0` with confirmation pullback enabled |
| [`bn-stra-top-gainers-exhaustion-short-1`](bn-stra-top-gainers-exhaustion-short-1.md) | Long | Candle-driven exhaustion reclaim | `4.10.0`, or `4.11.0` with confirmation pullback enabled |
| [`cz-gainers-long`](cz-gainers-long.md) | Long | Top-three filtered four-hour momentum pullback | `2.3.0` |
| [`bn-stra-copy-lead-1`](bn-stra-copy-lead-1.md) | Lead direction | Event-driven copy strategy | `2.4.0` |
| [`bn-stra-fast-stop-reversal-1`](bn-stra-fast-stop-reversal-1.md) | Opposite pullback trade | Structured fast-stop reversal | `1.3.0` |
| [`bn-stra-momentum-ignition-long-1`](bn-stra-momentum-ignition-long-1.md) | Long | Flow-confirmed momentum ignition pullback | `1.0.0` |

## Shared Runtime Rules

The strategy selects a symbol, side, reference price, stop, score, and reason. The
platform owns account-level allocation, exchange execution, persistence,
notifications, and recovery.

- `mode` can be `live`, `paper`, or `shadow` per strategy.
- Position size uses the configured fixed margin and leverage. A signal is rejected
  when its planned stop loss exceeds `risk_per_trade_usdt`; size is not silently
  reduced to fit the risk ceiling.
- Live entries use bounded-slippage IOC limit orders. An entry that receives no fill
  is not converted to a market order.
- Every live fill must receive an exchange-hosted protective stop. Failure to create
  that stop triggers an emergency reduce-only close.
- Normal exits first use a reduce-only IOC order. Any unfilled remainder is completed
  with a reduce-only market order.
- `max_positions` counts positions whose stop can still lose principal.
  `hard_max_positions` counts every live position, including protected runners.
- Capital rotation uses position age and R progress, not entry score. It never
  reduces another strategy below its reserved `base_positions`.
- Global, directional, per-strategy, planned-loss, margin, and daily-loss limits are
  applied before entry.
- A valid live signal rejected only by allocation controls can be recorded as a
  `mode=paper`, `entry_context=position_limit` trade when
  `paper_on_position_limit=true`.
- PAPER opens and full closes are sent through the configured notification targets
  with explicit `[PAPER OPEN]` and `[PAPER CLOSED]` labels. Partial exits are silent.
- Open positions, initial risk, current stop intent, partial tiers, scale-in state,
  best price, worst price, entry score, and strategy version are persisted in MySQL.
- Startup restores persisted positions and reconciles live positions with Binance
  before new entries are allowed.

## Configuration Authority

The Python settings classes define defaults and validation. The active `config.json`
overrides those defaults. `config.production.example.json` is an example, not a
snapshot of the running account. The strategy version must change whenever entry,
exit, sizing, or risk behavior changes.

The source of truth is:

1. The strategy implementation under `src/bn_strategy_platform/strategies/`.
2. The active runtime `config.json`.
3. The shared risk and execution code under `src/bn_strategy_platform/core/`.

## Common Loss Re-entry Controls

The candle-driven strategies expose `loss_cooldown_minutes`,
`reentry_breakout_lookback`, and `max_symbol_stop_losses_per_day`.

After a losing close in the same strategy, symbol, and direction, the runtime waits
for the cooldown and then requires a completed 5-minute candle to break the prior
structure. Two pure stop losses per Asia/Shanghai day disable that same combination
when the example value is `2`. The fast-stop reversal observer remains independent
of this same-direction lock.
