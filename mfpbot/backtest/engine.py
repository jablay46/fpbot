"""Bar-by-bar backtest engine with the same risk guards as the live bot.

The engine walks closed bars in order and, for each bar, asks the strategy for a
signal computed from bars up to and including that bar. It then:

* manages an open position against the bar's high/low (stop first when both the
  stop and the take profit fall inside one bar — the conservative reading), and
* opens a new position at the signal bar's close when flat and allowed.

Every fill pays the cost model's slippage and commission, and holding pays
swap, so the reported equity is net of cost. The daily-loss, cumulative-drawdown
and daily-trade guards mirror ``RiskManager`` so a strategy is judged under the
same constraints it will trade under.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from ..risk.sizing import PositionSizer
from ..strategy.indicators import atr as atr_series
from .costs import CostModel
from .data import Bar
from .metrics import Metrics, compute_metrics


def _bar_day(open_time_ms: int, tz: str) -> int:
    """Day bucket for a bar's open time in the given IANA timezone.

    Matches the live bot's day boundary (default ``America/New_York``) so the
    daily guard trips on the same bars it would guard live. Falls back to UTC
    when the zone is unavailable.
    """
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc
    moment = datetime.fromtimestamp(open_time_ms / 1000.0, tz=timezone.utc).astimezone(zone)
    return moment.toordinal()


@dataclass
class BacktestConfig:
    starting_equity: float = 100_000.0
    risk_per_trade_pct: float = 1.0
    leverage: float = 2.0
    max_margin_pct: float = 50.0
    atr_period: int = 14
    # Exit management, mirroring the live bot (0 disables each). The breakeven
    # move arms when a bar's extreme reaches breakeven_at_r * R and ratchets
    # the stop to entry +/- breakeven_plus_r * R; the trailing stop ratchets to
    # peak -/+ trail_atr_mult * ATR. Both are evaluated on closed bars, after
    # that bar's exit check, exactly like the live per-candle manager.
    breakeven_at_r: float = 0.0
    breakeven_plus_r: float = 0.1
    trail_atr_mult: float = 0.0
    # Day boundary for the daily guard; keep in sync with the live bot.
    day_timezone: str = "America/New_York"
    # Guards (0 disables). Percentages are of the stated basis.
    max_daily_loss_pct: float = 0.0
    max_total_drawdown_pct: float = 0.0
    drawdown_basis: str = "starting"
    max_daily_trades: int = 0
    allow_reversal: bool = False
    costs: CostModel = field(default_factory=CostModel)


@dataclass
class Trade:
    entry_time: int
    exit_time: int
    side: str
    entry: float
    exit: float
    size: float
    notional: float
    pnl: float
    fees: float
    reason: str


@dataclass
class Result:
    metrics: Metrics
    trades: list[Trade]
    equity_curve: list[tuple[float, float]]
    halt_reason: str = ""


def _interval_ms(bars: Sequence[Bar]) -> float:
    gaps = [b.open_time - a.open_time for a, b in zip(bars, bars[1:]) if b.open_time > a.open_time]
    if not gaps:
        return 60_000.0
    gaps.sort()
    return float(gaps[len(gaps) // 2])


class Backtester:
    def __init__(self, strategy, config: BacktestConfig, *, market: Optional[dict] = None) -> None:
        self.strategy = strategy
        self.cfg = config
        self.market = market or {}
        self.costs = config.costs

    def run(self, bars: Sequence[Bar]) -> Result:
        if len(bars) < getattr(self.strategy, "min_candles", 2):
            return Result(Metrics(final_equity=self.cfg.starting_equity), [], [], "")

        cash = self.cfg.starting_equity
        equity_curve: list[tuple[float, float]] = []
        trades: list[Trade] = []
        pnls: list[float] = []

        position: Optional[dict] = None          # open position state
        day = None
        day_start_equity = self.cfg.starting_equity
        entries_today = 0
        peak_equity = self.cfg.starting_equity
        dd_halted = False
        halt_reason = ""

        atr_values = atr_series(
            [b.high for b in bars], [b.low for b in bars], [b.close for b in bars],
            self.cfg.atr_period,
        )

        for i in range(1, len(bars)):
            bar = bars[i]
            window = bars[: i + 1]

            # 1) Manage an open position against this bar's range.
            if position is not None:
                exit_price, exit_reason = self._check_exit(position, bar)
                if exit_price is not None:
                    trade = self._close(position, exit_price, bar.open_time, exit_reason)
                    cash += trade.pnl
                    trades.append(trade)
                    pnls.append(trade.pnl)
                    position = None
                else:
                    # Still open: ratchet the stop from this closed bar, exactly
                    # like the live per-candle manager. The exit check above
                    # runs first with the incoming stop, so a bar that spans
                    # both the old stop and a management trigger conservatively
                    # fills the stop (no intrabar look-ahead).
                    self._update_exits(position, bar, atr_values[i])

            # 2) Mark equity (realized + unrealized) and record the curve.
            equity = cash + self._unrealized(position, bar.close)
            equity_curve.append((bar.close_time, equity))
            peak_equity = max(peak_equity, equity)

            # 3) Day rollover and guards.
            bar_day = _bar_day(bar.open_time, self.cfg.day_timezone)
            if bar_day != day:
                day = bar_day
                day_start_equity = equity
                entries_today = 0
                if halt_reason == "daily loss":
                    halt_reason = ""
            if self.cfg.max_total_drawdown_pct > 0:
                basis = peak_equity if self.cfg.drawdown_basis == "peak" else self.cfg.starting_equity
                if basis > 0 and (basis - equity) / basis * 100.0 >= self.cfg.max_total_drawdown_pct:
                    if not dd_halted:
                        dd_halted = True
                        halt_reason = "total drawdown"
            if self.cfg.max_daily_loss_pct > 0 and day_start_equity > 0:
                if (day_start_equity - equity) / day_start_equity * 100.0 >= self.cfg.max_daily_loss_pct:
                    halt_reason = "daily loss"

            # 4) Consider a new entry (only when flat).
            signal = self.strategy.evaluate(window, self.market)
            if position is None and signal is not None:
                if dd_halted or halt_reason:
                    continue
                if self.cfg.max_daily_trades and entries_today >= self.cfg.max_daily_trades:
                    continue
                position = self._open(signal, bar, equity)
                if position is not None:
                    entries_today += 1

        # Reflect any still-open position at the last close (mark-to-market).
        if position is not None:
            last = bars[-1]
            equity = cash + self._unrealized(position, last.close)
            equity_curve.append((last.close_time, equity))

        intervals = _interval_ms(bars)
        periods_per_year = (365.25 * 24 * 3600 * 1000) / intervals
        metrics = compute_metrics(
            equity_curve, pnls,
            starting_equity=self.cfg.starting_equity,
            periods_per_year=periods_per_year,
        )
        return Result(metrics=metrics, trades=trades, equity_curve=equity_curve, halt_reason=halt_reason)

    # -- position lifecycle ---------------------------------------------

    def _open(self, signal, bar: Bar, equity: float) -> Optional[dict]:
        if signal.stop_price is None:
            return None
        side = "long" if signal.action == "long" else "short"
        entry = self.costs.entry_price(bar.close, side)
        stop = signal.stop_price
        # Size with the same sizer the live bot uses, so fills the live bot
        # would reject (below min size/notional, off-step, over margin) are
        # skipped here too instead of flattering the backtest.
        sizer = PositionSizer(
            risk_per_trade_pct=self.cfg.risk_per_trade_pct,
            leverage=self.cfg.leverage,
            min_notional=float(self.market.get("min_notional") or 0.0),
            min_size=float(self.market.get("min_size") or 0.0),
            size_step=float(self.market.get("size_step") or 0.0),
            max_leverage=self.market.get("max_leverage"),
            available_balance=equity * (self.cfg.max_margin_pct / 100.0),
        )
        sizing = sizer.size(equity=equity, entry=entry, stop=stop)
        if not sizing.ok:
            return None
        tp = signal.take_profit_price
        return {
            "side": side, "entry": entry, "stop": stop, "tp": tp,
            "size": sizing.size, "entry_time": bar.open_time,
            "risk_distance": abs(entry - stop),
            "stop_source": "initial",  # initial | breakeven | trailing
            "peak": None,
        }

    def _update_exits(self, position: dict, bar: Bar, atr_now: Optional[float]) -> None:
        """Ratchet a position's stop from one closed bar (breakeven + trailing).

        Mirrors ``Bot._manage_exits``: the trigger uses the bar's extreme, the
        stop only ever tightens, and the take profit is left alone.
        """
        side = position["side"]
        is_long = side == "long"
        risk = position["risk_distance"]
        if risk <= 0:
            return
        entry = position["entry"]
        extreme = bar.high if is_long else bar.low
        excursion = (extreme - entry) if is_long else (entry - extreme)
        desired = position["stop"]
        source = position["stop_source"]
        if (
            self.cfg.breakeven_at_r > 0
            and source == "initial"
            and excursion >= self.cfg.breakeven_at_r * risk
        ):
            if is_long:
                desired = entry + self.cfg.breakeven_plus_r * risk
            else:
                desired = entry - self.cfg.breakeven_plus_r * risk
            source = "breakeven"
        if self.cfg.trail_atr_mult > 0 and atr_now is not None and atr_now > 0:
            peak = position["peak"]
            if peak is None:
                peak = extreme
            else:
                peak = max(peak, extreme) if is_long else min(peak, extreme)
            position["peak"] = peak
            trail = peak - self.cfg.trail_atr_mult * atr_now if is_long else \
                peak + self.cfg.trail_atr_mult * atr_now
            if (is_long and trail > desired) or (not is_long and trail < desired):
                desired, source = trail, "trailing"
        if (is_long and desired > position["stop"]) or (not is_long and desired < position["stop"]):
            position["stop"] = desired
            position["stop_source"] = source

    def _check_exit(self, position: dict, bar: Bar) -> tuple[Optional[float], str]:
        side, stop, tp = position["side"], position["stop"], position["tp"]
        if side == "long":
            hit_stop = bar.low <= stop
            hit_tp = tp is not None and bar.high >= tp
        else:
            hit_stop = bar.high >= stop
            hit_tp = tp is not None and bar.low <= tp
        # Conservative: if both levels are inside the bar, assume the stop filled.
        if hit_stop:
            source = position.get("stop_source") or "initial"
            reason = {"initial": "stop", "breakeven": "breakeven_stop"}.get(source, "trailing_stop")
            return self.costs.exit_price(stop, side), reason
        if hit_tp:
            return self.costs.exit_price(tp, side), "take_profit"
        return None, ""

    def _close(self, position: dict, exit_price: float, exit_time: int, reason: str) -> Trade:
        side, entry, size = position["side"], position["entry"], position["size"]
        if side == "long":
            gross = (exit_price - entry) * size
        else:
            gross = (entry - exit_price) * size
        entry_notional = entry * size
        exit_notional = exit_price * size
        fees = self.costs.commission(entry_notional) + self.costs.commission(exit_notional)
        hours = max(0.0, (exit_time - position["entry_time"]) / 3_600_000.0)
        fees += self.costs.swap(entry_notional, hours)
        return Trade(
            entry_time=position["entry_time"], exit_time=exit_time, side=side,
            entry=entry, exit=exit_price, size=size, notional=entry_notional,
            pnl=gross - fees, fees=fees, reason=reason,
        )

    @staticmethod
    def _unrealized(position: Optional[dict], price: float) -> float:
        if position is None:
            return 0.0
        side, entry, size = position["side"], position["entry"], position["size"]
        return (price - entry) * size if side == "long" else (entry - price) * size
