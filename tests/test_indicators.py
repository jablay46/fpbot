"""Tests for the indicators added for the new strategies."""

from __future__ import annotations

from mfpbot.strategy.indicators import (
    adx,
    donchian,
    ema,
    roc,
    stdev,
    supertrend,
)


def test_donchian_channel_uses_the_lookback_window():
    highs = [5, 6, 7, 8, 9, 10]
    lows = [1, 2, 3, 4, 5, 6]
    upper, lower = donchian(highs, lows, 3)
    # First two bars cannot form a 3-bar window.
    assert upper[0] is None and upper[1] is None
    assert upper[2] == 7 and lower[2] == 1
    assert upper[-1] == 10 and lower[-1] == 4


def test_adx_is_zero_on_a_flat_market_and_high_on_a_trend():
    # Range of zero width would skip every bar, so a flat market still needs a
    # small high/low range for ADX to be defined at all.
    flat_high = [100.5] * 60
    flat_low = [99.5] * 60
    flat_close = [100.0] * 60
    adx_flat = adx(flat_high, flat_low, flat_close, 14)
    assert adx_flat[-1] == 0.0

    up = [100.0 + i for i in range(60)]
    adx_trend = adx(up, [v - 1 for v in up], up, 14)
    assert adx_trend[-1] is not None and adx_trend[-1] > 25.0


def test_adx_needs_enough_bars():
    values = [100.0] * 10
    assert adx(values, values, values, 14) == [None] * 10


def test_supertrend_flips_on_a_reversal():
    down = [100.0 - i for i in range(20)]
    up = [80.0 + i for i in range(20)]
    closes = down + up
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    bands = supertrend(highs, lows, closes, 5, 2.0)
    # Ends in an uptrend after the reversal, and started the series in a downtrend.
    assert bands[-1][0] is True
    assert any(is_up is False for is_up, _ in bands)


def test_supertrend_line_is_none_while_atr_warms_up():
    closes = [100.0, 101.0, 102.0]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    bands = supertrend(highs, lows, closes, 5, 2.0)
    assert bands[-1][1] is None


def test_stdev_and_roc_basic_values():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    sd = stdev(values, 5)
    assert sd[-1] is not None and abs(sd[-1] - 1.4142) < 1e-3
    r = roc(values, 2)
    assert r[0] is None and r[1] is None
    assert abs(r[-1] - (5.0 - 3.0) / 3.0 * 100.0) < 1e-9


def test_ema_still_works_after_the_refactor():
    out = ema([1.0, 2.0, 3.0, 4.0], 2)
    assert out[0] is None and out[1] == 1.5
