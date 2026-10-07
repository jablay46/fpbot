"""EMA crossover strategy with an ATR-based stop and reward:risk take profit.

Emits a signal only on the candle where the fast EMA crosses the slow EMA, so
the bot acts once per crossover rather than on every candle.
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..market_stream import Candle
from .base import BaseStrategy, Signal
from .indicators import atr, ema


class EmaCrossStrategy(BaseStrategy):
    name = "ema_cross"

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        atr_period: int = 14,
        atr_stop_mult: float = 2.0,
        take_profit_rr: float = 2.0,
    ) -> None:
        if fast >= slow:
            raise ValueError("fast period must be smaller than slow period")
        self.fast = fast
        self.slow = slow
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.take_profit_rr = take_profit_rr

    @property
    def min_candles(self) -> int:
        return max(self.slow, self.atr_period) + 2

    def evaluate(self, candles: Sequence[Candle], market: dict) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        fast_series = ema(closes, self.fast)
        slow_series = ema(closes, self.slow)
        atr_series = atr(highs, lows, closes, self.atr_period)

        fast_now, fast_prev = fast_series[-1], fast_series[-2]
        slow_now, slow_prev = slow_series[-1], slow_series[-2]
        atr_now = atr_series[-1]
        if None in (fast_now, fast_prev, slow_now, slow_prev, atr_now) or atr_now <= 0:
            return None

        price = closes[-1]
        stop_distance = atr_now * self.atr_stop_mult
        if stop_distance <= 0:
            return None

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up:
            stop = price - stop_distance
            return Signal(
                action="long",
                reason=f"EMA{self.fast} crossed above EMA{self.slow}",
                stop_price=stop,
                take_profit_price=price + (price - stop) * self.take_profit_rr,
            )
        if crossed_down:
            stop = price + stop_distance
            return Signal(
                action="short",
                reason=f"EMA{self.fast} crossed below EMA{self.slow}",
                stop_price=stop,
                take_profit_price=price - (stop - price) * self.take_profit_rr,
            )
        return None
