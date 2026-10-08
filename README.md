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
| `FP_MAX_TOTAL_DRAWDOWN_PCT`, `FP_DRAWDOWN_BASIS` | Cumulative (whole-account) drawdown limit and its basis (`starting` or trailing `peak`); `0` disables |
| `FP_ACK_NO_DRAWDOWN_GUARD` | Required to run live `bot-only` with no cumulative guard set |
| `FP_ALLOW_FRESH_STATE` | Allow a live run to start from empty state if the file and its `.bak` are unreadable |
| `FP_MIN_DAILY_ROOM_PCT` | Stop/flatten when account loss room falls below this % of starting balance |
| `FP_MAX_MARGIN_PCT` | Cap total position margin across markets as a percent of equity |
| `FP_MAX_ENTRY_DRIFT_ATR` | Max quote-vs-close drift to enter, as a multiple of ATR (5% absolute cap on top) |
| `FP_FLATTEN_SCOPE` | `bot` closes only bot-opened positions; `account` closes all |
| `FP_ON_MISSING_ROOM` | `halt` (fail closed) or `bot-only` when the risk snapshot has null room; empty = halt on live, bot-only on sandbox |
| `FP_DRY_RUN` | Log intended orders without sending them (uses a separate `<FP_STATE_FILE>.dryrun` state) |

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

Three strategies share one interface (`mfpbot/strategy/`), selected with
`FP_STRATEGY`:

* `ema_cross` (default) — long/short when EMA(fast) crosses EMA(slow). Fires
  only on the crossing candle.
* `donchian_breakout` — enters when a closed candle closes beyond the previous
  `FP_DONCHIAN_PERIOD`-bar high/low, with an optional ADX regime floor
  (`FP_REGIME_ADX_MIN`) and EMA trend filter (`FP_TREND_EMA`). Fewer, less
  whipsaw-prone entries than an EMA cross.
* `supertrend` — an ATR trailing band; signals on the flip. Calmest of the
  three in a range.

All three size the stop from `FP_ATR_STOP_MULT * ATR` (Supertrend uses its own
band line) and set the take profit at `FP_TP_RR` times the stop distance, so
position sizing and the broker-side exits work identically. The stop and take
profit live on the broker side, so they trigger even if the bot is offline.

An ATR-based stop plus a fixed reward:risk means the risk per trade is a
constant fraction of equity regardless of volatility. Combined with a trend
filter, that is what keeps the account drawdown guard (below) from ever firing.

**Which strategy is better is an empirical question — measure it, do not guess.**
See the next section.

## Backtesting and edge

The REST API has no historical-candle endpoint, but the public WebSocket does
expose a `candles.history` request (`MarketDataStream.fetch_history`). Two ways
to get a dataset:

```bash
# A) Record live candles from the stream to a JSONL archive (run alongside the
#    bot, or on its own). Resumable and de-duplicated by bar open time.
python -m mfpbot archive --symbols binance|BTCUSDT --timeframe 15m --out data/btc15m.jsonl

# B) Or use any OHLCV CSV/JSONL you already have (e.g. an exchange export).
```

Then compare every strategy over the same bars, net of the firm's published
costs, and validate out-of-sample:

```bash
python -m mfpbot backtest --bars data/btc15m.jsonl \
  --equity 100000 --risk-pct 0.5 \
  --max-daily-loss-pct 3 --max-total-drawdown-pct 3 \
  --walk-forward 5
```

The reported metrics are all **after** costs:
commission (crypto 0.03%/fill), hourly swap (crypto 0.03%/day) and adverse
slippage (default 1.2 bps/side), matching
`docs.myfundedperpetuals.com/guides/commissions-and-fees` and
`/guides/trading-guide`. `--no-costs` shows the gross picture for contrast.

Read the output this way:

* `MAR` (CAGR / max drawdown) is the headline for a prop account — you are
  judged on drawdown, not on headline return.
* `profitable_folds` from `--walk-forward` is the reality check. A strategy that
  only wins in-sample is overfit; require most folds to be positive.
* Compare against `ema_cross` as the baseline that a replacement must beat.

The engine (`mfpbot/backtest/engine.py`) applies the same daily-loss,
cumulative-drawdown and daily-trade guards as the live bot, so a strategy is
measured under the constraints it will actually trade under. When both the stop
and the take profit fall inside one bar it assumes the stop filled first
(conservative).

## Two accounts: execution and copy trading

MyFundedPerps has **native copy trading** within one owner's accounts
(`docs.myfundedperpetuals.com/guides/copy-trading-guide`). Configure it in the
website UI: one **lead** and one or more **followers**, each with a multiplier
(0.1x–2x). Follower size is normalized by starting balance, so a $2.5K follower
takes the $100K lead's position at ~1/40th size automatically.

```bash
# Run the bot only on the lead; configure the follower via Settings → Copy Trading.
FP_ACCOUNT_ID=FP-94193894 FP_STRATEGY=donchian_breakout python -m mfpbot run
```

