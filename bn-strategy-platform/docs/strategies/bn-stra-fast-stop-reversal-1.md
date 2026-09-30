# bn-stra-fast-stop-reversal-1

## Identity

- Strategy ID: `bn-stra-fast-stop-reversal-1`
- Direction: opposite a failed pullback-long trade
- Type: event-driven structured fast-stop reversal
- Candle interval: 5 minutes
- Version: `1.3.0`
- Implementation: `strategies/fast_stop_reversal.py`

The production example runs this strategy in PAPER. It does not reverse every stopped
trade. It waits for evidence that the original pullback-long thesis failed and that a
new short structure formed.

## Eligible Source

By default, only `bn-stra-top-gainers-exhaustion-short-1` is observed. A source trade
must:

- Be a normal LIVE trade closed by a pure initial stop loss.
- Close within `max_source_hold_minutes`, currently 15 minutes.
- Lose at least `min_source_loss_r`, currently `0.6R`, measured from the source entry,
  initial stop, and exit price.
- Still be inside the 30-minute observation window.

The source strategy is locked from reopening the symbol during this observation
window. Locks are rebuilt from MySQL after a restart.

## Structured Reversal Entry

The observer uses complete 5-minute candles and requires this sequence:

1. A directional candle closes beyond the source trade's original stop.
2. That candle also breaks the prior three-candle structure.
3. Its quote volume is at least 1.5 times the trailing ten-candle average.
4. A later candle retests the broken structure within the configured tolerance.
5. The retest candle closes back in the reversal direction, and the current mark
   remains beyond the broken level without excessive entry deviation.

If price closes back across the source's original stop during the retest, the setup is
invalidated. Missing structure, volume, retest, or mark confirmation is persisted as
an explicit skip event.

Only one reversal trade per symbol is allowed per Asia/Shanghai day, regardless of
whether the first reversal wins or loses.

## Risk

The production example uses half of the pullback strategy's normal allocation:

- Margin: `75 USDT`
- Planned risk: `7.5 USDT`
- Leverage: `3x`
- Maximum daily strategy loss: `22.5 USDT`

The initial stop sits beyond both the retest candle and the source trade's original
stop, including `structure_buffer`. Its distance remains bounded by the configured
minimum price distance and maximum ROI loss.

## Profit Protection

The PAPER strategy keeps the existing staged ROI protection:

| Best margin ROI | Action |
| --- | --- |
| `8%` | Move stop to lock `3%` ROI |
| `15%` | Trail by `1.2%` price distance |
| `25%` | Tighten trailing distance to `0.6%` |

## Stored Metrics

Each entry records the source identity, source loss in R, original source stop,
structure-break level, breakout volume ratio, retest candle, and entry deviation.
These fields are intended to separate the value of the break, volume, and retest
requirements when enough PAPER samples have accumulated.
