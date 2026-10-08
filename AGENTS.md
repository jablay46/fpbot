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

## Conventions

* No third-party trading SDK (PyPI has no official Python package); the REST
  client is hand-written in `mfpbot/client.py`.
* Sizes must be exact multiples of the market `size_step`; use
  `mfpbot.util.round_step` (Decimal-based) rather than float math.
* Strategies emit a signal only on the crossing candle and only from **closed**
  candles. Keep the strategy interface in `mfpbot/strategy/base.py`.
* Risk guards live in `mfpbot/risk/`. Two layers: bot-side daily caps and
  account-side room from the API risk snapshot. `RiskState` also lives in
  `mfpbot/state.py` alongside the persisted `BotState`.
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
