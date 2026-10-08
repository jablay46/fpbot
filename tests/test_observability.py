"""Tests for candle observability and log noise (R4)."""

from __future__ import annotations

import logging

from mfpbot.risk.manager import _utc_day
from tests.conftest import QUOTES, risk_snapshot, make_candles
from tests.test_bot import build_bot, scale


def test_snapshot_summary_logged_once_after_history(stub_server, tmp_path, caplog):
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = build_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    closes = scale([100.0] * 290 + [95.0, 90.0, 85.0, 80.0, 81.0, 95.0], QUOTES["binance|BTCUSDT"])
    start = now_ms - len(closes) * 60_000
    history = make_candles(closes, start_time=start, symbol="BTCUSDT")

    with caplog.at_level(logging.INFO, logger="mfpbot.bot"):
        for candle in history:
            bot._process_candle("binance|BTCUSDT", candle)

    summaries = [r for r in caplog.records if "snapshot processed" in r.message]
    assert len(summaries) == 1
    assert "history candle(s) skipped" in summaries[0].message


def test_stale_stream_warning_after_three_intervals(stub_server, tmp_path, caplog):
    bot, state = build_bot(stub_server, tmp_path)
    clock = {"now": 1_700_000_000.0}
    bot.clock = lambda: clock["now"]
    bot._last_fresh_candle_wall = clock["now"]

    clock["now"] += 4 * 60  # 4 intervals with no fresh candle
    with caplog.at_level(logging.WARNING, logger="mfpbot.bot"):
        bot._check_stream_health()
        bot._check_stream_health()  # rate-limited: no second warning

    warnings = [r for r in caplog.records if "no fresh candle" in r.message]
    assert len(warnings) == 1
    assert "clock" in warnings[0].message.lower()


def test_future_candle_warns_about_local_clock(stub_server, tmp_path, caplog):
    now_ms = 1_700_000_000_000
    bot, state = build_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    # A candle closing two minutes "in the future" relative to our clock.
    candles = make_candles([100.0] * 4, start_time=now_ms + 120_000, symbol="BTCUSDT")

    with caplog.at_level(logging.WARNING, logger="mfpbot.bot"):
        for candle in candles:
            bot._process_candle("binance|BTCUSDT", candle)

    warnings = [r for r in caplog.records if "future" in r.message.lower()]
    assert warnings


def test_already_halted_does_not_log_kill_switch_none(stub_server, tmp_path, caplog):
    bot, state = build_bot(stub_server, tmp_path)
    bot.state.risk.halted = True
    bot.state.risk.halt_reason = "already halted"
    bot.state.risk.day = _utc_day()
    bot.state.risk.day_start_equity = 100000.0

    with caplog.at_level(logging.DEBUG, logger="mfpbot.bot"):
        bot.on_closed_candle(
            "binance|BTCUSDT", make_candles([100.0] * 12, symbol="BTCUSDT")[-1]
        )

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not any("kill switch: None" in r.message for r in errors)
    # No REST flatten is needed: nothing is owned.
    assert not any(r["path"].endswith("/close") for r in state.requests)
