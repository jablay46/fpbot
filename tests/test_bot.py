"""End-to-end tests for the multi-asset bot against the local HTTP stub."""

from __future__ import annotations

import asyncio
import threading

import pytest

import mfpbot.bot as bot_module
from mfpbot.bot import Bot
from mfpbot.client import MfpClient
from mfpbot.config import Config
from mfpbot.risk.manager import _utc_day
from mfpbot.state import BotState, PendingEntry, load_state
from tests.conftest import MARKET, MARKET_ETH, QUOTES, risk_snapshot, make_candles


class _FakeStream:
    """A stream that yields the given candles once and then ends."""

    def __init__(self, candles):
        self._candles = candles

    async def candles(self):
        for candle in self._candles:
            yield candle


# Downtrend that turns up: bullish EMA cross on the final candle.
UP_CLOSES = [100.0, 95.0, 90.0, 85.0, 80.0, 81.0, 95.0]
# Uptrend that turns down: bearish EMA cross on the final candle.
DOWN_CLOSES = [100.0, 105.0, 110.0, 115.0, 120.0, 119.0, 105.0]


def scale(closes, mid):
    """Rescale a close series so its last value equals the stub quote mid."""
    factor = mid / closes[-1]
    return [round(c * factor, 4) for c in closes]


def build_bot(stub_server, tmp_path, symbols=None, state=None, clock=None, **overrides):
    base_url, stub = stub_server
    cfg = Config(
        api_key="fp_test_abc",
        environment="sandbox",
        account_id="acct-1",
        symbols=symbols if symbols is not None else ["binance|BTCUSDT"],
        strategy="ema_cross",
        timeframe="1m",
        ema_fast=2,
        ema_slow=4,
        atr_period=2,
        atr_stop_mult=2.0,
        take_profit_rr=2.0,
        risk_per_trade_pct=1.0,
        leverage=2.0,
        margin_mode="cross",
        max_daily_trades=6,
        max_daily_loss_pct=2.0,
        min_daily_room_pct=0.5,
        state_file=str(tmp_path / "state.json"),
        dry_run=False,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.validate()
    client = MfpClient(cfg.api_key, base_url, sleep=lambda _s: None)
    bot = Bot(cfg, client=client, state=state if state is not None else BotState())
    bot.account = client.get_account("acct-1")
    bot.resolve_markets()
    bot.risk = bot._build_risk_manager()
    if clock is not None:
        bot.clock = clock
    return bot, stub


def feed(bot, market_id, closes, **kwargs):
    symbol = bot.markets[market_id]["coin"]
    for candle in make_candles(closes, symbol=symbol, **kwargs):
        bot.series[market_id].add(candle)
    return bot.series[market_id].closed[-1]


def test_bullish_cross_places_order_with_protection(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", candle)

    orders = [r for r in state.requests if r["method"] == "POST" and r["path"] == "/v1/orders"]
    assert len(orders) == 1
    body = orders[0]["body"]
    assert body["side"] == "buy"
    assert body["market_id"] == "binance|BTCUSDT"
    assert body["size"] > 0
    assert body["stop_loss_price"] < body["take_profit_price"]
    assert orders[0]["headers"]["Idempotency-Key"]
    assert body["client_order_id"].startswith("mfpbot:binance|BTCUSDT:")


def test_fill_is_adopted_so_the_bot_owns_the_position(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", candle)

    # The stub opened pos-1 for our fill; the bot must track it as its own.
    assert bot.state.owned_position_ids == ["pos-1"]


def test_bearish_cross_places_short(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    candle = feed(bot, "binance|BTCUSDT", scale(DOWN_CLOSES, QUOTES["binance|BTCUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", candle)

    orders = [r for r in state.requests if r["path"] == "/v1/orders"]
    assert len(orders) == 1
    assert orders[0]["body"]["side"] == "sell"


def test_multi_asset_trades_each_market_independently(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path, symbols=["binance|BTCUSDT", "binance|ETHUSDT"])
    btc = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))
    eth = feed(bot, "binance|ETHUSDT", scale(DOWN_CLOSES, QUOTES["binance|ETHUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", btc)
    bot.on_closed_candle("binance|ETHUSDT", eth)

    orders = [r["body"] for r in state.requests if r["path"] == "/v1/orders"]
    by_market = {o["market_id"]: o["side"] for o in orders}
    assert by_market == {"binance|BTCUSDT": "buy", "binance|ETHUSDT": "sell"}
    assert bot.state.risk.entries_today == 2


def test_existing_position_in_one_market_does_not_block_another(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path, symbols=["binance|BTCUSDT", "binance|ETHUSDT"])
    state.positions = [{
        "id": "pos-btc", "account_id": "acct-1", "market_id": "binance|BTCUSDT",
        "provider": "binance", "symbol": "BTC", "coin": "BTCUSDT", "side": "long",
        "size": 0.01, "entry_price": 100.0, "leverage": 2.0, "margin_mode": "cross",
        "status": "open", "opened_at": 1,
    }]
    eth = feed(bot, "binance|ETHUSDT", scale(UP_CLOSES, QUOTES["binance|ETHUSDT"]))

    bot.on_closed_candle("binance|ETHUSDT", eth)

    orders = [r["body"] for r in state.requests if r["path"] == "/v1/orders"]
    assert len(orders) == 1
    assert orders[0]["market_id"] == "binance|ETHUSDT"


def test_daily_room_floor_flattens_owned_and_blocks_entry(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    state.risk = risk_snapshot(daily_loss_room=100.0)  # below the 500 floor
    state.positions = [{
        "id": "pos-own", "account_id": "acct-1", "market_id": "binance|ETHUSDT",
        "provider": "binance", "symbol": "ETH", "coin": "ETHUSDT", "side": "long",
        "size": 0.01, "entry_price": 100.0, "leverage": 2.0, "margin_mode": "cross",
        "status": "open", "opened_at": 1,
    }]
    bot.state.owned_position_ids = ["pos-own"]
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", candle)

    assert any(r["path"] == "/v1/positions/pos-own/close" for r in state.requests)
    assert not any(r["path"].endswith("close-all-positions") for r in state.requests)
    assert not any(r["path"] == "/v1/orders" for r in state.requests)
    assert bot.state.risk.halted


def test_account_scope_flatten_uses_close_all(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path, flatten_scope="account")
    state.risk = risk_snapshot(daily_loss_room=100.0)
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", candle)

    assert any(r["path"] == "/v1/accounts/acct-1/close-all-positions" for r in state.requests)


def test_manual_position_is_never_closed_or_reversed(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    # A manual short position in the traded market, not opened by the bot.
    state.positions = [{
        "id": "pos-manual", "account_id": "acct-1", "market_id": "binance|BTCUSDT",
        "provider": "binance", "symbol": "BTC", "coin": "BTCUSDT", "side": "short",
        "size": 0.01, "entry_price": 120.0, "leverage": 2.0, "margin_mode": "cross",
        "status": "open", "opened_at": 1,
    }]
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))  # bullish -> would reverse

    bot.on_closed_candle("binance|BTCUSDT", candle)

    assert not any(r["path"] == "/v1/positions/pos-manual/close" for r in state.requests)
    assert not any(r["path"] == "/v1/orders" for r in state.requests)


def test_no_trade_without_a_signal(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    candle = feed(bot, "binance|BTCUSDT", [100.0] * 12)

    bot.on_closed_candle("binance|BTCUSDT", candle)

    assert not any(r["path"] == "/v1/orders" for r in state.requests)


def test_failed_account_stops_the_bot(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    state.account_status = "failed"
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))

    with pytest.raises(SystemExit):
        bot.on_closed_candle("binance|BTCUSDT", candle)


def test_reversal_closes_existing_position(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    state.positions = [{
        "id": "pos-1", "account_id": "acct-1", "market_id": "binance|BTCUSDT",
        "provider": "binance", "symbol": "BTC", "coin": "BTCUSDT", "side": "short",
        "size": 0.01, "entry_price": 120.0, "leverage": 2.0, "margin_mode": "cross",
        "status": "open", "opened_at": 1,
    }]
    bot.state.owned_position_ids = ["pos-1"]
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", candle)

    assert any(r["path"] == "/v1/positions/pos-1/close" for r in state.requests)
    orders = [r["body"] for r in state.requests if r["path"] == "/v1/orders"]
    assert len(orders) == 1
    assert orders[0]["side"] == "buy"


def test_dry_run_does_not_send_orders(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path, dry_run=True)
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", candle)

    assert not any(r["path"] == "/v1/orders" for r in state.requests)
    assert bot.state.risk.entries_today == 1


def test_stale_quote_skips_entry(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    # Candle close is 100 but the quote mid is 100.1; make the candle wildly
    # different from the quote to trip the drift guard.
    candle = feed(bot, "binance|BTCUSDT", [100.0] * 6 + [100.0])
    bot.series["binance|BTCUSDT"]._final[-1] = type(candle)(
        provider=candle.provider, symbol=candle.symbol, interval=candle.interval,
        open_time=candle.open_time, close_time=candle.close_time,
        open=10.0, high=10.0, low=10.0, close=10.0, volume=1.0, is_final=True,
    )
    bot.on_closed_candle("binance|BTCUSDT", bot.series["binance|BTCUSDT"].closed[-1])
    assert not any(r["path"] == "/v1/orders" for r in state.requests)


def _feed_fresh_cross(bot, market_id, closes, *, now_ms):
    symbol = bot.markets[market_id]["coin"]
    start = now_ms - len(closes) * 60_000
    candles = make_candles(closes, start_time=start, symbol=symbol)
    for candle in candles:
        bot._process_candle(market_id, candle)


def test_lost_order_response_is_reconciled(stub_server, tmp_path):
    """An order accepted but with a lost reply must still be owned and counted."""
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = build_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    state.drop_order_response = True

    _feed_fresh_cross(
        bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]), now_ms=now_ms
    )

    # The client may retry the POST; the exchange must still create one order.
    assert len(state.orders) == 1
    assert bot.state.risk.entries_today == 1
    assert bot.state.owned_position_ids == ["pos-1"]
    assert bot.state.pending_entry is None


def test_unknown_order_blocks_entry_without_duplicate(stub_server, tmp_path):
    """When the reply and the lookup both fail, block the market, never re-order."""
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = build_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    state.drop_order_response = True
    state.fail_order_lookup_times = 99

    _feed_fresh_cross(
        bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]), now_ms=now_ms
    )

    assert len(state.orders) == 1
    assert bot.state.pending_entry is not None
    assert bot.state.risk.entries_today == 0
    assert bot.state.owned_position_ids == []

    # A later signal in the same market must not fire a second order.
    now2 = now_ms + 60_000
    bot.clock = lambda: now2 / 1000.0
    _feed_fresh_cross(
        bot, "binance|BTCUSDT", scale(DOWN_CLOSES, QUOTES["binance|BTCUSDT"]), now_ms=now2
    )
    assert len(state.orders) == 1


def test_pending_entry_is_reconciled_on_later_candle(stub_server, tmp_path):
    """Once the lookup recovers, a pending entry is adopted exactly once."""
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = build_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    state.drop_order_response = True
    state.fail_order_lookup_times = 10  # exhaust the client's retries this candle

    _feed_fresh_cross(
        bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]), now_ms=now_ms
    )
    assert bot.state.pending_entry is not None
    assert bot.state.risk.entries_today == 0

    # The lookup recovers; the next candle reconciles the outstanding entry.
    state.fail_order_lookup_times = 0
    now2 = now_ms + 60_000
    bot.clock = lambda: now2 / 1000.0
    _feed_fresh_cross(
        bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]), now_ms=now2
    )

    assert len(state.orders) == 1
    assert bot.state.risk.entries_today == 1
    assert bot.state.owned_position_ids == ["pos-1"]
    assert bot.state.pending_entry is None


