"""Performance metrics for a backtest, all after costs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional, Sequence


@dataclass
class Metrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_return_pct: float = 0.0
    final_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    max_daily_drawdown_pct: float = 0.0
    cagr_pct: float = 0.0
    sharpe: float = 0.0
    mar: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _daily_equity_marks(equity_curve: Sequence[tuple[float, float]]) -> list[float]:
    """Last equity value per UTC day from a (timestamp_ms, equity) curve."""
    by_day: dict[int, float] = {}
    for ts_ms, eq in equity_curve:
        day = int(ts_ms // 86_400_000)
        by_day[day] = eq  # curve is chronological, so the last wins
    return [by_day[d] for d in sorted(by_day)]


def compute_metrics(
    equity_curve: Sequence[tuple[float, float]],
    pnls: Sequence[float],
    *,
    starting_equity: float,
    periods_per_year: float,
) -> Metrics:
    m = Metrics(trades=len(pnls), final_equity=starting_equity)
    if not equity_curve:
        return m

    m.final_equity = equity_curve[-1][1]
    m.total_return_pct = (m.final_equity - starting_equity) / starting_equity * 100.0

    # Max drawdown on the equity curve.
    peak = equity_curve[0][1]
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100.0 if peak else 0.0
        max_dd = max(max_dd, dd)
    m.max_drawdown_pct = max_dd

    daily = _daily_equity_marks(equity_curve)
    peak_d = daily[0] if daily else starting_equity
    max_ddd = 0.0
    for eq in daily:
        peak_d = max(peak_d, eq)
        ddd = (peak_d - eq) / peak_d * 100.0 if peak_d else 0.0
        max_ddd = max(max_ddd, ddd)
    m.max_daily_drawdown_pct = max_ddd

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    m.wins, m.losses = len(wins), len(losses)
    m.win_rate = len(wins) / len(pnls) * 100.0 if pnls else 0.0
    m.avg_win = sum(wins) / len(wins) if wins else 0.0
    m.avg_loss = sum(losses) / len(losses) if losses else 0.0
    gains = sum(wins)
    gross_loss = abs(sum(losses))
    m.profit_factor = gains / gross_loss if gross_loss else (float("inf") if gains else 0.0)
    m.expectancy = sum(pnls) / len(pnls) if pnls else 0.0

    # Per-bar returns from the equity curve for the Sharpe estimate.
    rets: list[float] = []
    for (_, prev), (_, cur) in zip(equity_curve, equity_curve[1:]):
        if prev:
            rets.append((cur - prev) / prev)
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = var ** 0.5
        if sd > 0:
            m.sharpe = mean / sd * (periods_per_year ** 0.5)

    # CAGR from the span of the curve (in years). Short synthetic curves would
    # otherwise raise the ratio to an enormous power, so require a real span.
    span_ms = equity_curve[-1][0] - equity_curve[0][0]
    if span_ms > 0 and starting_equity > 0 and m.final_equity > 0:
        years = span_ms / (365.25 * 86_400_000)
        if years >= 1.0 / 365.0:
            try:
                m.cagr_pct = ((m.final_equity / starting_equity) ** (1.0 / years) - 1.0) * 100.0
            except OverflowError:
                m.cagr_pct = 0.0
    m.mar = m.cagr_pct / m.max_drawdown_pct if m.max_drawdown_pct else 0.0
    return m
