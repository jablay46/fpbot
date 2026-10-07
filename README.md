# mfpbot — Trading bot for MyFundedPerps

A small, dependency-light Python bot that trades the MyFundedPerps
(myfundedperpetuals.com) developer API. It streams live market data over
WebSocket for **many assets at once** (crypto, stocks, indices, commodities,
forex), evaluates an EMA-crossover strategy on closed candles per market, sizes
each position from a fixed risk budget, and manages the trade with a
broker-side take-profit and stop-loss.

> Status: the MyFundedPerps developer API is in **beta**. Endpoints and fields
> may change. This bot is a working reference, not a production trading system.
> Trade a sandbox account first.

## What the API allows

MyFundedPerps is a perpetual-futures prop firm. Its REST API can trade
**existing challenge accounts** (live) or free **sandbox accounts** (test), but
it does **not** expose signup, purchases, payouts, or API-key management.
You create the account and the API key in the website UI; the bot uses the key.

* Live keys (`fp_live_...`) → `https://developers.myfundedperpetuals.com`
* Test keys (`fp_test_...`) → `https://sandbox.myfundedperpetuals.com`
* Market data → `wss://api-stream.myfundedperpetuals.com/v1/market-data` (public, no key)

The bot deliberately avoids API-key creation; that is a sensitive, browser-only
step and stays with you.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # or: pip install -e .
```

## Configure

```bash
cp .env.example .env
# edit .env and set FP_API_KEY (a fp_test_ key to start) and FP_ACCOUNT_ID
```

Key settings (see `.env.example` for all of them):

| Variable | Meaning |
| --- | --- |
| `FP_API_KEY` | Sandbox (`fp_test_`) or live (`fp_live_`) API key |
| `FP_ENV` | `sandbox` (default) or `live` |
| `FP_ACCOUNT_ID` | Challenge account to trade; empty = first active account |
| `FP_SYMBOLS` | Comma-separated market IDs to trade (multi-asset). Max 32 |
| `FP_MARKET_ID` | Single market fallback used when `FP_SYMBOLS` is empty |
| `FP_TIMEFRAME` | Candle interval, e.g. `15m` |
| `FP_RISK_PER_PCT` | Percent of equity risked between entry and stop |
| `FP_ATR_STOP_MULT` | Stop distance as a multiple of ATR |
| `FP_TP_RR` | Take profit as a multiple of the stop distance |
| `FP_LEVERAGE`, `FP_MARGIN_MODE` | Order leverage and `cross`/`isolated` |
| `FP_MAX_DAILY_LOSS_PCT`, `FP_MAX_DAILY_TRADES` | Bot-side daily guards |
| `FP_MIN_DAILY_ROOM_PCT` | Stop/flatten when account loss room falls below this % of starting balance |
| `FP_MAX_MARGIN_PCT` | Cap total position margin across markets as a percent of equity |
| `FP_FLATTEN_SCOPE` | `bot` closes only bot-opened positions; `account` closes all |
| `FP_DRY_RUN` | Log intended orders without sending them |

## Use

```bash
# Discover markets (public, no key needed)
python -m mfpbot markets --filter BTC

# Confirm credentials, account, market and a live quote
python -m mfpbot accounts
python -m mfpbot check

# Run without placing orders first
python -m mfpbot run --dry-run

# Trade
python -m mfpbot run

