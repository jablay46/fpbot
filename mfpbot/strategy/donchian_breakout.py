"""Donchian channel breakout with an ATR stop and a regime filter.

Enters when a closed candle closes beyond the previous ``period``-bar channel,
so the entry level is a level the market has not traded through recently rather
than a moving-average intersection. Two optional filters keep it out of chop:

* ``regime_adx_min`` — skip unless ADX is at or above this (trending only).
* ``trend_ema`` — only take longs above the EMA and shorts below it.

The stop is ``atr_stop_mult`` ATR from the entry; the take profit is
``take_profit_rr`` times the stop distance, matching the EMA-cross contract so
the existing bot and backtester need no change.
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..market_stream import Candle
from .base import BaseStrategy, Signal
from .indicators import adx, atr, donchian, ema


class DonchianBreakoutStrategy(BaseStrategy):
    name = "donchian_breakout"

    def __init__(
        self,
        donchian_period: int = 20,
        atr_period: int = 14,
        atr_stop_mult: float = 2.0,
        take_profit_rr: float = 2.0,
        regime_adx_min: float = 0.0,
        trend_ema: int = 0,
    ) -> None:
        if donchian_period < 1:
            raise ValueError("donchian_period must be >= 1")
        self.period = donchian_period
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.take_profit_rr = take_profit_rr
        self.regime_adx_min = regime_adx_min
        self.trend_ema = trend_ema

    @property
    def min_candles(self) -> int:
        return max(self.period, self.atr_period, self.trend_ema) + 3

    def evaluate(self, candles: Sequence[Candle], market: dict) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        atr_series = atr(highs, lows, closes, self.atr_period)
        atr_now = atr_series[-1]
        if atr_now is None or atr_now <= 0:
            return None

        upper, lower = donchian(highs, lows, self.period)
        # Channel through the *previous* bar: the bar being tested must not be
        # part of its own breakout level (no look-ahead).
        upper_prev = upper[-2]
        lower_prev = lower[-2]
        if upper_prev is None or lower_prev is None:
            return None

        bought = closes[-2] <= upper_prev and closes[-1] > upper_prev
        sold = closes[-2] >= lower_prev and closes[-1] < lower_prev
        if not bought and not sold:
            return None

        if self.regime_adx_min > 0:
            adx_series = adx(highs, lows, closes, 14)
            adx_now = adx_series[-1]
            if adx_now is None or adx_now < self.regime_adx_min:
                return None

        if self.trend_ema > 0:
            ema_series = ema(closes, self.trend_ema)
            ema_now = ema_series[-1]
            if ema_now is None:
                return None
            if bought and closes[-1] <= ema_now:
                return None
            if sold and closes[-1] >= ema_now:
                return None

        price = closes[-1]
        stop_distance = atr_now * self.atr_stop_mult
        if stop_distance <= 0:
            return None

        if bought:
            return Signal(
                action="long",
                reason=f"close broke the {self.period}-bar high {upper_prev}",
                stop_price=price - stop_distance,
                take_profit_price=price + stop_distance * self.take_profit_rr,
            )
        return Signal(
            action="short",
            reason=f"close broke the {self.period}-bar low {lower_prev}",
            stop_price=price + stop_distance,
            take_profit_price=price - stop_distance * self.take_profit_rr,
        )
