"""End-to-end tests for the multi-asset bot against the local HTTP stub."""

from __future__ import annotations

import pytest

from mfpbot.bot import Bot
from mfpbot.client import MfpClient
from mfpbot.config import Config
from mfpbot.state import BotState
from tests.conftest import MARKET, MARKET_ETH, QUOTES, risk_snapshot, make_candles

# Downtrend that turns up: bullish EMA cross on the final candle.
UP_CLOSES = [100.0, 95.0, 90.0, 85.0, 80.0, 81.0, 95.0]
# Uptrend that turns down: bearish EMA cross on the final candle.
DOWN_CLOSES = [100.0, 105.0, 110.0, 115.0, 120.0, 119.0, 105.0]


def scale(closes, mid):
    """Rescale a close series so its last value equals the stub quote mid."""
    factor = mid / closes[-1]
    return [round(c * factor, 4) for c in closes]


def build_bot(stub_server, tmp_path, symbols=None, **overrides):
    base_url, state = stub_server
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
    bot = Bot(cfg, client=client, state=BotState())
    bot.account = client.get_account("acct-1")
    bot.resolve_markets()
    bot.risk = bot._build_risk_manager()
    return bot, state


def feed(bot, market_id, closes):
    symbol = bot.markets[market_id]["coin"]
    for candle in make_candles(closes, symbol=symbol):
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


def test_daily_room_floor_flattens_and_blocks_entry(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    state.risk = risk_snapshot(daily_loss_room=100.0)  # below the 500 floor
    candle = feed(bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]))

    bot.on_closed_candle("binance|BTCUSDT", candle)

    assert any(r["path"] == "/v1/accounts/acct-1/close-all-positions" for r in state.requests)
    assert not any(r["path"] == "/v1/orders" for r in state.requests)
    assert bot.state.risk.halted


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


def test_market_id_mapping(stub_server, tmp_path):
    bot, _ = build_bot(stub_server, tmp_path, symbols=["binance|BTCUSDT", "binance|ETHUSDT"])
    from mfpbot.market_stream import Candle

    candle = Candle("binance", "ETHUSDT", "1m", 1, 2, 1, 1, 1, 1, 1, True)
    assert bot._market_id_for(candle) == "binance|ETHUSDT"
    unknown = Candle("binance", "DOGEUSDT", "1m", 1, 2, 1, 1, 1, 1, 1, True)
    assert bot._market_id_for(unknown) is None
