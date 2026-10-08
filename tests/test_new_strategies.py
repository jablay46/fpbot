"""Tests for the Donchian breakout and Supertrend strategies."""

from __future__ import annotations

import pytest

from mfpbot.strategy import build_strategy
from tests.conftest import MARKET, make_candles


def test_build_strategy_routes_only_accepted_kwargs():
    # A shared config passes every strategy's params; each must ignore the rest.
    strat = build_strategy(
        "donchian_breakout",
        fast=12, slow=26, donchian_period=5, atr_period=3, trend_ema=3,
        supertrend_period=10, supertrend_mult=3.0,
    )
    assert strat.period == 5
    strat2 = build_strategy("supertrend", supertrend_period=7, supertrend_mult=2.5, donchian_period=99)
    assert strat2.period == 7 and strat2.multiplier == 2.5


def test_donchian_emits_long_on_a_close_above_the_channel():
    strat = build_strategy("donchian_breakout", donchian_period=3, atr_period=2, atr_stop_mult=2.0)
    # Flat then a decisive new high on the last close.
    closes = [100, 100, 100, 100, 100, 110]
    signal = strat.evaluate(make_candles(closes), MARKET)
    assert signal is not None and signal.action == "long"
    assert signal.stop_price < closes[-1]
    assert signal.take_profit_price > closes[-1]


def test_donchian_emits_short_on_a_close_below_the_channel():
    strat = build_strategy("donchian_breakout", donchian_period=3, atr_period=2, atr_stop_mult=2.0)
    closes = [100, 100, 100, 100, 100, 90]
    signal = strat.evaluate(make_candles(closes), MARKET)
    assert signal is not None and signal.action == "short"
    assert signal.stop_price > closes[-1]


def test_donchian_is_silent_inside_the_channel():
    strat = build_strategy("donchian_breakout", donchian_period=5, atr_period=3)
    closes = [100, 101, 99, 100, 101, 100, 100]
    assert strat.evaluate(make_candles(closes), MARKET) is None


def test_donchian_regime_filter_blocks_a_low_adx_breakout():
    strat = build_strategy("donchian_breakout", donchian_period=3, atr_period=2, regime_adx_min=99.0)
    closes = [100, 100, 100, 100, 100, 110]
    # ADX can never reach 99 on this short series, so the breakout is filtered out.
    assert strat.evaluate(make_candles(closes), MARKET) is None


def test_donchian_trend_filter_allows_long_above_ema_only():
    strat = build_strategy("donchian_breakout", donchian_period=3, atr_period=2, trend_ema=4)
    # A steady climb keeps the close above a 4-bar EMA, so the breakout passes.
    closes = [100, 105, 110, 115, 120, 125, 130]
    signal = strat.evaluate(make_candles(closes), MARKET)
    assert signal is not None and signal.action == "long"


def test_donchian_rejects_bad_period():
    with pytest.raises(ValueError):
        build_strategy("donchian_breakout", donchian_period=0)


def test_supertrend_emits_a_signal_on_a_flip():
    strat = build_strategy("supertrend", supertrend_period=3, supertrend_mult=1.0)
    # The up-flip lands on the final bar for this pattern (verified against the
    # indicator), so ``evaluate`` sees a fresh flip and fires.
    down = [100 - i * 3 for i in range(6)]
    up = [down[-1] + i * 3 for i in range(3)]
    signal = strat.evaluate(make_candles(down + up), MARKET)
    assert signal is not None
    assert signal.stop_price is not None and signal.take_profit_price is not None


def test_supertrend_is_silent_while_atr_warms_up():
    strat = build_strategy("supertrend", supertrend_period=10, supertrend_mult=3.0)
    assert strat.evaluate(make_candles([100, 101, 102]), MARKET) is None


def test_supertrend_stop_respects_the_min_fraction():
    strat = build_strategy("supertrend", supertrend_period=3, supertrend_mult=1.0, min_stop_frac=0.05)
    down = [100 - i * 3 for i in range(6)]
    up = [down[-1] + i * 3 for i in range(3)]
    candles = make_candles(down + up)
    signal = strat.evaluate(candles, MARKET)
    assert signal is not None
    if signal.action == "long":
        assert signal.stop_price <= candles[-1].close * 0.95 + 1e-9
    else:
        assert signal.stop_price >= candles[-1].close * 1.05 - 1e-9
