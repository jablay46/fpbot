"""Supertrend (ATR trailing band) strategy.

Fires when the trend line flips. Compared with an EMA cross the same period
this signals far less often in a range: the band only flips after the close
actually crosses an ATR-scaled level. The stop is the band's own line, floored
at a minimum fraction of the entry so a flip candle cannot produce a
near-zero-risk order.
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..market_stream import Candle
from .base import BaseStrategy, Signal
from .indicators import supertrend


class SupertrendStrategy(BaseStrategy):
    name = "supertrend"

    def __init__(
        self,
        supertrend_period: int = 10,
        supertrend_mult: float = 3.0,
        take_profit_rr: float = 2.0,
        min_stop_frac: float = 0.001,
    ) -> None:
        self.period = supertrend_period
        self.multiplier = supertrend_mult
        self.take_profit_rr = take_profit_rr
        self.min_stop_frac = min_stop_frac

    @property
    def min_candles(self) -> int:
        return self.period + 3

    def evaluate(self, candles: Sequence[Candle], market: dict) -> Optional[Signal]:
        if len(candles) < self.min_candles:
            return None

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        bands = supertrend(highs, lows, closes, self.period, self.multiplier)
        up_now, line_now = bands[-1]
        up_prev, line_prev = bands[-2]
        if line_now is None or line_prev is None:
            return None
        if up_now == up_prev:
            return None  # only act on the flip candle

        price = closes[-1]
        min_distance = price * self.min_stop_frac
        if up_now:
            distance = max(price - line_now, min_distance)
            return Signal(
                action="long",
                reason=f"supertrend flipped up at {line_now}",
                stop_price=price - distance,
                take_profit_price=price + distance * self.take_profit_rr,
            )
        distance = max(line_now - price, min_distance)
        return Signal(
            action="short",
            reason=f"supertrend flipped down at {line_now}",
            stop_price=price + distance,
            take_profit_price=price - distance * self.take_profit_rr,
        )
