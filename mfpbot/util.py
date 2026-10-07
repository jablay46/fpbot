"""Small numeric and time helpers shared across the bot."""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, Decimal
from typing import Optional

_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_INTERVAL_UNITS_MS = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}


def parse_interval_ms(interval: str) -> int:
    """Convert a candle interval like ``15m`` or ``1h`` to milliseconds.

    Returns 0 for anything unparseable so callers can treat it as "unknown".
    """
    match = _INTERVAL_RE.match(interval or "")
    if not match:
        return 0
    return int(match.group(1)) * _INTERVAL_UNITS_MS[match.group(2).lower()]


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
