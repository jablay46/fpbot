"""Tests for indicators and the EMA cross strategy."""

from __future__ import annotations

from mfpbot.strategy import build_strategy
from mfpbot.strategy.indicators import atr, ema
from tests.conftest import MARKET, make_candles


def test_ema_seed_and_length():
    values = [float(i) for i in range(1, 11)]
    out = ema(values, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == 2.0  # mean of 1,2,3
    assert out[-1] is not None


def test_ema_insufficient_data():
    assert ema([1.0, 2.0], 5) == [None, None]


def test_atr_wilder_smoothing():
    highs = [10, 11, 12, 13, 14]
    lows = [9, 10, 11, 12, 13]
    closes = [9.5, 10.5, 11.5, 12.5, 13.5]
    out = atr(highs, lows, closes, 3)
    assert out[0] is None and out[1] is None
    assert out[2] is not None


def test_ema_cross_emits_long_on_upward_cross():
    strat = build_strategy("ema_cross", fast=2, slow=4, atr_period=2, atr_stop_mult=2.0, take_profit_rr=2.0)
    # Downtrend then a sharp turn up; the bullish cross lands on the last candle.
    closes = [100, 95, 90, 85, 80, 81, 95]
    signal = strat.evaluate(make_candles(closes), MARKET)
    assert signal is not None
    assert signal.action == "long"
    assert signal.stop_price < closes[-1]
    assert signal.take_profit_price > closes[-1]


def test_ema_cross_emits_short_on_downward_cross():
    strat = build_strategy("ema_cross", fast=2, slow=4, atr_period=2, atr_stop_mult=2.0, take_profit_rr=2.0)
    closes = [100, 105, 110, 115, 120, 119, 105]
    signal = strat.evaluate(make_candles(closes), MARKET)
    assert signal is not None
    assert signal.action == "short"
    assert signal.stop_price > closes[-1]
    assert signal.take_profit_price < closes[-1]


def test_ema_cross_quiet_when_not_enough_data():
    strat = build_strategy("ema_cross", fast=2, slow=4, atr_period=2)
    assert strat.evaluate(make_candles([10, 11, 12]), MARKET) is None
