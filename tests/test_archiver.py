"""Tests for the candle archiver's append/dedup behaviour (no network)."""

from __future__ import annotations

from mfpbot.archiver import CandleArchiver
from mfpbot.backtest import load_bars
from mfpbot.market_stream import Candle


def _candle(open_time: int, close: float, *, final: bool = True) -> Candle:
    return Candle(
        provider="binance", symbol="BTCUSDT", interval="15m",
        open_time=open_time, close_time=open_time + 899_999,
        open=close, high=close + 1, low=close - 1, close=close,
        volume=1.0, is_final=final,
    )


def test_archiver_writes_final_candles_once_and_skips_forming(tmp_path):
    path = tmp_path / "bars.jsonl"
    arch = CandleArchiver(path)
    assert arch.write(_candle(1, 100)) is True
    assert arch.write(_candle(1, 100)) is False      # duplicate bar
    assert arch.write(_candle(2, 101, final=False)) is False  # forming bar
    assert arch.write(_candle(2, 101)) is True
    arch.close()

    loaded = load_bars(path)
    assert [b.open_time for b in loaded] == [1, 2]


def test_archiver_resumes_without_rewriting_existing_bars(tmp_path):
    path = tmp_path / "bars.jsonl"
    CandleArchiver(path).write(_candle(1, 100))
    # A fresh archiver must see bar 1 already on disk and not duplicate it.
    arch = CandleArchiver(path)
    assert arch.write(_candle(1, 100)) is False
    assert arch.write(_candle(2, 101)) is True
    arch.close()
    assert len(load_bars(path)) == 2


def test_config_parses_the_new_strategy_parameters(monkeypatch):
    from mfpbot.config import load_config

    monkeypatch.setenv("FP_ENV", "sandbox")
    monkeypatch.setenv("FP_ACCOUNT_ID", "FP-TEST")
    monkeypatch.setenv("FP_STRATEGY", "donchian_breakout")
    monkeypatch.setenv("FP_DONCHIAN_PERIOD", "30")
    monkeypatch.setenv("FP_REGIME_ADX_MIN", "20")
    monkeypatch.setenv("FP_TREND_EMA", "200")
    monkeypatch.setenv("FP_SUPERTREND_MULT", "2.5")
    cfg = load_config(require_key=False)
    assert cfg.strategy == "donchian_breakout"
    assert cfg.donchian_period == 30
    assert cfg.regime_adx_min == 20.0
    assert cfg.trend_ema == 200
    assert cfg.supertrend_mult == 2.5


def test_config_rejects_a_bad_donchian_period(monkeypatch):
    from mfpbot.config import ConfigError, load_config

    monkeypatch.setenv("FP_ENV", "sandbox")
    monkeypatch.setenv("FP_ACCOUNT_ID", "FP-TEST")
    monkeypatch.setenv("FP_DONCHIAN_PERIOD", "0")
    try:
        load_config(require_key=False)
    except ConfigError as exc:
        assert "FP_DONCHIAN_PERIOD" in str(exc)
    else:
        raise AssertionError("expected ConfigError")