def test_run_offloads_candle_work_to_a_worker_thread(stub_server, tmp_path, monkeypatch):
    """The blocking REST work must not run on the event-loop thread."""
    bot, state = build_bot(stub_server, tmp_path)
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot.clock = lambda: now_ms / 1000.0
    candles = make_candles(
        scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]),
        start_time=now_ms - 7 * 60_000,
        symbol="BTCUSDT",
    )
    stream = _FakeStream(candles)
    monkeypatch.setattr(
        bot_module.MarketDataStream, "for_markets", staticmethod(lambda *a, **k: stream)
    )
    bot._market_id_for = lambda c: "binance|BTCUSDT"
    bot.cfg.poll_seconds = 5.0

    seen: dict[str, threading.Thread] = {}
    original = bot._process_candle

    def wrapped(market_id, c):
        seen["thread"] = threading.current_thread()
        return original(market_id, c)

    bot._process_candle = wrapped

    asyncio.run(bot.run())

    assert "thread" in seen
    assert seen["thread"] is not threading.current_thread()
    assert len(state.orders) == 1  # the trade still happened


def _owned_short(state, market_id="binance|BTCUSDT", position_id="pos-1"):
    state.positions = [{
        "id": position_id, "account_id": "acct-1", "market_id": market_id,
        "provider": "binance", "symbol": "BTC", "coin": "BTCUSDT", "side": "short",
        "size": 0.01, "entry_price": 120.0, "leverage": 2.0, "margin_mode": "cross",
        "status": "open", "opened_at": 1,
    }]
    return [position_id]


