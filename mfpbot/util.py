"""Small numeric and time helpers shared across the bot."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Optional


def round_step(value: float, step: float, *, mode: str = "down") -> float:
    """Round ``value`` to the nearest multiple of ``step`` using Decimal math.

    Order sizes must be exact multiples of a market's ``size_step``; float
    division would introduce precision errors, so use Decimal.
    """
    if step <= 0:
        return value
    rounding = ROUND_DOWN if mode == "down" else None
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    units = (d_value / d_step).to_integral_value(rounding=rounding)
    return float(units * d_step)


def fmt(value: Optional[float], places: int = 8) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def pct(part: float, whole: float) -> float:
    if whole == 0:
        return 0.0
    return (part / whole) * 100.0
