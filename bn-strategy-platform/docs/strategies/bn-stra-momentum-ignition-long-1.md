# bn-stra-momentum-ignition-long-1

## Identity

- Strategy ID: `bn-stra-momentum-ignition-long-1`
- Direction: long only
- Type: flow-confirmed momentum ignition and first-pullback continuation
- Candle interval: 5 minutes
- Version: `1.0.0`
- Initial production mode: PAPER
- Implementation: `strategies/momentum_ignition_long.py`

This strategy does not predict bottoms and does not buy the 24-hour gainers list
directly. It detects a move that has already started, verifies that derivatives flow
supports the move, and waits for the first controlled pullback before entering.

## Candidate Discovery

The broad universe requires adequate quote volume and a bounded spread. The 24-hour
change is only a permissive prefilter. Final qualification uses closed 5-minute bars:

- One-hour return between 2% and 12%.
- Four-hour return between 4% and 25%.
- One-hour return exceeds BTC by at least 2%.
- EMA20 is above EMA60 and both are non-declining.
- Price is no more than 1.5 ATR above EMA20.
- Average quote volume over the latest three bars is at least 1.8 times the preceding
  twelve-bar average.
- The latest close breaks the prior 20-bar high while preserving a higher low.

## Derivatives Flow

Technically qualified candidates must also pass:

- Fifteen-minute open-interest value change of at least 3%.
- Fifteen-minute taker buy/sell volume ratio of at least 1.2.
- Funding rate no greater than 0.05%.
- Top-trader position long/short ratio no greater than 2.0.

Flow data is cached for 60 seconds. A missing or malformed flow response rejects the
candidate instead of silently bypassing the filter.

## Entry State Machine

1. Register the completed breakout bar and its prior structure level.
2. Wait for price to pull back at least 0.5 ATR from the breakout close.
3. Invalidate the setup below the breakout level minus 0.3 ATR.
4. Require two complete 5-minute closes above the breakout level after the touch.
5. Recheck EMA distance and open PAPER long only when the structural stop is within
   the configured 1.2%-3.0% price-distance range.

Setups expire after 30 minutes. This deliberately misses moves that never offer a
controlled first pullback.

## Initial Risk And Exit

The production example starts with:

- Margin: 100 USDT
- Leverage: 3x
- Planned risk ceiling: 10 USDT
- Maximum PAPER positions: 2
- Maximum strategy daily PAPER loss: 20 USDT

Management is expressed in initial R:

| Best progress | Protection |
| --- | --- |
| `0.6R` | Reduce remaining risk to `0.25R` |
| `1R` | Lock `0.1R` |
| `1.5R` | Lock `0.75R` |
| `2R` | Close 30% and lock at least `1R` |
| `3R` | Trail by `1R` |
| `5R` | Tighten the trail to `0.5R` |

A position still losing after four hours is closed by the time stop.

## Evaluation

The entry metrics stored in MySQL include short-term returns, relative BTC strength,
volume expansion, open-interest change, taker ratio, funding, top-trader positioning,
breakout structure, pullback depth, confirmation closes, and stop distance. PAPER
results should be evaluated using net PnL, MFE, MAE, and the percentage reaching 1R
and 2R before considering LIVE mode.
