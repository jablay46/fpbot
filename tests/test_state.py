"""Tests for state persistence."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from mfpbot.config import Config
from mfpbot.risk.manager import RiskState
from mfpbot.state import (
    BotState,
    PendingEntry,
    StateLoadError,
    load_state,
    save_state,
)


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = BotState(
        risk=RiskState(day="2026-01-01", day_start_equity=100000.0, entries_today=3, halted=True,
                       halt_reason="daily loss"),
        last_processed_open_time={"binance|BTCUSDT": 123456789, "binance|ETHUSDT": 987654321},
        last_entry_client_order_id="mfpbot:x:1",
    )
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.risk.entries_today == 3
    assert loaded.risk.halted is True
    assert loaded.last_processed_open_time == {"binance|BTCUSDT": 123456789, "binance|ETHUSDT": 987654321}
    assert loaded.last_entry_client_order_id == "mfpbot:x:1"


def test_missing_file_returns_fresh_state(tmp_path):
    loaded = load_state(tmp_path / "nope.json")
    assert loaded.risk.entries_today == 0
    assert loaded.last_processed_open_time == {}


def test_corrupt_file_returns_fresh_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    loaded = load_state(path)
    assert loaded.risk.entries_today == 0


def test_legacy_scalar_cursor_migrates_to_empty_dict(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"risk": {"day": "2026-01-01"}, "last_processed_open_time": 123}', encoding="utf-8")
    loaded = load_state(path)
    assert loaded.last_processed_open_time == {}


def test_save_state_never_raises_on_unwritable_path(tmp_path):
    # A directory where the state file should be makes writes fail; the helper
    # must swallow the error so the trading loop keeps running.
    target = tmp_path / "state.json"
    target.mkdir()
    save_state(target, BotState(owned_position_ids=["pos-1"]))  # must not raise


def test_owned_position_ids_round_trip(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, BotState(owned_position_ids=["pos-1", "pos-2"]))
    assert load_state(path).owned_position_ids == ["pos-1", "pos-2"]


def test_pending_entries_round_trip(tmp_path):
    path = tmp_path / "state.json"
    a = PendingEntry(
        market_id="binance|BTCUSDT",
        client_order_id="mfpbot:btc:1",
        idempotency_key="key-1",
        sent_at=1.5,
        pre_position_ids=["pos-old"],
    )
    b = PendingEntry(
        market_id="binance|ETHUSDT",
        client_order_id="mfpbot:eth:2",
        idempotency_key="key-2",
        sent_at=2.5,
    )
    save_state(path, BotState(pending_entries={a.client_order_id: a, b.client_order_id: b}))
    loaded = load_state(path)
    assert loaded.pending_entries == {a.client_order_id: a, b.client_order_id: b}


def test_concurrent_persist_and_mutation_never_corrupts_the_file(tmp_path):
    """Many threads saving/mutating while another reads must never see bad JSON."""
    from mfpbot.bot import Bot
    from mfpbot.client import MfpClient

    path = tmp_path / "state.json"
    cfg = Config(
        api_key="fp_test_abc", environment="sandbox", account_id="acct-1",
        symbols=["binance|BTCUSDT"], state_file=str(path),
    )
    bot = Bot(cfg, client=MfpClient(cfg.api_key, "http://127.0.0.1:9", sleep=lambda _s: None))

    stop = threading.Event()
    errors: list[BaseException] = []

    def saver():
        try:
            for _ in range(400):
                if stop.is_set():
                    return
                bot._persist()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def mutator():
        try:
            i = 0
            while not stop.is_set():
                i += 1
                bot.state.last_processed_open_time["binance|BTCUSDT"] = i
                bot.state.owned_position_ids = [f"pos-{i}"]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(800):
                if path.exists():
                    json.loads(path.read_text(encoding="utf-8"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=saver) for _ in range(2)]
    threads += [threading.Thread(target=mutator), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    # The file must still be valid JSON at the end.
    json.loads(path.read_text(encoding="utf-8"))


def test_load_state_recovers_from_backup(tmp_path):
    path = tmp_path / "state.json"
    # A valid backup and a corrupt main file.
    save_state(str(path), BotState(risk=RiskState(entries_today=7)))
    (tmp_path / "state.json.bak").write_text(
        json.dumps(BotState(risk=RiskState(entries_today=4)).to_dict()), encoding="utf-8"
    )
    path.write_text("{not json", encoding="utf-8")

    loaded = load_state(str(path))
    assert loaded.risk.entries_today == 4


def test_live_refuses_fresh_state_when_both_unreadable(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    (tmp_path / "state.json.bak").write_text("also broken", encoding="utf-8")

    with pytest.raises(StateLoadError):
        load_state(str(path), allow_fresh=False, environment="live", dry_run=False)


def test_live_allows_fresh_state_with_opt_in(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    (tmp_path / "state.json.bak").write_text("also broken", encoding="utf-8")

    loaded = load_state(str(path), allow_fresh=True, environment="live", dry_run=False)
    assert loaded.risk.entries_today == 0


def test_sandbox_starts_empty_with_warning(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    (tmp_path / "state.json.bak").write_text("also broken", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="mfpbot.state"):
        loaded = load_state(str(path), allow_fresh=False, environment="sandbox")
    assert loaded.risk.entries_today == 0
    assert any("empty state" in r.message.lower() for r in caplog.records)


def test_dry_run_live_starts_empty(tmp_path):
    """A live dry run never sends orders, so an empty state is acceptable."""
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    (tmp_path / "state.json.bak").write_text("also broken", encoding="utf-8")
    loaded = load_state(str(path), allow_fresh=False, environment="live", dry_run=True)
    assert loaded.risk.entries_today == 0


def test_cli_run_refuses_live_corrupt_state(tmp_path, monkeypatch):
    """A live run must exit 2, not trade, when state and backup are unreadable."""
    from mfpbot.cli import main as cli_main

    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    (tmp_path / "state.json.bak").write_text("also broken", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FP_STATE_FILE", str(path))
    monkeypatch.setenv("FP_ENV", "live")
    monkeypatch.setenv("FP_API_KEY", "fp_live_abc")
    monkeypatch.delenv("FP_ALLOW_FRESH_STATE", raising=False)

    assert cli_main(["run"]) == 2


def test_legacy_single_pending_entry_migrates(tmp_path):
    """State written before the collection format must still load."""
    path = tmp_path / "state.json"
    path.write_text(
        '{"pending_entry": {"market_id": "binance|BTCUSDT", '
        '"client_order_id": "mfpbot:btc:1", "idempotency_key": "key-1", '
        '"sent_at": 1.5, "pre_position_ids": ["pos-old"]}}',
        encoding="utf-8",
    )
    loaded = load_state(path)
    assert list(loaded.pending_entries) == ["mfpbot:btc:1"]
    pending = loaded.pending_entries["mfpbot:btc:1"]
    assert pending.market_id == "binance|BTCUSDT"
    assert pending.pre_position_ids == ["pos-old"]


def test_position_trades_round_trip(tmp_path):
    from mfpbot.state import PositionTrade

    path = tmp_path / "state.json"
    trade = PositionTrade(
        position_id="pos-1", market_id="binance|BTCUSDT", side="long",
        entry=100.0, risk_distance=2.0, take_profit=104.0, last_stop=98.0,
        breakeven_done=True, trailing=False, peak=101.0, entry_order_id="order-1",
    )
    save_state(str(path), BotState(owned_position_ids=["pos-1"], position_trades={"pos-1": trade}))
    reloaded = load_state(str(path))
    assert reloaded.position_trades["pos-1"] == trade


def test_position_trades_absent_in_legacy_files(tmp_path):
    import json

    path = tmp_path / "state.json"
    path.write_text(json.dumps({"owned_position_ids": ["pos-1"]}), encoding="utf-8")
    assert load_state(str(path)).position_trades == {}
