"""Technical indicators implemented without external dependencies."""

from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average. Entries before ``period`` samples are None."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    trs: list[float] = []
    prev_close: float | None = None
    for high, low, close in zip(highs, lows, closes):
        if prev_close is None:
            trs.append(high - low)
        else:
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return trs


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> list[float | None]:
    """Average True Range using Wilder's smoothing."""
    trs = true_ranges(highs, lows, closes)
    out: list[float | None] = [None] * len(trs)
    if period <= 0 or len(trs) < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out
