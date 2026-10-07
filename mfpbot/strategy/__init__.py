"""Trading strategies and indicators."""

from .base import BaseStrategy, Signal
from .ema_cross import EmaCrossStrategy

STRATEGIES = {
    EmaCrossStrategy.name: EmaCrossStrategy,
}


def build_strategy(name: str, **kwargs) -> BaseStrategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"Unknown strategy {name!r}. Available: {sorted(STRATEGIES)}") from None
    return cls(**kwargs)


__all__ = ["BaseStrategy", "Signal", "EmaCrossStrategy", "STRATEGIES", "build_strategy"]
