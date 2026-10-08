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


def _wilder(values: list[float], period: int) -> list[float | None]:
    """Wilder's smoothing over a list already aligned so values[0] is the first sample."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    prev = sum(values[:period])
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = prev - (prev / period) + values[i]
        out[i] = prev
    return out


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    """Average Directional Index (Wilder). High values mark trending regimes.

    Returns ``None`` until enough bars accumulate. A regime filter built on this
    keeps a trend system out of the choppy ranges where an EMA cross whipsaws.
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n < 2 * period + 1:
        return out

    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    tr_s = _wilder(trs, period)
    plus_s = _wilder(plus_dm, period)
    minus_s = _wilder(minus_dm, period)

    dx: list[float] = []
    dx_index: list[int] = []
    for j in range(len(trs)):
        tr_v, p_v, m_v = tr_s[j], plus_s[j], minus_s[j]
        if tr_v is None or p_v is None or m_v is None or tr_v == 0:
            continue
        plus_di = 100.0 * p_v / tr_v
        minus_di = 100.0 * m_v / tr_v
        denom = plus_di + minus_di
        # A flat market has directional movement of zero on both sides; that is
        # no trend (DX=0), not a perfectly trending one.
        dx.append(100.0 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0)
        dx_index.append(j + 1)  # index in the original series (DM starts at bar 1)

    if len(dx) < period:
        return out
    prev = sum(dx[:period]) / period
    out[dx_index[period - 1]] = prev
    for k in range(period, len(dx)):
        prev = (prev * (period - 1) + dx[k]) / period
        out[dx_index[k]] = prev
    return out


def donchian(
    highs: Sequence[float],
    lows: Sequence[float],
    period: int,
) -> tuple[list[float | None], list[float | None]]:
    """Rolling Donchian channel: highest high / lowest low of the last ``period`` bars.

    ``upper[i]``/``lower[i]`` use bars ``i-period+1..i`` inclusive. A breakout
    strategy compares the *current* close against the channel computed through
    the *previous* bar to avoid look-ahead.
    """
    n = len(highs)
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    if period <= 0:
        return upper, lower
    for i in range(period - 1, n):
        window_h = highs[i - period + 1: i + 1]
        window_l = lows[i - period + 1: i + 1]
        upper[i] = max(window_h)
        lower[i] = min(window_l)
    return upper, lower


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> list[tuple[bool, float | None]]:
    """Supertrend bands: ``(is_uptrend, line)`` per bar.

    An ATR trailing stop that flips regime when the close crosses the band. It
    behaves like a calmer EMA cross: fewer signals, and the stop is the trend
    line itself. ``line`` is ``None`` while the ATR is still warming up.
    """
    n = len(closes)
    out: list[tuple[bool, float | None]] = [(True, None)] * n
    atr_series = atr(highs, lows, closes, period)
    if period <= 0:
        return out

    trend_up = True
    final_upper: float | None = None
    final_lower: float | None = None
    for i in range(n):
        a = atr_series[i]
        if a is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        basic_upper = hl2 + multiplier * a
        basic_lower = hl2 - multiplier * a
        close = closes[i]

        if final_upper is None:
            final_upper, final_lower = basic_upper, basic_lower
        else:
            final_upper = basic_upper if (basic_upper < final_upper or closes[i - 1] > final_upper) else final_upper
            final_lower = basic_lower if (basic_lower > final_lower or closes[i - 1] < final_lower) else final_lower

        if trend_up:
            if close < final_lower:
                trend_up = False
            else:
                pass
        else:
            if close > final_upper:
                trend_up = True
        line = final_lower if trend_up else final_upper
        out[i] = (trend_up, line)
    return out


def stdev(values: Sequence[float], period: int) -> list[float | None]:
    """Rolling population standard deviation."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 1:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        out[i] = var ** 0.5
    return out


def roc(values: Sequence[float], period: int) -> list[float | None]:
    """Rate of change in percent over ``period`` bars."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0:
        return out
    for i in range(period, n):
        base = values[i - period]
        out[i] = ((values[i] - base) / base * 100.0) if base else None
    return out
