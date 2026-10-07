"""Tests for position sizing and daily risk guards."""

from __future__ import annotations

from datetime import datetime, timezone

from mfpbot.risk.manager import RiskManager, RiskState
from mfpbot.risk.sizing import PositionSizer
from tests.conftest import risk_snapshot


def test_sizing_matches_risk_budget():
    sizer = PositionSizer(risk_per_trade_pct=1.0, leverage=2.0, size_step=0.001, min_notional=10)
    # 1% of 100000 = 1000 risk; stop distance 100 -> size 10, notional 100000.
    result = sizer.size(equity=100000, entry=10000, stop=9900)
    assert result.ok
    assert result.size == 10.0
    assert abs(result.risk_amount - 1000.0) < 1e-6
    assert abs(result.required_margin - 50000.0) < 1e-6


def test_sizing_rounds_down_to_step():
    sizer = PositionSizer(risk_per_trade_pct=1.0, leverage=2.0, size_step=0.1, min_notional=1)
    result = sizer.size(equity=1000, entry=100, stop=90)
    # raw size 1.0 -> exactly one step
    assert result.ok
    assert result.size == 1.0


def test_sizing_rejects_below_min_notional():
    sizer = PositionSizer(risk_per_trade_pct=0.01, leverage=2.0, size_step=0.001, min_notional=10000)
    result = sizer.size(equity=100000, entry=100, stop=99)
    assert not result.ok
    assert "minimum" in result.reason


def test_sizing_rejects_zero_stop_distance():
    sizer = PositionSizer(risk_per_trade_pct=1.0, leverage=2.0)
    result = sizer.size(equity=1000, entry=100, stop=100)
    assert not result.ok


def test_sizing_caps_leverage_at_market_max():
    sizer = PositionSizer(risk_per_trade_pct=1.0, leverage=50.0, max_leverage=5.0, size_step=0.001, min_notional=1)
    result = sizer.size(equity=100000, entry=100, stop=99)
    assert result.ok
    assert result.leverage == 5.0


def _manager() -> RiskManager:
    return RiskManager(max_daily_trades=2, max_daily_loss_pct=2.0, min_daily_room_pct=0.5, starting_balance=100000)


def test_daily_loss_cap_halts_and_flags_flatten():
    mgr = _manager()
    state = RiskState(day="2026-01-01", day_start_equity=100000)
    decision = mgr.can_open(state, equity=97000, account_risk=risk_snapshot())
    assert not decision.allowed
    assert decision.flatten


def test_trade_limit_blocks_after_n_entries():
    mgr = _manager()
    state = RiskState(day="2026-01-01", day_start_equity=100000)
    mgr.record_entry(state)
    mgr.record_entry(state)
    decision = mgr.can_open(state, equity=100000, account_risk=risk_snapshot())
    assert not decision.allowed
    assert "limit" in decision.reason


def test_account_daily_room_floor_blocks():
    mgr = _manager()
    state = RiskState(day="2026-01-01", day_start_equity=100000)
    # floor is 0.5% of 100000 = 500; room of 400 is below it.
    decision = mgr.can_open(state, equity=100000, account_risk=risk_snapshot(daily_loss_room=400.0))
    assert not decision.allowed
    assert decision.flatten


def test_roll_day_resets_counters():
    mgr = _manager()
    state = RiskState(day="2026-01-01", day_start_equity=100000, entries_today=5, halted=True)
    mgr.roll_day(state, equity=100000, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert state.day == "2026-01-02"
    assert state.entries_today == 0
    assert state.halted is False


def test_allows_when_everything_is_healthy():
    mgr = _manager()
    state = RiskState(day="2026-01-01", day_start_equity=100000)
    decision = mgr.can_open(state, equity=100500, account_risk=risk_snapshot())
    assert decision.allowed


def test_check_kill_flags_bot_daily_loss():
    mgr = _manager()
    state = RiskState(day="2026-01-01", day_start_equity=100000)
    decision = mgr.check_kill(state, equity=97000, account_risk=risk_snapshot())
    assert not decision.allowed
    assert decision.flatten


def test_check_kill_flags_room_floor():
    mgr = _manager()
    state = RiskState(day="2026-01-01", day_start_equity=100000)
    decision = mgr.check_kill(state, equity=100000, account_risk=risk_snapshot(daily_loss_room=100.0))
    assert not decision.allowed
    assert decision.flatten


def test_can_enter_ignores_loss_but_honours_trade_limit():
    mgr = _manager()
    state = RiskState(day="2026-01-01", day_start_equity=100000)
    # A large loss does not block entry by itself; that is check_kill's job.
    assert mgr.can_enter(state, equity=90000, account_risk=risk_snapshot()).allowed
    mgr.record_entry(state)
    mgr.record_entry(state)
    decision = mgr.can_enter(state, equity=100000, account_risk=risk_snapshot())
    assert not decision.allowed
    assert "limit" in decision.reason