def test_reversal_adopts_the_new_position_when_close_lags(stub_server, tmp_path):
    """A closed-but-still-listed old position must not be re-adopted."""
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = build_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    bot.state.owned_position_ids = _owned_short(state)
    state.slow_close_polls = 2  # the old position lingers for two polls

    _feed_fresh_cross(
        bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]), now_ms=now_ms
    )

    assert any(r["path"] == "/v1/positions/pos-1/close" for r in state.requests)
    assert bot.state.owned_position_ids == ["pos-2"]


def test_reversal_skips_entry_when_old_position_will_not_close(stub_server, tmp_path):
    """If the old position will not go away, do not stack a new one on top."""
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = build_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    bot.state.owned_position_ids = _owned_short(state)
    state.slow_close_polls = 10_000  # never disappears within the wait window

    _feed_fresh_cross(
        bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]), now_ms=now_ms
    )

    assert any(r["path"] == "/v1/positions/pos-1/close" for r in state.requests)
    assert not any(r["path"] == "/v1/orders" for r in state.requests)
    assert bot.state.owned_position_ids == []


def test_kill_switch_runs_while_holding_a_position(stub_server, tmp_path):
    """A bot-owned position must not shield the account from the daily kill switch."""
    bot, state = build_bot(stub_server, tmp_path)
    state.positions = [{
        "id": "pos-own", "account_id": "acct-1", "market_id": "binance|BTCUSDT",
        "provider": "binance", "symbol": "BTC", "coin": "BTCUSDT", "side": "long",
        "size": 0.01, "entry_price": 100.0, "leverage": 2.0, "margin_mode": "cross",
        "status": "open", "opened_at": 1,
    }]
    bot.state.owned_position_ids = ["pos-own"]
    bot.state.risk.day = _utc_day()
    bot.state.risk.day_start_equity = 100000.0
    state.risk = risk_snapshot(equity=96000.0)  # -4% vs the 2% cap
    candle = feed(bot, "binance|BTCUSDT", [100.0] * 12)  # no crossover

    bot.on_closed_candle("binance|BTCUSDT", candle)

    assert any(r["path"] == "/v1/positions/pos-own/close" for r in state.requests)
    assert bot.state.risk.halted


