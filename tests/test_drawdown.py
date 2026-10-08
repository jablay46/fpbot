"""End-to-end tests for the bot-side cumulative drawdown guard (R1)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import mfpbot.bot as bot_module
from mfpbot.cli import main as cli_main
from mfpbot.risk.manager import RiskState, day_key
from mfpbot.state import BotState, load_state, save_state
from tests.conftest import QUOTES, risk_snapshot, make_candles


# The bot guards the firm's day boundary (America/New_York by default), so
# tests pin "today" to the same zone instead of UTC.
_DAY_TZ = "America/New_York"


def _today() -> str:
    return day_key(tz=_DAY_TZ)

from tests.test_bot import (
    DOWN_CLOSES,
    UP_CLOSES,
    _feed_fresh_cross,
    _owned_long,
    build_bot,
    feed,
    scale,
)


def _dd_bot(stub_server, tmp_path, **overrides):
    overrides.setdefault("max_total_drawdown_pct", 5.0)
    overrides.setdefault("drawdown_basis", "starting")
    return build_bot(stub_server, tmp_path, **overrides)


def test_total_drawdown_halt_survives_day_rollover(stub_server, tmp_path):
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = _dd_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    bot.state.owned_position_ids = _owned_long(state)
    bot.state.risk.day = "2026-01-01"
    bot.state.risk.day_start_equity = 100000.0
    state.risk = risk_snapshot(equity=94000.0)  # -6% vs the 5% limit

    # A candle with no crossover still runs the kill switch.
    bot.on_closed_candle("binance|BTCUSDT", feed(bot, "binance|BTCUSDT", [100.0] * 12))

    assert bot.state.risk.total_drawdown_halted
    assert any(r["path"] == "/v1/positions/pos-own/close" for r in state.requests)

    # The day rolls over (00:01 ET is 05:01 UTC in January); the cumulative
    # halt must NOT clear and no entry may be taken even on a fresh crossover.
    bot.state.owned_position_ids = []
    bot._now = lambda: datetime(2026, 1, 2, 5, 1, tzinfo=timezone.utc)
    now2 = now_ms + 60_000
    bot.clock = lambda: now2 / 1000.0
    _feed_fresh_cross(
        bot, "binance|BTCUSDT", scale(UP_CLOSES, QUOTES["binance|BTCUSDT"]), now_ms=now2
    )

    assert bot.state.risk.total_drawdown_halted
    assert not any(r["path"] == "/v1/orders" for r in state.requests)
    assert bot.state.risk.entries_today == 0


def test_total_drawdown_halt_survives_restart(stub_server, tmp_path):
    now_ms = 1_700_000_000_000 + 1000 * 60_000
    bot, state = _dd_bot(stub_server, tmp_path, clock=lambda: now_ms / 1000.0)
    bot.state.owned_position_ids = _owned_long(state)
    bot.state.risk.day = "2026-01-01"
    bot.state.risk.day_start_equity = 100000.0
    state.risk = risk_snapshot(equity=94000.0)

    bot.on_closed_candle("binance|BTCUSDT", feed(bot, "binance|BTCUSDT", [100.0] * 12))
    assert bot.state.risk.total_drawdown_halted

    reloaded = load_state(str(tmp_path / "state.json"))
    assert reloaded.risk.total_drawdown_halted is True
    assert reloaded.risk.total_drawdown_reason


def test_total_drawdown_flatten_retried_by_watchdog(stub_server, tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "CLOSE_VERIFY_TIMEOUT", 0.2)
    bot, state = _dd_bot(stub_server, tmp_path, poll_seconds=0.02)
    bot.state.owned_position_ids = _owned_long(state)
    bot.state.risk.day = _today()
    bot.state.risk.day_start_equity = 100000.0
    state.risk = risk_snapshot(equity=94000.0)  # triggers the cumulative guard
    state.fail_close_times = 2  # two rejected closes, then it works

    async def scenario():
        task = asyncio.create_task(bot._watchdog())
        for _ in range(400):
            if bot.state.owned_position_ids == []:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    closes = [r for r in state.requests if r["path"] == "/v1/positions/pos-own/close"]
    assert len(closes) >= 3  # retried after the two failures
    assert state.closed_position_ids == ["pos-own"]
    assert bot.state.owned_position_ids == []
    assert bot.state.risk.total_drawdown_halted


def test_reset_halt_cli_requires_yes_and_clears(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "bot_state.json"
    save_state(
        str(state_path),
        BotState(risk=RiskState(
            day="2026-01-01", day_start_equity=100000.0, halted=True, halt_reason="daily",
            total_drawdown_halted=True, total_drawdown_reason="total drawdown",
        )),
    )
    monkeypatch.setenv("FP_STATE_FILE", str(state_path))
    monkeypatch.setenv("FP_ENV", "sandbox")
    monkeypatch.delenv("FP_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    assert cli_main(["reset-halt"]) == 2
    assert load_state(str(state_path)).risk.total_drawdown_halted is True

    assert cli_main(["reset-halt", "--yes"]) == 0
    reloaded = load_state(str(state_path))
    assert reloaded.risk.total_drawdown_halted is False
    assert reloaded.risk.halted is False
