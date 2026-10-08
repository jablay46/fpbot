"""Walk-forward validation and strategy comparison.

A single in-sample number is how strategies get overfit. This splits the bars
into consecutive folds and reports each fold's metrics plus the fraction of
folds that are profitable, so a result has to survive out-of-sample to count.
``compare`` runs several strategies over the same bars and ranks them by
drawdown-adjusted return (MAR).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .data import Bar
from .engine import BacktestConfig, Backtester, Result


@dataclass
class Fold:
    index: int
    start: int
    end: int
    result: Result


@dataclass
class WalkForward:
    folds: list[Fold] = field(default_factory=list)

    @property
    def profitable_fraction(self) -> float:
        if not self.folds:
            return 0.0
        good = sum(1 for f in self.folds if f.result.metrics.total_return_pct > 0)
        return good / len(self.folds)

    @property
    def median_return_pct(self) -> float:
        if not self.folds:
            return 0.0
        rets = sorted(f.result.metrics.total_return_pct for f in self.folds)
        return rets[len(rets) // 2]

    @property
    def worst_drawdown_pct(self) -> float:
        return max((f.result.metrics.max_drawdown_pct for f in self.folds), default=0.0)


def walk_forward(
    strategy_factory,
    bars: Sequence[Bar],
    config: BacktestConfig,
    *,
    folds: int = 4,
    market: dict | None = None,
) -> WalkForward:
    """Run ``strategy_factory()`` on ``folds`` consecutive slices of ``bars``."""
    if folds < 1:
        raise ValueError("folds must be >= 1")
    n = len(bars)
    if n == 0:
        return WalkForward()
    size = n // folds
    wf = WalkForward()
    for k in range(folds):
        start = k * size
        end = n if k == folds - 1 else (k + 1) * size
        slice_bars = bars[start:end]
        result = Backtester(strategy_factory(), config, market=market).run(slice_bars)
        wf.folds.append(Fold(index=k, start=start, end=end, result=result))
    return wf


def compare(
    factories: dict,
    bars: Sequence[Bar],
    config: BacktestConfig,
    *,
    market: dict | None = None,
) -> list[tuple[str, Result]]:
    """Backtest each factory over the same bars; return them ranked by MAR."""
    results = [
        (name, Backtester(factory(), config, market=market).run(bars))
        for name, factory in factories.items()
    ]
    results.sort(key=lambda item: item[1].metrics.mar, reverse=True)
    return results