Two cautions, straight from the docs:

* A follower account is **locked against manual trading** while its group is
  enabled. Do not point the bot at both accounts.
* Every follower order is re-validated against **its own** rules and balance, so
  it can reject or shrink a copy. The $2.5K account's 3% daily allowance is only
  $75 — the lead's risk per trade must be sized so that a normal losing day does
  not breach the follower's 3% daily limit before it breaches the lead's.
  Keeping **risk per trade at or below ~1%** and **total exposure well under the
  daily cap** is what keeps the follower inside its own rules.
* Copying the same owner's accounts is expected. The **October Competition**
  separately forbids mirroring *across different users* and disqualifies it; do
  not point this bot at a competition account that the rules do not permit.

## Risk controls

* **Position sizing** — loss between entry and stop is capped at the risk
  budget; size is rounded down to the market's `size_step` and rejected below
  `min_size` / `min_notional`.
* **Daily guards** — max entries per UTC day and max daily loss measured against
  the day's starting equity. The baseline is the first equity seen that day, not
  exactly 00:00 UTC; the bot logs a warning when it has to estimate it.
* **Account guards** — reads the API risk snapshot (`daily_loss_room`,
  `max_drawdown_room`). When room falls below `FP_MIN_DAILY_ROOM_PCT` of the
  starting balance, the bot flattens positions and halts. If the snapshot has
  no room figures at all, the bot fails closed (`FP_ON_MISSING_ROOM=halt`) unless
  it is explicitly allowed to keep going on bot-side caps (`bot-only`).
* **Cumulative drawdown guard** — the competition account reports null room
  figures, so with `FP_ON_MISSING_ROOM=bot-only` nothing beyond the daily cap
  (which resets every UTC day) would stop a multi-day slide. Set
  `FP_MAX_TOTAL_DRAWDOWN_PCT` to cap the whole-account drawdown from
  `FP_DRAWDOWN_BASIS` (`starting`, the default, or the trailing `peak`). It
  warns at 50% and 80% of the limit and, once breached, sets a sticky halt that
  survives day rollover and restarts until released with
  `python -m mfpbot reset-halt --yes`. A live `bot-only` run with no guard must
  be acknowledged with `FP_ACK_NO_DRAWDOWN_GUARD=true` or it refuses to start.
* **Kill switch runs even while holding a position** — a watchdog polls the
  account between candles, so the daily loss cap and room floor fire without
  waiting for the next signal. It keeps running for the life of the bot (the
  halt clears on the next UTC day) and, while halted, reconciles any unconfirmed
  entry and retries flattening until every bot-owned position is confirmed closed.
* **Portfolio margin cap** — total position margin across all markets cannot
  exceed `FP_MAX_MARGIN_PCT` of equity; new entries are skipped once it is hit.
* **Manual positions are safe** — the bot tracks the positions it opened and
  never closes or reverses a position it did not create. Flattening defaults to
  `bot` scope; set `FP_FLATTEN_SCOPE=account` to close the whole account.
* **Account state** — if the account is `failed` or `closed`, the bot stops.
* **Idempotency** — every create/close uses an `Idempotency-Key`; entries also
  carry a `client_order_id` for reconciliation after a lost response. The lookup
  scans the full order list (paging via the cursor) for the exact id rather than
  trusting the first row, and retries without the server filter if it is empty,
  so a misbehaving server can never hide a live in-flight entry and trigger a
  duplicate.

## Reliability

* The market stream reconnects with exponential backoff and re-subscribes with
  a retained history limit so gaps are backfilled.
* Replayed history (the candle snapshot sent on every start/reconnect) is never
  traded: only candles that closed within the last interval are acted on.
* `429`/`5xx` responses are retried with jittered backoff and `Retry-After`. A
  transient error while handling a fresh candle is retried, and the candle is
  only marked processed once it is actually handled.
* Blocking REST work runs off the WebSocket event loop, and the state lock is
  never held across network I/O. The kill-switch watchdog therefore fires
  promptly even while a candle handler is mid-request, and a kill that lands
  mid-entry stops the order from being sent.
* State (daily counters, last processed candle, last entry ID, ownership and
  pending entries) is persisted to `FP_STATE_FILE` so a restart does not
  double-count entries or drop a position. Saves are serialized under a lock and
  written via a unique tempfile with `fsync`, keeping a `<state>.bak` copy. An
  entry whose reply was lost is recorded before sending and reconciled on
  startup, on the watchdog tick, or on the next candle. If a live, non-dry-run
  start finds the file and its backup both unreadable, it refuses to start
  (exit 2) rather than silently trade from empty state, unless
  `FP_ALLOW_FRESH_STATE=true`.

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
  market_stream.py  public WebSocket candle/price stream (+ candles.history)
  archiver.py       record closed candles to a JSONL dataset
  bot.py            orchestration: reconcile, signal, size, order
  strategy/         indicators + ema_cross / donchian_breakout / supertrend
  backtest/         data loader, cost model, engine, metrics, walk-forward
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
