# bn-stra-top-gainers-1

## Identity

- Strategy ID: `bn-stra-top-gainers-1`
- Model: `continuation_short`
- Direction: short only
- Candle interval: 5 minutes
- Version: `3.12.0` with immediate confirmed entry; `3.13.0` when
  `confirm_pullback_fraction > 0`
- Implementation: `strategies/top_gainers.py`

Despite the historical ID, this production strategy does not chase gainers long. It
finds a strong bullish impulse and enters short only after a closed-candle exhaustion
breakdown confirms that the impulse is weakening.

## Universe

The strategy scans USDT contracts supported by the exchange adapter, including
`PERPETUAL` and `TRADIFI_PERPETUAL` instruments. Candidates must pass:

- Minimum 24-hour quote volume: `min_quote_volume`
- Minimum 24-hour high-low range: `min_range_24h`
- Maximum bid-ask spread: `max_spread`

Candidates are ranked by 24-hour percentage change plus a range bonus. The highest
`active_symbol_limit` symbols are scanned. Symbols with an armed or pending setup
remain tracked even if they leave the refreshed top list.

## Entry

Entry has two required stages.

### 1. Bullish impulse setup

The source setup requires all of the following on closed candles:

- EMA20 is above EMA60.
- ADX is at least `min_adx`.
- One-hour return is at least 1%.
- Current volume ratio is at least 1.5.
- The latest candle forms a healthy reclaim: its low holds above the recent swing
  low and its close exceeds the preceding candle high.
- The current mark has not fallen below the confirming close.

This stage arms a short setup but does not open a position.

### 2. Short confirmation

Within `entry_confirmation_window_minutes`, a later completed 5-minute candle must:

- Form a lower high.
- Close below the previous candle low.
- Close below EMA9.
- Produce negative three-candle, approximately 15-minute, momentum.
- Keep the mark no more than 0.25 ATR above the confirming close.

The setup is invalidated if price extends above its setup high by
`entry_invalidation_atr` ATR. The reason stored for an accepted entry is
`momentum_exhaustion_confirmed_short`.

When `confirm_pullback_fraction` is greater than zero, confirmation registers a
pending entry instead of entering immediately. The short waits for a retracement
toward the confirmation price, expires after `confirm_pullback_window_minutes`, and
is invalidated when the retracement exceeds
`confirm_pullback_invalidate_fraction` of the source-to-confirmation move.

## Score

The confirmed score model is `momentum-short-v5`. It combines:

- Trend maturity
- Original impulse quality
- Volume quality
- Breakdown strength
- Rejection from the setup high

Scores are persisted for analysis and live allocation. They do not replace the
mandatory setup and confirmation conditions.

## Initial Stop And R

Raw stop distance is `ATR ratio * stop_atr`, clamped between
`min_stop_distance` and `max_stop_distance`. The short stop also respects the setup
high plus `structure_stop_buffer_atr`, without exceeding the configured maximum
distance. The final entry-to-stop distance is `1R`.

The production example uses 3x leverage, 100 USDT fixed margin, a 10 USDT planned
loss ceiling, and at most two strategy positions. These are configuration values,
not code constants.

## Profit And Risk Management

All thresholds below are measured from the persisted initial `1R`:

| Best/current progress | Action |
| --- | --- |
| Best reaches `0.50R` | Reduce remaining stop risk to `0.20R` |
| Best reaches `1R` | Move stop beyond entry by the configured cost buffer |
| Best reaches `1.5R` | Lock `0.75R` |
| Current reaches `2R` | Close 50% |
| First partial completed | Runner stop locks at least `1R` |
| Best reaches `3R` | Trail one initial R behind the best price |
| Best reaches `5R` | Tighten to 0.5R behind the best price |

If a newly calculated protective stop is already crossed, the remaining position is
closed immediately rather than submitting an invalid stop.

## Re-entry And Limits

- Same-direction loss cooldown: 15 minutes in the production example.
- A fresh completed 5-minute structure breakdown is required after the cooldown.
- Two pure short stop losses can disable this strategy-symbol-direction for the
  remainder of the Asia/Shanghai day.
- Shared portfolio limits, risk limits, capital rotation, and PAPER fallback are
  described in the [strategy reference](README.md).

## Stored Reasons

Typical entry and exit reasons include:

- `momentum_exhaustion_confirmed_short`
- `stop_loss`
- `risk_reduction`
- `break_even`
- `profit_lock`
- `take_profit_1`
- `post_tp1_lock`
- `trailing_stop`
- `protected_stop`
