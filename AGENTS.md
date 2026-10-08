# AGENTS.md

Repository memory for the `mfpbot` project. Keep this current when the API or
project conventions change.

## What this is

An automated trading bot for the **MyFundedPerps** (myfundedperpetuals.com)
developer REST API. Pure Python, no framework. It streams market data over a
public WebSocket, evaluates an EMA-crossover strategy, sizes positions from a
risk budget, and places orders with broker-side TP/SL.

## Environment and commands

* Python 3.10+ (developed on 3.13). Dependencies: `requests`, `websockets`
  (pinned in `requirements.txt`); tests use `pytest` and `pip-audit`
  (`requirements-dev.txt`).
* Install: `pip install -r requirements-dev.txt`
* Audit dependencies: `pip-audit -r requirements.txt`
* Run tests: `python -m pytest -q` (tests use a local HTTP stub server, no
  network and no API key required).
* Discover markets: `python -m mfpbot markets`
* Dry run: `python -m mfpbot run --dry-run`
* Config comes from `.env` (git-ignored) or environment variables; see
  `.env.example`. `FP_` prefix.

## MyFundedPerps API facts (verified against docs + live servers)

* Docs: https://docs.myfundedperpetuals.com — machine-readable index at
  `/llms.txt`, OpenAPI at `/openapi.yaml`.
* Live host: `https://developers.myfundedperpetuals.com` (keys `fp_live_`).
* Sandbox host: `https://sandbox.myfundedperpetuals.com` (keys `fp_test_`).
  A key on the wrong host returns `401`.
* Market data WS: `wss://api-stream.myfundedperpetuals.com/v1/market-data`
  (public, no auth; never send a trading key to it).
* Auth: `Authorization: Bearer <key>`. Access levels: Read Only / Read And Trade.
* Public endpoints (no key): `GET /v1`, `GET /v1/restricted-countries`,
  `GET /v1/markets`, `GET /v1/markets/{market_id}`.
* Responses are wrapped: the payload is under `data`. Errors use
  `{"error": {"code", "message", "request_id", "details"}}` and an
  `X-Request-Id` header.
* Every create/close needs an `Idempotency-Key` header. Standard orders also
  accept a `client_order_id` in the body for reconciliation.
* `market_id` contains a pipe (`binance|BTCUSDT`); URL-encode it in paths.
* Rate limits: ~300 req/min per source IP (per host, approximate). Handle
  `429`/`5xx` with jittered exponential backoff and `Retry-After`.
* WebSocket framing: send `{"op":"sub","id":N,"channel":"candles","payload":
  {"symbols":[coin],"providers":[provider],"intervals":[interval],
  "historyLimit":N}}`. Server replies `sub_ok`, then `events` batches, then
  `snapshot_end`. Candle fields are decimal **strings**; use `isFinal` to act
  only on closed bars. Send `{"op":"req","id":N,"method":"ping"}` for heartbeat.
* **Provider grouping gotcha (verified live):** a subscription that lists more
  than one provider resolves every symbol against the first provider, so
  `symbols:[BTCUSDT, xyz:AAPL], providers:[binance, hyperliquid]` is rejected
  with `unknown market`. Group symbols by provider — one `sub` frame per
  provider — on the same connection. `MarketDataStream.for_markets` does this.
* The API is in **beta**; breaking changes are possible.
* REST only covers trading on existing accounts. Signup, purchases, payouts,
  and API-key creation are website-only (and intentionally not automated here).
* **Verified live (2026-10-08):** `GET /v1/orders?client_order_id=...` filters
  server-side (a bogus id returns `data: []`), and the returned `Order` echoes
  the `client_order_id` *only when it was supplied at placement* (orders placed
  by hand on the website carry `client_order_id: null`). `find_order_by_client_id`
  therefore verifies the field on the returned order and treats a mismatch as
  "not found". The lookup scans the *whole* returned list (paging via the
  cursor) rather than trusting row[0], and retries without the filter if the
  filtered reply is empty — a server that ignores the filter must never hide a
  live, in-flight entry (which the bot would re-place as a duplicate).
  `close_position` is a blocking POST that returns an ack; execution
  is asynchronous, so the caller confirms via `_wait_position_gone`.
* **Verified live (2026-10-08):** the candle WebSocket snapshot arrives with
  `isFinal: true` for completed history bars and `isFinal: false` for the
  forming bar (repeated on each update). `Candle.from_event` reads `isFinal`,
  and `_process_candle` ignores non-final bars.
* **Historical candles:** REST has no klines endpoint. The WebSocket exposes a
  one-shot `{"op":"req","id":N,"method":"candles.history","payload":
  {"provider","symbol","interval","limit",...,"startTime","endTime","priceKind"}}`
  whose `result` is an array of candle events. Retention/availability is
  provider-bounded (short or empty results happen). Implemented as
  `MarketDataStream.fetch_history`. `limit 0` skips retained replay.
  Accepted intervals: `1s 1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M`.
