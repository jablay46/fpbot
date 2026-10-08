"""Trading-cost model for the backtester.

Rates come from the MyFundedPerps published schedules (see
``docs.myfundedperpetuals.com/guides/commissions-and-fees`` and
``/guides/trading-guide``):

* commission per fill: **0.03%** crypto, **0.005%** stocks/commodities/
  indices ("tradfi"), **0.0025%** forex (maker and taker alike);
* hourly swap on the open notional: **0.03%/day** crypto, **0.015%/day**
  tradfi, **0.005%/day** forex, each divided into 24 hourly charges;
* adverse slippage: basis points of the reference price. Crypto is banded by
  the instrument's open interest — the default sits in the "$100M–$500M, up
  to $100k" bucket (1.2 bps/side); pass a different value for thin markets.
  Major indices/commodities cap at 0.25 bps and forex at 0.05 bps (up to
  $100k notional).

A backtest that ignores these will overstate a marginal edge, so they are on by
default and every metric is reported net of cost.
"""

from __future__ import annotations

from dataclasses import dataclass

# (commission_pct, swap_daily_pct, slippage_bps) per asset class, from the
# published fee and slippage schedules. Slippage figures are the worst-case
# caps for the smallest notional bucket — the conservative reading.
ASSET_CLASS_COSTS: dict[str, tuple[float, float, float]] = {
    "crypto": (0.03, 0.03, 1.2),
    "tradfi": (0.005, 0.015, 0.25),   # stocks, commodities, indices
    "forex": (0.0025, 0.005, 0.05),
}


@dataclass
class CostModel:
    commission_pct: float = 0.03        # crypto default, per fill
    swap_daily_pct: float = 0.03        # crypto default, per day held
    slippage_bps: float = 1.2           # crypto default, per side, adverse
    apply_costs: bool = True

    @classmethod
    def for_asset_class(cls, name: str, **overrides) -> "CostModel":
        """Build a cost model from a published asset-class preset.

        ``name`` is one of ``crypto``, ``tradfi`` (stocks/commodities/indices)
        or ``forex``. Explicit keyword overrides (e.g. a thinner market's
        slippage band) win over the preset.
        """
        try:
            commission, swap, slippage = ASSET_CLASS_COSTS[name]
        except KeyError:
            raise ValueError(
                f"unknown asset class {name!r}; choose one of {sorted(ASSET_CLASS_COSTS)}"
            ) from None
        params = {"commission_pct": commission, "swap_daily_pct": swap, "slippage_bps": slippage}
        params.update(overrides)
        return cls(**params)

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
