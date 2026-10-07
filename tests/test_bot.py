"""End-to-end tests for the bot against the local HTTP stub."""

from __future__ import annotations

import pytest

from mfpbot.bot import Bot
from mfpbot.client import MfpClient
from mfpbot.config import Config
from mfpbot.state import BotState
from tests.conftest import risk_snapshot, make_candles

# A downtrend that turns up, producing a bullish EMA cross on the final candle.
BULLISH_CLOSES = [100.0, 95.0, 90.0, 85.0, 80.0, 81.0, 95.0]


def build_bot(stub_server, tmp_path, **overrides):
    base_url, state = stub_server
    cfg = Config(
        api_key="fp_test_abc",
        environment="sandbox",
        account_id="acct-1",
        market_id="binance|BTCUSDT",
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
    bot.market = client.get_market(cfg.market_id)
    bot.risk = bot._build_risk_manager()
    return bot, state


def test_bullish_cross_places_order_with_protection(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    for candle in make_candles(BULLISH_CLOSES):
        bot.series.add(candle)

    bot.on_closed_candle(bot.series.closed[-1])

    orders = [r for r in state.requests if r["method"] == "POST" and r["path"] == "/v1/orders"]
    assert len(orders) == 1
    body = orders[0]["body"]
    assert body["side"] == "buy"
    assert body["size"] > 0
    assert body["stop_loss_price"] < body["take_profit_price"]
    assert orders[0]["headers"]["Idempotency-Key"]
    assert body["client_order_id"].startswith("mfpbot:")


def test_daily_room_floor_flattens_and_blocks_entry(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    state.risk = risk_snapshot(daily_loss_room=100.0)  # below the 500 floor
    for candle in make_candles(BULLISH_CLOSES):
        bot.series.add(candle)

    bot.on_closed_candle(bot.series.closed[-1])

    assert any(r["path"] == "/v1/accounts/acct-1/close-all-positions" for r in state.requests)
    assert not any(r["path"] == "/v1/orders" for r in state.requests)
    assert bot.state.risk.halted


def test_no_trade_without_a_signal(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    # A flat series produces no crossover.
    for candle in make_candles([100.0] * 12):
        bot.series.add(candle)

    bot.on_closed_candle(bot.series.closed[-1])

    assert not any(r["path"] == "/v1/orders" for r in state.requests)


def test_failed_account_stops_the_bot(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    state.account_status = "failed"
    for candle in make_candles(BULLISH_CLOSES):
        bot.series.add(candle)

    with pytest.raises(SystemExit):
        bot.on_closed_candle(bot.series.closed[-1])


def test_reversal_closes_existing_position(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path)
    state.positions = [{
        "id": "pos-1", "account_id": "acct-1", "market_id": "binance|BTCUSDT",
        "provider": "binance", "symbol": "BTC", "coin": "BTCUSDT", "side": "short",
        "size": 0.01, "entry_price": 120.0, "leverage": 2.0, "margin_mode": "cross",
        "status": "open", "opened_at": 1,
    }]
    for candle in make_candles(BULLISH_CLOSES):
        bot.series.add(candle)

    bot.on_closed_candle(bot.series.closed[-1])

    assert any(r["path"] == "/v1/positions/pos-1/close" for r in state.requests)
    # A new long entry follows the close.
    orders = [r for r in state.requests if r["path"] == "/v1/orders"]
    assert len(orders) == 1
    assert orders[0]["body"]["side"] == "buy"


def test_dry_run_does_not_send_orders(stub_server, tmp_path):
    bot, state = build_bot(stub_server, tmp_path, dry_run=True)
    for candle in make_candles(BULLISH_CLOSES):
        bot.series.add(candle)

    bot.on_closed_candle(bot.series.closed[-1])

    assert not any(r["path"] == "/v1/orders" for r in state.requests)
    assert bot.state.risk.entries_today == 1
