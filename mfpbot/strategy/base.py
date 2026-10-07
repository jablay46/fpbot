"""Strategy interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from ..market_stream import Candle


@dataclass
class Signal:
    """A desired position direction produced from closed candles.

    ``action`` is one of ``long``, ``short`` or ``flat``. ``stop_price`` and
    ``take_profit_price`` are absolute prices when the strategy can provide them.
    """

    action: str
    reason: str
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None


class BaseStrategy:
    name = "base"

    def evaluate(self, candles: Sequence[Candle], market: dict) -> Optional[Signal]:
        """Return a Signal when the latest closed candle produces one, else None."""
        raise NotImplementedError