# Show open positions
python -m mfpbot positions
```

## Multi-asset trading

Set `FP_SYMBOLS` to a comma-separated list of market IDs to trade many assets
in one run:

```bash
FP_SYMBOLS=binance|BTCUSDT,binance|ETHUSDT,binance|SOLUSDT,binance|XRPUSDT,\
hyperliquid|xyz:AAPL,hyperliquid|xyz:SP500,binance|XAUUSDT,hyperliquid|xyz:EUR
```

The catalog spans five asset classes (all perpetuals): crypto (`binance|*USDT`),
stocks (`hyperliquid|xyz:AAPL`, `xyz:MSFT`, …), indices (`xyz:SP500`,
`xyz:XYZ100`, `xyz:JP225`), commodities (`binance|XAUUSDT`, `xyz:GOLD`,
`xyz:SILVER`, `binance|NATGASUSDT`, `binance|CLUSDT`), and forex (`xyz:EUR`,
`xyz:JPY`). Run `python -m mfpbot markets` to list every valid ID.

How it works:

* All markets share **one WebSocket connection**, but subscriptions are grouped
  **per provider** (`binance` vs `hyperliquid`). A subscription that lists
  several providers resolves its symbols against the first provider, so mixing
  venues in one frame is rejected as an unknown market; grouping avoids that.
* The strategy runs independently per market. One position per market; a signal
  in one market never blocks another.
* Portfolio-level daily guards are shared: `FP_MAX_DAILY_TRADES` counts entries
  across all markets, and the daily-loss / drawdown checks use account equity.
* Position sizing subtracts margin already reserved by other open positions, so
  concurrent entries do not over-commit the account.
* When several signals land on the same candle, markets are processed in a
  rotating order so none starves the others.

## Strategy

The default `ema_cross` strategy:

1. Builds EMA(fast) and EMA(slow) plus Wilder ATR from **closed** candles.
2. Fires a **long** signal when EMA(fast) crosses above EMA(slow), and a
   **short** signal when it crosses below. Signals fire only on the crossing
   candle, so the bot acts once per cross.
3. Places a market order with an attached take profit (`entry + rr * stop
   distance`) and stop loss (`entry - atr_stop_mult * ATR`), sized so the loss
   to the stop equals `FP_RISK_PER_PCT` of equity.

If a signal reverses an existing position, the bot closes the position and
opens the new one. The stop and take profit live on the broker side, so they
trigger even if the bot is offline.

## Risk controls

* **Position sizing** — loss between entry and stop is capped at the risk
  budget; size is rounded down to the market's `size_step` and rejected below
  `min_size` / `min_notional`.
* **Daily guards** — max entries per UTC day and max daily loss measured against
  the day's starting equity.
* **Account guards** — reads the API risk snapshot (`daily_loss_room`,
  `max_drawdown_room`). When room falls below `FP_MIN_DAILY_ROOM_PCT` of the
  starting balance, the bot flattens positions and halts.
* **Portfolio margin cap** — total position margin across all markets cannot
  exceed `FP_MAX_MARGIN_PCT` of equity; new entries are skipped once it is hit.
* **Manual positions are safe** — the bot tracks the positions it opened and
  never closes or reverses a position it did not create. Flattening defaults to
  `bot` scope; set `FP_FLATTEN_SCOPE=account` to close the whole account.
* **Account state** — if the account is `failed` or `closed`, the bot stops.
* **Idempotency** — every create/close uses an `Idempotency-Key`; entries also
  carry a `client_order_id` for reconciliation after a lost response.

## Reliability

* The market stream reconnects with exponential backoff and re-subscribes with
  a retained history limit so gaps are backfilled.
* `429`/`5xx` responses are retried with jittered backoff and `Retry-After`.
* State (daily counters, last processed candle, last entry ID) is persisted to
  `FP_STATE_FILE` so a restart does not double-count entries.

## Tests

```bash
python -m pytest -q
```

Tests run against a **local HTTP stub server** (real sockets, real client code)
plus unit tests for indicators, strategy, sizing, risk, config and state.

## Layout

```
mfpbot/
  cli.py            command-line interface
  client.py         REST client (auth, retries, idempotency)
  market_stream.py  public WebSocket candle/price stream
  bot.py            orchestration: reconcile, signal, size, order
  strategy/         indicators + EMA-cross strategy
  risk/             sizing + daily/account guards
  state.py          persisted state
```

## Going live safely

The bot can trade a live challenge account, but go in this order:

1. Set `FP_ENV=sandbox` with a `fp_test_` key and confirm signals and orders on
   the free sandbox account first.
2. Run live with `FP_DRY_RUN=true` to see the intended entries against real
   quotes without sending orders.
3. Start live with **one** market, small `FP_RISK_PER_PCT` (e.g. `0.25`), and a
   low `FP_MAX_DAILY_TRADES`.
4. Only then widen `FP_SYMBOLS` and risk.

Sizing uses the account risk snapshot, so a live challenge account's daily-loss
and max-drawdown floors are respected. The bot still cannot guarantee profit;
challenge rules can fail the account on a bad day.

### Loading the API key

Put the key directly in `.env` (`FP_API_KEY=...`); `.env` is git-ignored and the
bot never logs it. If you receive the key in a text file, paste just the key
value into `.env` and delete the text file. Never commit the key or paste it
into chat.

## Security

Never commit `.env` or an API key. The bot reads the key from the environment
and never logs it. Use a scoped key (single account, read+trade) and revoke it
from the website when you are done.
