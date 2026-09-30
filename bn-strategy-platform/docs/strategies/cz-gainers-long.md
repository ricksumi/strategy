# cz-gainers-long

## Identity

- Strategy ID: `cz-gainers-long`
- Direction: long only
- Type: filtered top-three four-hour momentum pullback
- Ranking interval: closed 15-minute candles
- Entry-management interval: closed 5-minute candles
- Version: `2.3.0`
- Implementation: `strategies/cz_gainers.py`

This strategy does not use EMA or ADX. It assumes that the strongest liquid
four-hour movers have a higher continuation probability, then waits for bounded
pullbacks instead of buying their ranked prices immediately.

## Universe And Ranking

The prefilter requires:

- A supported USDT Futures instrument
- 24-hour quote volume at least `min_quote_volume`
- 24-hour change between `min_gain_24h` and `max_gain_24h`
- Spread no greater than `max_spread`
- No stablecoin pair, leveraged `UP`/`DOWN` token, or disallowed suffix

Eligible symbols are scanned concurrently. Four-hour momentum is calculated from 16
closed 15-minute candles. The raw top 10 are shortlisted before the slower momentum
gates are evaluated. An extreme upper wick can cause the ranker to use the previous
close instead of the latest close, reducing single-bar spike distortion.

Each shortlisted symbol must also satisfy:

- Four-hour momentum at least `momentum_4h_min`
- Non-negative 30-minute momentum
- Non-negative four-hour value measured from four closed 1-hour candles

Volume ratio, consecutive green candles, and distance from the 24-hour high are
stored as metrics but do not reject the candidate. Rejected symbols are removed
before ranking; the strongest three qualified symbols receive independent pullback
windows. A rejected raw rank-one symbol therefore cannot hide a qualified rank-two
or rank-three symbol.

## Pullback Entry

Each of the top-three qualified candidates is registered at the current mark for
`pullback_window_minutes`.

- No entry before pullback reaches `pullback_min`.
- The first touch arms confirmation instead of entering immediately.
- A later completed 5-minute candle must form a higher low and close back above the
  `pullback_min` trigger price while the current mark also remains above it.
- Invalidate when pullback reaches or exceeds `pullback_max`.
- After discard, only that symbol waits `pullback_rearm_minutes` before it can be
  registered again; the other candidate windows remain active.

The production example uses a 2% minimum pullback, 4% invalidation, and a 15-minute
window. Entry reason is `top3_4h_pullback_continuation`.

## Score

Score model `cz-gainers-v5` combines:

- Rank quality, weighted from rank one through rank three
- Four-hour momentum above the 5% floor
- Pullback quality, centered near 2.5%
- 30-minute momentum
- One-hour-series momentum

## Initial Stop And R

The initial stop is a fixed `stop_distance` below entry. With the production example
value of 2%, that 2% price distance defines `1R`. The strategy-level fixed margin,
leverage, planned-loss ceiling, and position count are configured independently of
the stop model.

## Profit And Risk Management

| Progress | Action |
| --- | --- |
| Best reaches `10%` margin ROI | Lock `2%` margin ROI |
| Best reaches `20%` margin ROI | Lock `8%` margin ROI |
| Best reaches `1.5R` | Move stop above entry by `breakeven_buffer` |
| Best reaches `2R` | Lock `1R` |
| Current reaches `2.25R` | Close 50% |
| First partial completed | Keep runner stop at least `1R` |
| Best reaches `5R` | Trail 1.5R behind the best price |
| Best/current reaches `9R` | Close the remaining runner |
| Still losing after four hours | Close with `time_stop` |

The early ROI guards are enabled in the production example to prevent a 10%-13%
margin-ROI gain from returning to the full initial stop before the R-based ladder is
active.

## Re-entry And Limits

- General per-symbol open cooldown defaults to 30 minutes.
- Same-direction loss cooldown and fresh 5-minute breakout rules are applied by the
  shared runtime.
- Two pure long stop losses can disable CZ for that symbol for the rest of the day.
- Shared portfolio allocation can divert an otherwise valid CZ entry to PAPER.

## Important Limitations

- At most three qualified candidates are tracked at once; lower-ranked candidates
  wait until a slot becomes available.
- Ranking uses closed candles, while pullback triggering uses the current mark.
- A fast move through the full 2%-4% entry band invalidates without an entry.
- A touched pullback that never produces a closed 5-minute reclaim expires without
  entering.
- The strategy's edge depends on continuation after a strong four-hour move; it can
  perform poorly when the ranking is dominated by one-off news spikes.
