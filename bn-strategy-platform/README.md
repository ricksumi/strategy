# Binance Strategy Platform

A single maintainable runtime for multiple Binance USD-M Futures strategies.
Exchange access, market data, risk controls, execution, persistence, notifications,
and recovery are shared. Strategy modules contain signal and position-management
rules only.

## Architecture

```text
src/bn_strategy_platform/
  core/           domain models, ports, risk policy, multi-strategy engine
  exchanges/      Binance USD-M REST adapter
  market_data/    shared market-data cache
  persistence/    unified MySQL storage
  notifications/  Hermes delivery to Weixin and Telegram
  strategies/     explicit long, short, and event-driven copy strategy plugins
  apps/            command-line runtime
```

Detailed strategy behavior is documented in
[`docs/strategies/README.md`](docs/strategies/README.md). Each strategy document
covers universe selection, entry confirmation, sizing, stop placement, profit
management, cooldowns, persisted telemetry, and operational limitations.

`momentum-long`, `exhaustion-short`, and `exhaustion_pullback_long` are explicit
models. The production pullback strategy converts the former exhaustion setup into
a long entry while retaining the database strategy ID for position recovery. Trade
rows record `strategy`, `strategy_version`, and `mode`; signal rows use
`strategy_name`, `strategy_version`, and `run_mode`.

`copy-lead` is an event-driven plugin using the same execution, risk, storage,
notification, and recovery layers. Its public Binance web endpoint is less stable
than the official Futures API, so it is disabled in the example configuration.
The exchange adapter supports both crypto `PERPETUAL` and `TRADIFI_PERPETUAL`
instruments. Set `event_not_before_ms` during a PAPER-to-LIVE cutover so historical
lead orders inside the polling lookback cannot be replayed as new real entries.

`cz-gainers-long` ranks the full liquid universe by four-hour momentum from closed
15-minute candles. It shortlists the raw top 10, applies the 30-minute and one-hour
quality gates, then tracks the strongest three qualified symbols independently. Each
candidate is registered for 15 minutes instead of being chased immediately: a 2%
pullback arms the trade, a later closed 5-minute higher-low reclaim confirms entry,
and a 4% pullback invalidates only that candidate. Volume ratio, consecutive-green-candle,
and off-high gates are retained only as recorded metrics and do not reject entries.
The strategy uses one risk definition based on its exchange-hosted 2% initial stop.
Half is realized at 2.25R, the runner locks 1R and targets 9R, with trailing from
5R. Losing positions alone retain the four-hour time stop.

The production momentum-short and pullback-long strategies use two-stage entries.
The long model first detects downside exhaustion, then requires a higher low, a closed
5-minute reclaim above both the prior high and EMA20, and reclaim volume confirmation.
The short model first detects a bullish impulse, then waits for a lower high, a closed
break below the prior low and EMA9, and negative 15-minute momentum. A setup expires
after 20 minutes or is invalidated if the original trend extends by another 0.5 ATR.
Both strategies use the same initial-risk framework with their ATR-derived opening
stop as 1R. Best observed R activates each protection stage: the short reduces risk
at 0.50R and the long at 0.60R, the configured price buffer protects trading costs
from 1R, and 0.75R is locked from 1.5R. At 2R the short strategy realizes 50% and the
long strategy realizes 30%; the remaining runner immediately locks 1R, uses a 1R
trailing offset from 3R, and a 0.5R offset from 5R. The initial stop and completed
partial tiers are persisted so restarts cannot redefine R or repeat a tier.
The pullback-long strategy can add 75 USDT once after it has reached 1R, moved its
stop above entry, and printed a completed 5-minute higher-low breakout. The add is
rejected if the combined position would exceed its 15 USDT planned-loss ceiling or
the shared 60 USDT open-risk ceiling.
After a losing live close, the same strategy and direction cool down for 15 minutes.
A new entry then requires a completed 5-minute candle to break the preceding
three-candle structure; a stale signal cannot immediately reopen the position. Two
pure stop losses disable that strategy, symbol, and direction for the rest of the
Asia/Shanghai day without disabling the PAPER reversal observer.

Live entries share a portfolio risk allocator. Reserved strategy slots are filled
before the remaining slot is awarded to an eligible signal. `max_positions` is the soft
limit for positions whose stops can still lose principal; positions protected at or
beyond entry release that slot. `hard_max_positions` remains an absolute count limit
because protected runners still consume exchange margin. Open risk is measured from
each remaining quantity to its current protective stop. Global and same-direction
open-risk caps prevent correlated entries from consuming the account's entire loss
allowance at once. Allocation-blocked signals continue as notified paper trades when
`paper_on_position_limit` is enabled.

Optional capital rotation applies only after an allocation limit rejects a valid
entry. A live position is eligible for replacement only when it remains unprotected
after the configured minimum hold, has never reached the configured best-R threshold,
and is currently inside the configured stagnant R range. Rotation does not use the
entry score: historical scores are retained for analysis but are not sufficiently
predictive to justify closing a live position. Cross-strategy rotation cannot reduce
the source strategy below its reserved `base_positions`; a strategy at its own local
limit may replace one of its own stale positions. The prospective entry is preflighted
before the old position is closed. Rotation is globally rate-limited and projected
realized loss cannot breach either daily loss limit. Protected runners are never
rotation targets.

