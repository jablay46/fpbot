"""Trading-cost model for the backtester.

Rates come from the MyFundedPerps published schedules (see
``docs.myfundedperpetuals.com/guides/commissions-and-fees`` and
``/guides/trading-guide``):

* commission per fill, crypto: **0.03%** of notional (maker and taker alike);
* hourly swap on the open notional, crypto: **0.03% per day** / 24;
* adverse slippage, crypto: basis points of the reference price, banded by the
  instrument's open interest. The default sits in the "$100M–$500M, up to
  $100k" bucket (1.2 bps/side); pass a different value for thin markets.

A backtest that ignores these will overstate a marginal edge, so they are on by
default and every metric is reported net of cost.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    commission_pct: float = 0.03        # crypto, per fill
    swap_daily_pct: float = 0.03        # crypto, per day held
    slippage_bps: float = 1.2           # crypto, per side, adverse
    apply_costs: bool = True

    # -- fills -----------------------------------------------------------

    def entry_price(self, reference: float, side: str, notional: float | None = None) -> float:
        """Reference price moved *against* us on entry."""
        if not self.apply_costs:
            return reference
        slip = reference * self.slippage_bps / 10_000.0
        return reference + slip if side == "long" else reference - slip

    def exit_price(self, reference: float, side: str, notional: float | None = None) -> float:
        """Reference price moved *against* us on exit (opposite of entry)."""
        if not self.apply_costs:
            return reference
        slip = reference * self.slippage_bps / 10_000.0
        return reference - slip if side == "long" else reference + slip

    # -- charges ---------------------------------------------------------

    def commission(self, notional: float) -> float:
        if not self.apply_costs:
            return 0.0
        return abs(notional) * self.commission_pct / 100.0

    def swap(self, notional: float, hours: float) -> float:
        """Hourly swap accrued over ``hours`` of holding the given notional."""
        if not self.apply_costs or hours <= 0:
            return 0.0
        return abs(notional) * self.swap_daily_pct / 100.0 / 24.0 * hours