def test_watchdog_flattens_without_a_new_candle(stub_server, tmp_path):
    """The poll watchdog enforces the kill switch between candles."""
    import asyncio

    bot, state = build_bot(stub_server, tmp_path, poll_seconds=0.05)
    state.positions = [{
        "id": "pos-own", "account_id": "acct-1", "market_id": "binance|BTCUSDT",
        "provider": "binance", "symbol": "BTC", "coin": "BTCUSDT", "side": "long",
        "size": 0.01, "entry_price": 100.0, "leverage": 2.0, "margin_mode": "cross",
        "status": "open", "opened_at": 1,
    }]
    bot.state.owned_position_ids = ["pos-own"]
    bot.state.risk.day = _utc_day()
    bot.state.risk.day_start_equity = 100000.0
    state.risk = risk_snapshot(equity=96000.0)

    async def scenario():
        task = asyncio.create_task(bot._watchdog())
        for _ in range(200):
            if bot.state.risk.halted:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert bot.state.risk.halted
    assert any(r["path"] == "/v1/positions/pos-own/close" for r in state.requests)


def test_history_replay_is_not_traded(stub_server, tmp_path):
    """300 historical candles with a crossover inside must not trade."""
    bot, state = build_bot(stub_server, tmp_path)
    # A long history whose last-but-one candle crosses; all of it is old.
    closes = [100.0] * 290 + [95.0, 90.0, 85.0, 80.0, 81.0, 95.0]
    history = make_candles(closes, symbol="BTCUSDT")
    for candle in history:
        bot.series["binance|BTCUSDT"].add(candle)
        bot._process_candle("binance|BTCUSDT", candle)

    orders = [r for r in state.requests if r["path"] == "/v1/orders"]
    accounts = [r for r in state.requests if r["path"] == "/v1/accounts/acct-1"]
    assert orders == []
    assert len(accounts) <= 2
    assert bot.state.risk.entries_today == 0


