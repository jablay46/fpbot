"""Tests for state persistence."""

from __future__ import annotations

from mfpbot.risk.manager import RiskState
from mfpbot.state import BotState, load_state, save_state


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = BotState(
        risk=RiskState(day="2026-01-01", day_start_equity=100000.0, entries_today=3, halted=True,
                       halt_reason="daily loss"),
        last_processed_open_time=123456789,
        last_entry_client_order_id="mfpbot:x:1",
    )
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.risk.entries_today == 3
    assert loaded.risk.halted is True
    assert loaded.last_processed_open_time == 123456789
    assert loaded.last_entry_client_order_id == "mfpbot:x:1"


def test_missing_file_returns_fresh_state(tmp_path):
    loaded = load_state(tmp_path / "nope.json")
    assert loaded.risk.entries_today == 0


def test_corrupt_file_returns_fresh_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    loaded = load_state(path)
    assert loaded.risk.entries_today == 0