`bn-stra-fast-stop-reversal-1` is an event-driven PAPER strategy for failed pullback
longs. A source must stop within 15 minutes and lose at least 0.6R. The reversal then
requires a complete 5-minute close beyond the original stop, a break of the prior
three-candle structure on at least 1.5 times recent volume, and a later failed retest
of the broken level. It uses half the pullback strategy's normal allocation and allows
only one reversal per symbol per Asia/Shanghai day. For 30 minutes after a qualifying
fast stop, the pullback strategy cannot reopen that symbol. The lock is rebuilt from
MySQL after restart, and source, structure-break, volume, and retest metrics are saved.

`bn-stra-momentum-ignition-long-1` is an independent PAPER strategy for confirmed
upside continuation. It uses one-hour and four-hour momentum, relative BTC strength,
EMA structure, volume expansion, open-interest growth, taker-buy dominance, funding,
and top-trader positioning. A qualified breakout is not bought immediately: the
strategy waits for its first ATR-bounded pullback and two complete 5-minute closes
back above the breakout level. All filter inputs, entry structure, MFE, and MAE are
persisted for PAPER evaluation before any LIVE cutover.

## Risk And Execution

- Portfolio-level `max_positions` limits principal-risk positions;
  `hard_max_positions` is the absolute live-position limit across all plugins.
- `position_rotation_enabled` may reclaim a hard or risk slot from a stale,
  unprotected position. Hold-time, R-progress, current-R, strategy reservation,
  daily-loss, and cooldown gates all have to pass before a live close is attempted.
- Normal PAPER strategies enforce their own configured position and daily-loss
  limits independently. Their results cannot block another PAPER strategy, and
  capacity-analysis trades with `entry_context=position_limit` bypass PAPER risk
  limits by design.
- Each strategy may define a `risk` object with independent leverage, fixed margin,
  planned-loss ceiling, position limit, daily loss limit, and entry/exit slippage.
  Missing per-strategy values inherit the top-level values for backward compatibility.
- `risk_per_trade_usdt` remains a hard planned-loss ceiling. Signals whose stop
  distance would exceed it are rejected instead of reducing or enlarging margin.
- Entries and normal exits use IOC limit orders with bounded slippage. Any unfilled
  normal-exit remainder is immediately completed with a reduce-only market order;
  entries are never enlarged by this fallback.
- Every live entry immediately creates an exchange-hosted `STOP_MARKET` Algo order.
- A failed protective stop triggers an immediate reduce-only market close.
- Stop replacement creates the new stop before cancelling the old stop.
- After a partial live exit, the exchange stop is immediately replaced with the
  remaining quantity. Failure to protect the remainder triggers an emergency close.
- Startup reconciles stored positions with Binance before managing or opening trades.
- Protective-stop intent is persisted with each position, allowing reconciled
  exchange exits to retain `stop_loss`, `break_even`, `protected_stop`, or
  `trailing_stop` instead of collapsing into one generic reason.
- Unknown manual or legacy-bot positions block every new platform entry.
- Only fully closed candles are passed to strategies.
- Strategies that explicitly need an in-progress candle may request a separately
  named point-in-time live snapshot; closed-candle indicator inputs remain unchanged.
- A live runtime can set `paper_on_position_limit=true` to turn an otherwise valid
  signal rejected by portfolio capacity, margin, daily-loss, or planned-risk limits
  into a PAPER trade. These
  trades use the same entry price, fixed-margin sizing, stop, and profit-management
  rules as the originating strategy, but do not consume live margin or live position
  slots. They are restored after restart and stored with `mode=paper` and
  `entry_context=position_limit` for later quality analysis. Opens and full closes
  are notified; partial exits remain silent.

Live closes are reconciled from Binance user trades and funding-income records so
the unified database includes actual realized PnL, commission, funding, and exit price.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
cp config.example.json config.json
```

Start in shadow mode first:

```bash
.venv/bin/bn-strategy --config config.json --once
.venv/bin/bn-strategy --config config.json
```

Live trading has two independent gates: set `mode` to `live` and `allow_live` to
`true`, then add `--confirm-live` to the service command. Never enable those gates
until shadow/paper results and position reconciliation have been reviewed.

## Configuration

Secrets live only in `.env`. Strategy behavior lives in `config.json`. Increase a
strategy's `version` in code whenever its entry, exit, sizing, or risk behavior
changes. `database_backend=memory` is intended only for tests and one-shot local
checks; use MySQL for persistent shadow, paper, and live operation.

Each strategy may set `mode` to `live`, `paper`, or `shadow`. If omitted, it inherits
the platform mode. This allows experimental strategies to share one process and one
market-data cache without gaining permission to place live orders.

Position-limit PAPER trades can be queried separately:

```sql
SELECT strategy, strategy_version, symbol, side, status, net_pnl, margin_roi
FROM strategy_trades
WHERE mode = 'paper' AND entry_context = 'position_limit'
ORDER BY opened_at_ms DESC;
```

Every newly opened trade records both `best_price` and `worst_price`. These
provide MFE and MAE inputs for later entry, stop-distance, and score analysis.
Rows created before the `worst_price` migration remain `NULL` because their
historical adverse excursion cannot be reconstructed reliably.

Copy-lead entries have a hard `max_entry_delay_seconds` gate (120 seconds by
default). Close events remain eligible throughout `lookback_seconds`, but stale
open events are ignored before any market lookup or order planning.

Live entries rejected because of portfolio capacity, margin, the daily loss
gate, or excessive planned per-trade loss are retained as notified
`entry_context='position_limit'` PAPER trades for unbiased evaluation.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