* **Fees (published, used by the backtester's cost model):** per asset class —
  commission **0.03%** crypto / **0.005%** stocks-commodities-indices /
  **0.0025%** forex per fill (maker = taker); hourly swap **0.03%/0.015%/
  0.005% per day** divided into 24 hourly charges (both sides pay). Adverse
  slippage caps: crypto banded by open interest (e.g. $100M–$500M OI, up to
  $100k notional: 1.2 bps/side), majors indices/commodities 0.25 bps, forex
  0.05 bps; a size-aware `GET /v1/markets/{id}/quote` returns the projected
  `slippage_bps`. `CostModel.for_asset_class` carries the presets; explicit
  CLI overrides win over them.
* **Exit management API (verified against the OpenAPI spec):** a `Position`
  carries **no** stop/target levels — exits are separate working orders linked
  from the entry (`take_profit_order_id` / `stop_loss_order_id`,
  `parent_order_id` on the legs, `trigger_price`, `status=working` lists the
  resting set). Moving a stop is `PUT /v1/positions/{id}/exit-orders` with a
  **stale-snapshot-guarded batch**: `expected_position_size` + `expected_orders`
  (`order_id`/`price`/`size`) + `operations` (`modify`/`place`/`cancel`). A
  `409` means the snapshot moved (refresh once and retry); a `422` means the
  move itself was rejected (fail safe, keep the old stop).
* **Challenge rules (1-Step Select):** profit target **9%**, daily loss limit
  **3%**, **static** max drawdown **3%** — all percentages of the *starting
  balance*, never current equity; rules use account equity incl. open P&L. The
  daily limit resets at midnight `America/New_York` (not a fixed UTC time). The
  $2.5K Select prize account has a $2,425 floor and a $225 target. These numbers
  come from `GET /v1/accounts/{id}/trading-policy` at runtime; prefer the API's
  `account_rules` over hard-coding.
* **Copy trading:** native, same-owner, one lead + followers, multiplier
  0.1x–2x, follower size normalized by starting balance. Configured in the
  website UI (not via this bot's API). A follower is **locked against manual
  trading** while enabled, and each follower order is re-validated against its
  own rules/balance (it can reject or shrink). `GET
  /v1/accounts/{id}/trading-policy` exposes `copy_follower_locked` and
  `copy_stop`. See `docs.myfundedperpetuals.com/guides/copy-trading-guide`.
* **Trading-policy fields to consume at runtime:** `account_rules` (effective
  daily-loss / drawdown / target), `limits` (`max_open_positions`,
  `max_position_value_usd`, `min_order_notional_usd`, `trades_per_minute`,
  `order_cooldown_ms`), `maximum_total_notional_usd`, `opening_exposure_restricted`,
  `trading_halt`, `competition` (window + symbol allowlist),
  `copy_follower_locked`, `copy_scope_blocked`, `manual_trading_blocked`,
  `fee_exempt`. The snapshot is advisory; placement is authoritative.
* **October Competition fair play:** forbids operating more than one account
  *for the competition*, trading someone else's, or **copying/mirroring/
  coordinating trades across (different users') accounts**, and exploiting stale
  prices/latency. Same-owner copy trading on normal challenge accounts is a
  platform feature, but do not run this bot on a competition account in a way
  the competition rules forbid.

## Conventions

* No third-party trading SDK (PyPI has no official Python package); the REST
  client is hand-written in `mfpbot/client.py`.
* Sizes must be exact multiples of the market `size_step`; use
  `mfpbot.util.round_step` (Decimal-based) rather than float math.
* Strategies emit a signal only on the crossing candle and only from **closed**
  candles. Keep the strategy interface in `mfpbot/strategy/base.py`. Strategies
  share the `Signal` contract (`action`, `stop_price`, `take_profit_price`), so
  a new strategy needs no change to the bot or the backtester. Give strategy
  `__init__` params unique names (`donchian_period`, `supertrend_mult`, …):
  `build_strategy` filters kwargs by each signature, so a shared name would
  leak between strategies.
* **Backtesting:** `mfpbot/backtest/` holds the dataset loader, the cost model
  (per-asset-class presets via `CostModel.for_asset_class`), the bar engine with
  the same guards, the same `PositionSizer`, and the same breakeven/trailing
  manager as the live bot, the metrics, and walk-forward. `mfpbot/archiver.py`
  records live candles to JSONL (the REST API has no history endpoint). The
  engine assumes the **stop fills first** when a bar spans both stop and target,
  and arms breakeven/trailing from a bar only *after* that bar's exit check (no
  intrabar look-ahead). Never report gross numbers as edge; the default is net
  of cost. The loader refuses multi-symbol/interval datasets (`load_many`
  merges same-market shards only); the archiver dedups by
  `(provider, symbol, interval, open_time)`.
* **Defaults:** `FP_STRATEGY=donchian_breakout` and `FP_TIMEFRAME=4h`. On real
  MFP candles (public `candles.history`) 15m is net-negative for every shipped
  strategy - gross, not just after costs - while 4h gives donchian a positive
  walk-forward edge (BTC MAR 0.73, PF 1.19, 100% folds; ETH PF 1.17). Keep the
  two in sync: the live stream interval (`Config.timeframe`), the `archive`
  CLI default and `MarketDataStream`'s default are all `4h`. Breakeven and
  trailing (`FP_BREAKEVEN_AT_R`, `FP_TRAIL_ATR_MULT`) stay off by default -
  enable per market only when a backtest shows they help.
* Risk guards live in `mfpbot/risk/`. Two layers: bot-side daily caps and
  account-side room from the API risk snapshot. `RiskState` also lives in
  `mfpbot/state.py` alongside the persisted `BotState`. The daily boundary is
  the firm's (`FP_DAY_TIMEZONE`, default `America/New_York`) via
  `risk.manager.day_key`; `_utc_day` stays as a UTC compat wrapper.
* **Exit management:** `_manage_exits` runs on each closed candle while holding
  an owned position (breakeven at `FP_BREAKEVEN_AT_R`, then ATR trailing at
  `FP_TRAIL_ATR_MULT`; the stop only tightens, TP untouched). Levels live in
  `BotState.position_trades` (recorded at adoption from the sent levels and the
  actual fill; pruned with ownership). `_move_stop` re-reads the working legs,
  modifies the single stop leg (re-places it if missing, skips if ambiguous),
  and retries once on 409. Like everything else, no `_lock` across its REST.
* Orders carry the sizer's clamped leverage (`sizing.leverage`), never the raw
  config value, so margin math and the broker agree on markets with a lower cap.
* **Ownership safety:** the bot records the `position_id` returned by each fill
  in `BotState.owned_position_ids` and only ever closes/reverses positions it
  owns. `_prune_owned` drops IDs once a position is gone (TP/SL/manual close).
  `FP_FLATTEN_SCOPE` (`bot` default, or `account`) controls kill-switch scope.
* A competition account's risk snapshot may report `daily_loss_room` and
  `max_drawdown_room` as `null`; the guards treat `null` as "no limit", so the
  bot-side daily caps are the effective protection there.
* **Verified on the live $100k challenge account (2026-10-08):** `GET
  /v1/accounts/{id}` returns `daily_loss_room=null`, `max_drawdown_room=null`,
  and `requirements.{daily_loss_pct,max_drawdown_pct,profit_target_pct}=0`
  (the account's `trading-policy` rules all carry `pct: 0`). With the default
  `FP_ON_MISSING_ROOM=halt`, the bot therefore **refuses every entry** on this
  account. Trading it requires `FP_ON_MISSING_ROOM=bot-only`, which leaves only
  the bot-side caps (`FP_MAX_DAILY_LOSS_PCT`, `FP_MAX_DAILY_TRADES`) as
  protection. `GET /v1/accounts/{id}/trading-policy` is available if the firm
  later exposes real rule percentages.
* `save_state` must never raise — a failed write logs and degrades to a direct
  write, because a state-file error must not kill the trading loop.
* Tests must exercise real code paths. `tests/conftest.py` starts a real
  `ThreadingHTTPServer` stub; do not replace it with mocks.
* Never log or commit API keys. The live key lives only in `.env` (git-ignored,
  mode 600); `load_dotenv_file()` from `mfpbot.config` parses it with the
  standard library (no `python-dotenv` dependency).
* A dry run uses `<FP_STATE_FILE>.dryrun` (`Config.state_path`) so it never
  reads or writes live state.
* Entry drift is capped at `min(5% of close, FP_MAX_ENTRY_DRIFT_ATR * ATR)`.
* **Concurrency rule:** never hold `Bot._lock` across network I/O. The candle
  handler makes blocking REST calls off the event loop; holding the lock across
  them would stall the watchdog's kill check. The halt flag is set under a short
  lock (no I/O) and the flatten runs outside it, guarded by `_flatten_lock`.
  A kill that lands mid-entry stops the order (`_place_entry` re-checks
  `halted` under the lock); a fill that still arrives after a halt is flattened
  by `_finish_entry`.
* **Pending entries:** `BotState.pending_entries` is a dict keyed by
  `client_order_id`, never a single slot. A pending marker is cleared only when
  the order reaches a terminal state; a still-working order stays pending so the
  lookup is retried instead of risking a duplicate.
* **Ownership is released only on confirmation:** `_close_position` does not
  forget the id on submit; `_prune_owned`/`_flatten` drop it once the exchange
  reports the position gone. The flatten verifies the result and the watchdog
  retries it every tick while halted.
