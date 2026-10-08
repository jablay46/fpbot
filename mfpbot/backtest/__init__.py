"""Backtesting and walk-forward validation for the trading strategies."""

from .costs import CostModel
from .data import Bar, load_bars, load_many, save_bars_jsonl
from .engine import BacktestConfig, Backtester, Result, Trade
from .metrics import Metrics, compute_metrics
from .walkforward import Fold, WalkForward, compare, walk_forward

__all__ = [
    "CostModel",
    "Bar",
    "load_bars",
    "load_many",
    "save_bars_jsonl",
    "BacktestConfig",
    "Backtester",
    "Result",
    "Trade",
    "Metrics",
    "compute_metrics",
    "Fold",
    "WalkForward",
    "walk_forward",
    "compare",
]
