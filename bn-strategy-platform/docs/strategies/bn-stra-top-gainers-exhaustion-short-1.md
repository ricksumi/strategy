# bn-stra-top-gainers-exhaustion-short-1

## Identity

- Strategy ID: `bn-stra-top-gainers-exhaustion-short-1`
- Model: `exhaustion_pullback_long`
- Direction: long only
- Candle interval: 5 minutes
- Version: `4.10.0` with immediate confirmed entry; `4.11.0` when
  `confirm_pullback_fraction > 0`
- Implementation: `strategies/top_gainers.py`

The ID is retained for database ownership and restart compatibility. The current
production behavior is a pullback-reclaim long strategy, not an exhaustion short.

## Universe

Universe selection is shared with `bn-stra-top-gainers-1`:

- USDT instruments supported by Binance USD-M Futures
- Minimum 24-hour quote volume
- Minimum 24-hour high-low range
- Maximum spread
- Ranking by 24-hour change with a range bonus

The top `active_symbol_limit` contracts are scanned, while armed setups remain in
the universe until they confirm, expire, or invalidate.

## Entry

Entry has two stages designed to avoid buying the first falling candle.

### 1. Exhaustion setup

The source setup requires:

- EMA20 is falling versus its prior value.
- The latest close remains below EMA20 and fails to reclaim the recent peak.
- At least two of the latest three candles are bearish.
- Price is at least 0.8% below the prior 12-candle peak.
- Volume ratio is at least 1.0.
- The current mark is no higher than the latest completed close.

This registers a possible long reversal but does not open a position.

### 2. Reclaim confirmation

Within `entry_confirmation_window_minutes`, a later completed 5-minute candle must:

- Form a higher low.
- Close above the previous candle high.
- Close above EMA20.
- Reach `pullback_reclaim_volume_ratio` relative to the preceding 12 candles.
- Keep the current mark no more than 0.25 ATR below the confirming close.

The setup is invalidated if price extends below the setup low by
`entry_invalidation_atr` ATR. The accepted entry reason is
`exhaustion_reclaim_confirmed_long`.

When `confirm_pullback_fraction > 0`, the strategy waits for a bounded retracement
after confirmation. The optional wait has its own expiry and invalidation distance;
enabling it changes the strategy version to `4.11.0`.

## Score

The confirmed score model is `pullback-long-v3`. Components are:

- Quality of the original exhaustion setup
- Strength of the reclaim above the prior high
- Higher-low quality
- Reclaim-volume quality

The score is stored for later calibration. Capital rotation deliberately does not
use it because the current score has not shown reliable separation of winners and
losers.

## Initial Stop And R

Stop distance is `ATR ratio * stop_atr`, clamped between
`min_stop_distance` and `max_stop_distance`. The long stop is placed that distance
below the actual entry. The persisted entry-to-stop distance defines `1R`.

The production example uses 3x leverage, 150 USDT fixed margin, a 15 USDT planned
loss ceiling, four reserved positions, and at most four strategy positions. Active
runtime configuration remains authoritative. The shared open-risk ceiling remains
60 USDT.

## Profit And Risk Management

| Best/current progress | Action |
| --- | --- |
| Best reaches `0.60R` | Reduce remaining stop risk to `0.25R` |
| Best reaches `1R` | Move stop above entry by the cost buffer |
| Best reaches `1.5R` | Lock `0.75R` |
| Best and current reach `1R`, followed by a completed 5-minute higher-low breakout | Move the stop above entry, then add 75 USDT once |
| Current reaches `2R` | Close 30% |
| First partial completed | Runner stop locks at least `1R` |
| Best reaches `3R` | Trail one initial R behind the best price |
| Best reaches `5R` | Tighten to 0.5R behind the best price |

The strategy has no fixed full-position take-profit. A strong trend can continue as
a protected runner until the trailing stop is crossed.

The confirmation add is available only to positions opened by the current strategy
version. Its fill is folded into the average entry while the original entry and
initial stop continue to define R. It is refused when the projected combined stop
loss exceeds the per-trade or portfolio risk ceiling.

## Re-entry And Limits

- A losing long waits for the configured cooldown.
- Re-entry requires a completed 5-minute candle to break above recent structure.
- Two pure long stop losses can disable the same strategy and symbol for the rest of
  the Asia/Shanghai day.
- The event-driven fast-stop observer may temporarily reserve the symbol after a
  sufficiently fast loss.

## Stored Reasons

Typical reasons include `exhaustion_reclaim_confirmed_long`, `stop_loss`,
`risk_reduction`, `break_even`, `profit_lock`, `take_profit_1`,
`post_tp1_lock`, `trailing_stop`, and `protected_stop`.
