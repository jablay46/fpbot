"""Trading strategies and indicators."""

from .base import BaseStrategy, Signal
from .donchian_breakout import DonchianBreakoutStrategy
from .ema_cross import EmaCrossStrategy
from .supertrend import SupertrendStrategy

STRATEGIES = {
    EmaCrossStrategy.name: EmaCrossStrategy,
    DonchianBreakoutStrategy.name: DonchianBreakoutStrategy,
    SupertrendStrategy.name: SupertrendStrategy,
}


def build_strategy(name: str, **kwargs) -> BaseStrategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"Unknown strategy {name!r}. Available: {sorted(STRATEGIES)}") from None
    # Only pass the kwargs the strategy accepts, so one shared config can drive
    # any strategy without every strategy declaring every parameter.
    import inspect

    accepted = set(inspect.signature(cls.__init__).parameters) - {"self"}
    return cls(**{k: v for k, v in kwargs.items() if k in accepted})


__all__ = [
    "BaseStrategy",
    "Signal",
    "EmaCrossStrategy",
    "DonchianBreakoutStrategy",
    "SupertrendStrategy",
    "STRATEGIES",
    "build_strategy",
]
