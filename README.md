# bn-stra-high-risk-1

`bn-stra-high-risk-1` is a high-risk Binance USD-M Futures trading bot. It targets volatile perpetual contracts and uses trend, volatility, pullback, and reversal-confirmation filters before opening a position.

The example configuration defaults to `dry_run: true` and does not place real orders. Binance Futures Testnet may not list the configured high-volatility symbols, so use supported symbols when testing order execution.

## Risk warning

This strategy can lose money quickly. It uses leverage, IOC limit entry orders, market exit orders, conditional stop orders, and dynamic stop replacement. Network latency, API errors, slippage, funding fees, liquidation rules, exchange outages, and symbol-specific trading limits can materially change results.

The configured margin is `200 USDT` per new position. ATR affects entry eligibility and stop distance, but never reduces the configured margin. Do not run it with funds you cannot afford to lose.

## Default symbols

```text
BMTUSDT
龙虾USDT
ACTUSDT
CAPUSDT
NILUSDT
BOMEUSDT
COAIUSDT
TUTUSDT
ESPUSDT
MUBARAKUSDT
GUAUSDT
BLUAIUSDT
CYSUSDT
SQDUSDT
PROMUSDT
BSPUSDT
BLESSUSDT
```

## Signal filters

```text
K-line interval: 5m
EMA20 > EMA60: long only
EMA20 < EMA60: short only
ADX14 > 30: entry allowed
ADX14 must be flat or rising versus the previous closed candle
0.5% <= ATR14 / close <= 6%: entry allowed
Otherwise: no entry
Price distance from EMA20 must not exceed 1.5 ATR
```

The bot also rejects a short when the global account long/short ratio is at or below `0.65` while the top-trader position ratio is at or above `1.20`. The inverse crowded-long condition rejects longs when the global ratio is at or above `1.55` and the top-trader ratio is at or below `0.83`.

An independent top-trader veto also rejects longs when the top-trader position ratio is at or below `0.80`, and rejects shorts when it is at or above `1.25`.

## Entry flow

The bot does not immediately chase a trend signal.

```text
Long pullback: current price <= signal close * 0.996
Short pullback: current price >= signal close * 1.004
Long confirmation: price rebounds by max(0.4%, ATR percentage * 0.15) from the pullback low
Short confirmation: price falls by max(0.4%, ATR percentage * 0.15) from the pullback high
Signal window: 300 seconds
Long recross: a complete 1-minute candle formed after reversal confirmation closes at or above the original signal close
Short recross: a complete 1-minute candle formed after reversal confirmation closes at or below the original signal close
The current mark price must remain on the confirmed side when the entry is evaluated
Maximum adverse entry distance from the original signal = min(0.3%, ATR percentage * 0.25)
Invalidate the signal instead of chasing when the entry distance exceeds that limit
Closed-candle confirmation window: 180 seconds
Invalidate the current signal when adverse pullback exceeds 1.5 ATR
Entry order: LIMIT IOC capped at 0.20% beyond the latest confirmation mark price
```

When the signal window expires, the bot starts a new window from the latest qualifying signal.

## Position sizing

```text
Base margin per position = configured margin_per_trade (default 200 USDT)
Leverage = 5x
Initial stop distance = min(2% of price, 1.5 * ATR percentage)
ATR never changes margin or order quantity
```

At full size, `200 USDT` of margin with 5x leverage controls approximately `1,000 USDT` of notional exposure.

The entry limit is calculated from the latest mark price used for confirmation. A long may fill no higher than `mark * 1.002`; a short may fill no lower than `mark * 0.998`, adjusted conservatively to the symbol tick size. An IOC order cancels any unfilled quantity immediately. A zero fill abandons the signal, while a partial fill is retained at its actual size and receives a matching protective stop.

The legacy `allocation_fraction` field is accepted for configuration compatibility but does not control live position margin. Before an order is placed, the bot checks Binance `availableBalance`. If it cannot cover the configured margin plus an opening-fee allowance, the order is skipped and a rate-limited Hermes notification is queued.

The configured `stop_loss_roi` is a hard initial-loss cap before fees and slippage. With the default `10%` ROI cap, `200 USDT` margin risks at most approximately `20 USDT` before fees and slippage. ATR may tighten the stop but cannot widen it beyond that cap.

## Exit management

```text
Breakeven trigger = +12% margin ROI, approximately a 2.4% favorable price move
Close 30% of the original quantity at +12% margin ROI
Profit locked at trigger = +5% margin ROI, approximately a 1.0% favorable price move
Trailing activation = +25% margin ROI, approximately a 5% favorable price move
Close another 25% of the original quantity at +40% margin ROI
Trail the remaining 70% position before +40% ROI and 45% afterward
Trailing callback at +25% ROI = 2% of price
Trailing callback at +40% ROI = 1.5% of price
Trailing callback at +60% ROI = 1% of price
```

At the first profit trigger, the bot realizes 30% of the original position and moves the remaining position's stop to the configured `profit_lock_roi` instead of nominal breakeven. With 5x leverage and `profit_lock_roi=0.05`, the stop is placed approximately 1.0% beyond entry. This buffer is intended to absorb taker fees and moderate stop-market slippage, but unusually thin order books can still reduce the result. Trailing management starts only after the separate trailing activation threshold is reached. Its callback tightens as the best margin ROI reaches each tier, and a reached tier never loosens the current stop.

