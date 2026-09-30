# bn-stra-copy-lead-1

## Identity

- Strategy ID: `bn-stra-copy-lead-1`
- Direction: follows the lead order
- Type: event-driven Binance lead-portfolio copier
- Risk candle interval: 5 minutes
- Version: `2.4.0`
- Implementation: `strategies/copy_lead.py`

The strategy reads order history from Binance's public lead-portfolio web endpoint.
It does not generate entries from technical indicators.

## Event Mapping

Lead orders are mapped as follows:

| Lead position side | Lead order side | Event |
| --- | --- | --- |
| LONG | BUY | Open long |
| LONG | SELL | Close long |
| SHORT | SELL | Open short |
| SHORT | BUY | Close short |

Every event receives a deterministic identity based on symbol, sides, quantity,
price, order time, and update time. Persisted identities prevent replay after restart.

## Entry Filters

An open event is ignored when:

- It predates `event_not_before_ms`.
- Its age exceeds `max_entry_delay_seconds`.
- Current price differs from the lead fill by more than
  `max_lead_price_deviation`.
- Lead notional is below `min_lead_notional`.
- The symbol is blocked or not present in a non-empty allowlist.
- The event has already been processed in the same run mode.

The score starts at 100 and decays by five points per minute of event age, with a
floor of zero. The stored entry reason is `lead_open`.

## Position Size

The signal may request `margin_per_trade`, but the shared strategy risk configuration
is authoritative for fixed margin and leverage. Planned loss, available margin,
strategy position count, and global allocation limits still apply.

## Initial Stop

The strategy calculates a 14-period true-range average from 5-minute candles and
uses `ATR * stop_atr_multiplier`. Price distance is capped by
`stop_max_roi / leverage`. If ATR cannot produce a positive distance, the fallback is
the smaller of the ROI cap and 2% price distance.

## Exit Behavior

- A matching lead close event closes the copied position.
- The local exchange-hosted stop remains active even if the public lead endpoint is
  unavailable.
- At `0.5R`, the local stop reduces remaining risk to `0.2R`.
- At `1R`, the stop moves beyond entry by the configured cost buffer.
- At `1.5R`, the stop locks `0.75R`.
- From `2R`, the stop trails one initial R behind the best price.
- The strategy has no partial take-profit; the full position remains until a lead
  close, local protected stop, or initial stop.
- A local stop crossing closes with `stop_loss`.

After a same-direction loss, a new lead open for that symbol is consumed but not
traded during `loss_cooldown_minutes`. Reaching
`max_symbol_stop_losses_per_day` blocks that symbol and direction for the rest of
the Asia/Shanghai day.

## Operational Limitations

- The lead history endpoint is a public Binance web API, not the official signed
  Futures trading API, and may change or become unavailable.
- Polling can observe an order after the lead's fill; the 120-second production
  maximum-entry-delay gate and 0.5% price-deviation gate are therefore essential.
- The follower enters at its own current market and bounded IOC price, not at the
  lead's historical fill price.
- Partial lead events are interpreted from the event stream; deduplication depends on
  the order fields supplied by Binance.
- Set `event_not_before_ms` before switching from PAPER to LIVE to prevent historical
  events inside the lookback window from becoming new live entries.
