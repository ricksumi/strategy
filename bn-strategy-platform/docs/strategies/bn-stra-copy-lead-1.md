# bn-stra-copy-lead-1

## Identity

- Strategy ID: `bn-stra-copy-lead-1`
- Direction: follows the lead order
- Type: event-driven Binance lead-portfolio copier
- Risk candle interval: 5 minutes
- Version: `2.5.0`
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
The identity format is unchanged from version 2.4.0.

## Source Collection and Audit

The production PAPER configuration permits a maximum entry delay of 300 seconds,
while keeping the 0.5% lead-price deviation limit. The shared risk configuration
still limits the copier to one open position. Age is checked again before execution.
The generic settings default remains 120 seconds so an unconfigured LIVE deployment
does not silently receive a wider entry window.

Order history is paginated with a fixed start/end window, up to `max_pages` (10 in
production). Collection has a 15-second request budget, individual requests use at
most a five-second timeout, and transient network, HTTP 429/5xx, or Binance busy
responses receive at most one retry. Already collected pages are retained if a
later page fails; repeated pages and truncation are logged. No historical order
is given a newer timestamp to make it eligible.

Every returned order is upserted into `strategy_lead_orders` before entry filters
or market-data lookup. The table stores:

- Strategy ID/version, run mode, portfolio ID, and stable event identity.
- Original JSON, order/update timestamps, first/last observation times, and the
  delay when the page containing the order was first received.
- Processing decisions, reasons, and metrics such as age and price deviation.

Repeated observations preserve the original first-seen timestamp. Processed
orders retain their terminal decision; retried orders expose their latest decision.
Previously processed events remain deduplicated after upgrading or restarting.
Old records from `strategy_external_events` are not backfilled with invented
first-seen times. The audit can only describe orders actually returned by Binance;
it cannot prove that the public endpoint exposes every lead trade.

Decision reasons distinguish stale orders, pre-cutover orders, invalid data,
symbol filters, small lead notionals, price deviation, existing positions,
allocation limits, daily limits, cooldowns, and execution/market-data errors.
The raw table is not a trade ledger and should not be summed as trading PnL.

```sql
SELECT run_mode, decision, decision_reason, COUNT(*) AS orders,
       ROUND(AVG(first_seen_delay_ms) / 1000, 2) AS average_first_seen_delay_seconds
FROM strategy_lead_orders
WHERE strategy_name = 'bn-stra-copy-lead-1'
GROUP BY run_mode, decision, decision_reason;
```

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
- Polling can observe an order after the lead's fill. The production PAPER
  maximum-entry-delay gate is 300 seconds and the price-deviation gate remains 0.5%.
- Failure in this event strategy is logged without aborting other strategy scans.
  Position protection is still managed before event collection on each cycle.
- The follower enters at its own current market and bounded IOC price, not at the
  lead's historical fill price.
- Partial lead events are interpreted from the event stream; deduplication depends on
  the order fields supplied by Binance.
- Set `event_not_before_ms` before switching from PAPER to LIVE to prevent historical
  events inside the lookback window from becoming new live entries.