def test_fresh_candle_after_history_still_trades(stub_server, tmp_path):
    """A fresh crossing candle is evaluated once the history is behind us."""
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = build_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    closes = scale([100.0] * 290 + [95.0, 90.0, 85.0, 80.0, 81.0, 95.0], QUOTES["binance|BTCUSDT"])
    start = now_ms - len(closes) * 60_000
    history = make_candles(closes, start_time=start, symbol="BTCUSDT")
    for candle in history[:-1]:
        bot._process_candle("binance|BTCUSDT", candle)
    fresh = history[-1]
    assert fresh.close_time >= now_ms - 60_000
    bot._process_candle("binance|BTCUSDT", fresh)

    orders = [r for r in state.requests if r["path"] == "/v1/orders"]
    assert len(orders) == 1


def test_restart_does_not_reprocess_last_candle(stub_server, tmp_path):
    """A restart with a persisted cursor skips already-seen candles."""
    cursor = 1_700_000_000_000 + 6 * 60_000
    saved = BotState(last_processed_open_time={"binance|BTCUSDT": cursor})
    now_ms = cursor + 60_000 + 1000
    bot, state = build_bot(
        stub_server, tmp_path, state=saved, clock=lambda: now_ms / 1000.0
    )
    closes = [100.0] * 290 + [95.0, 90.0, 85.0, 80.0, 81.0, 95.0]
    start = cursor - (len(closes) - 1) * 60_000
    for candle in make_candles(closes, start_time=start, symbol="BTCUSDT"):
        bot._process_candle("binance|BTCUSDT", candle)

    orders = [r for r in state.requests if r["path"] == "/v1/orders"]
    assert orders == []
    assert bot.state.risk.entries_today == 0


def test_market_id_mapping(stub_server, tmp_path):
    bot, _ = build_bot(stub_server, tmp_path, symbols=["binance|BTCUSDT", "binance|ETHUSDT"])
    from mfpbot.market_stream import Candle

    candle = Candle("binance", "ETHUSDT", "1m", 1, 2, 1, 1, 1, 1, 1, True)
    assert bot._market_id_for(candle) == "binance|ETHUSDT"
    unknown = Candle("binance", "DOGEUSDT", "1m", 1, 2, 1, 1, 1, 1, 1, True)
    assert bot._market_id_for(unknown) is None
