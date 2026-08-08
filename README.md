# bn-stra-high-risk-1

`bn-stra-high-risk-1` is a high-risk Binance USD-M Futures trading bot. It targets volatile perpetual contracts and uses trend, volatility, pullback, and reversal-confirmation filters before opening a market position.

The example configuration defaults to `dry_run: true` and does not place real orders. Binance Futures Testnet may not list the configured high-volatility symbols, so use supported symbols when testing order execution.

## Risk warning

This strategy can lose money quickly. It uses leverage, market orders, conditional stop orders, and dynamic stop replacement. Network latency, API errors, slippage, funding fees, liquidation rules, exchange outages, and symbol-specific trading limits can materially change results.

The current sizing rule allocates account margin across the configured symbol count. With five symbols, each new position uses approximately 20% of current account equity as margin. Do not run it with funds you cannot afford to lose.

## Default symbols

```text
BICOUSDT
TUTUSDT
GWEIUSDT
EPICUSDT
CAPUSDT
```

## Signal filters

```text
K-line interval: 5m
EMA20 > EMA60: long only
EMA20 < EMA60: short only
ADX14 > 30: entry allowed
0.5% <= ATR14 / close <= 4%: entry allowed
Otherwise: no entry
```

## Entry flow

The bot does not immediately chase a trend signal.

```text
Long pullback: current price <= signal close * 0.996
Short pullback: current price >= signal close * 1.004
Long confirmation: price rebounds 0.4% from the pullback low
Short confirmation: price falls 0.4% from the pullback high
Signal window: 300 seconds
Order type after confirmation: MARKET
```

When the signal window expires, the bot starts a new window from the latest qualifying signal.

## Position sizing

```text
Margin per position = current USDT totalMarginBalance / configured symbol count
Leverage = 5x
Initial stop = -10% margin ROI, approximately a 2% adverse price move
```

For example, with `1,500 USDT` of equity and five configured symbols, each new position uses approximately `300 USDT` of margin and controls approximately `1,500 USDT` of notional exposure.

The legacy `allocation_fraction` field is accepted for configuration compatibility but does not control live position margin.

## Exit management

```text
Breakeven trigger = +15% margin ROI, approximately a 3% favorable price move
Trailing activation = +15% margin ROI
Trailing callback = 2% of price
```

At the trigger, the bot moves the stop beyond entry by `fee_rate * 2` and activates trailing management. With the default `fee_rate=0.0004`, the breakeven stop is approximately `entry * 1.0008` for a long and `entry * 0.9992` for a short. Actual fills may still lose money because of slippage or higher fees.

## Risk controls

```text
high_vol mode: maximum 2 initial stop losses per symbol per day
Cooldown after any position closes: 30 minutes
Position mode: one-way mode with positionSide=BOTH
Stop trigger source: MARK_PRICE
```

Breakeven and trailing-stop exits do not increment the daily initial-stop counter.

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

`config.json` and `.env` are ignored by Git and must never be committed.

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
- Always verify the protective stop after a live entry.