ATR above 6% blocks new entries; ATR within the allowed range does not change position size. Managed trailing stops are replaced only when the stop improves by at least 0.2%.

## Risk controls

```text
high_vol mode: maximum 2 initial stop losses per symbol per day
Global limit: pause new entries after more than 5 confirmed initial stop losses across all symbols per Asia/Shanghai day
Concurrent exposure: maximum 3 open positions
Cooldown after any position closes: 30 minutes
Position mode: one-way mode with positionSide=BOTH
Stop trigger source: MARK_PRICE
```

Only an initial protective stop that Binance confirms as triggered or finished increments the stop counters. Manual closes, unconfirmed closes, breakeven exits, and trailing-stop exits do not count. The sixth confirmed initial stop pauses real entries until the next Asia/Shanghai day while existing positions continue to be protected and managed. A Hermes notification is sent once when the limit is exceeded. The global count and notification state are persisted and the count is reconstructed from per-symbol counts when an older state file is loaded.

When `paper_trading_only` is enabled, every qualified entry becomes a clearly labeled paper position and no Binance entry order is submitted, regardless of daily stop counters or day changes. `paper_signals_after_global_stop` provides the narrower fallback mode when live trading is enabled but the global limit has paused real orders. Paper positions use the configured fixed margin and leverage and follow the same stop cap, partial exits, profit lock, and trailing-stop tiers. They survive service restarts and produce only `PAPER OPEN - NO REAL ORDER` and `PAPER CLOSED - NO REAL ORDER` Hermes messages. Estimated paper PnL includes configured trading commissions but treats funding as zero.

## Hermes notifications

The bot can enqueue open and close notifications through a local `hermes-work` Unix socket. Open notifications include position sizing and protection levels. Close notifications query Binance income history and report realized PnL, commission, funding, net PnL, margin ROI, and holding duration. Partial take-profit executions remain in the service log and do not generate Hermes messages.

```json
"hermes_enabled": true,
"hermes_socket_path": "/home/inkb/apps/hermes-work/hermes-work.sock",
"hermes_target": "weixin"
```

Hermes delivery failures are logged but never interrupt order or risk-management processing. Runtime state persists each trade's opening time and initial margin so PnL summaries survive normal service restarts.

## Requirements

- Python 3.11 or later is recommended.
- A Binance USD-M Futures account is required for live trading.
- The account must use one-way position mode.
- API credentials need Futures trading permission but should not have withdrawal permission.
- Restrict the API key to the server IP whenever possible.

## Configuration

Create a local runtime configuration:

```bash
cp config.example.json config.json
```

`config.json`, `.env`, and the runtime state file are ignored by Git and must never be committed.

Run one dry-run scan:

```bash
python3 bn_stra_high_risk_1.py --config config.json --once --log-level INFO
```

Run continuously:

```bash
python3 bn_stra_high_risk_1.py --config config.json --log-level INFO
```

## Testnet

Change `symbols` to contracts available on Binance Futures Testnet, then configure:

```json
{
  "dry_run": false,
  "testnet": true
}
```

Set credentials through environment variables or `.env`:

```bash
BINANCE_API_KEY="testnet-api-key"
BINANCE_API_SECRET="testnet-api-secret"
```

## Mainnet

Mainnet order execution requires:

```json
{
  "dry_run": false,
  "testnet": false
}
```

Never store real credentials in `config.json` or commit them to source control.

## systemd service

Install the user service from the expected server directory:

```bash
cd /home/inkb/bn-stra-high-risk-1
chmod +x install_user_service.sh run_once.sh
./install_user_service.sh
```

Manage the service:

```bash
systemctl --user start bn-stra-high-risk-1
systemctl --user status bn-stra-high-risk-1
journalctl --user -u bn-stra-high-risk-1 -f
systemctl --user restart bn-stra-high-risk-1
systemctl --user stop bn-stra-high-risk-1
```

To keep the user service running after logout and start it after reboot, an administrator must run:

```bash
loginctl enable-linger inkb
```

## Files

```text
bn_stra_high_risk_1.py         Live bot entry point
config.example.json            Default dry-run configuration
config.high-risk.json          High-risk configuration template
bn-stra-high-risk-1.service    systemd user service
install_user_service.sh        systemd installation helper
run_once.sh                    One-scan launcher with .env loading
test_bn_stra_high_risk_1.py    Unit tests
```

## Operational notes

- The bot polls symbols sequentially at the configured interval.
- Protective stops are submitted through the Binance Algo Order API.
- Trailing behavior is implemented by canceling and replacing conditional stop orders.
- Restarting while positions are open can lose in-memory stop-management state. Prefer restarting only when the account is flat.
- Per-symbol and global daily initial-stop counts are persisted in `.bn-stra-high-risk-1-state.json` and restored after a restart.
- Existing account positions outside the configured entry symbols remain monitored until they close; the bot will not reopen them.
- The daily stop-limit message is logged once per symbol per day instead of once per polling cycle.
- Always verify the protective stop after a live entry.
