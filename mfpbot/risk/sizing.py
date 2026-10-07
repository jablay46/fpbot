"""Risk-based position sizing for perpetual futures orders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..util import round_step


@dataclass
class SizingResult:
    ok: bool
    size: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    stop_distance: float = 0.0
    required_margin: float = 0.0
    leverage: float = 0.0
    reason: str = ""


class PositionSizer:
    """Sizes a trade so the loss between entry and stop equals the risk budget."""

    def __init__(
        self,
        *,
        risk_per_trade_pct: float,
        leverage: float,
        min_notional: float = 0.0,
        min_size: float = 0.0,
        size_step: float = 0.0,
        max_leverage: Optional[float] = None,
        available_balance: Optional[float] = None,
    ) -> None:
        self.risk_per_trade_pct = risk_per_trade_pct
        self.leverage = leverage
        self.min_notional = min_notional
        self.min_size = min_size
        self.size_step = size_step
        self.max_leverage = max_leverage
        self.available_balance = available_balance

    def size(self, *, equity: float, entry: float, stop: float) -> SizingResult:
        if equity <= 0:
            return SizingResult(ok=False, reason="account equity is not positive")
        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            return SizingResult(ok=False, reason="stop distance is zero")

        leverage = self.leverage
        if self.max_leverage is not None:
            leverage = min(leverage, self.max_leverage)
        if leverage <= 0:
            return SizingResult(ok=False, reason="leverage is not positive")

        risk_amount = equity * (self.risk_per_trade_pct / 100.0)
        raw_size = risk_amount / stop_distance
        size = round_step(raw_size, self.size_step, mode="down") if self.size_step else raw_size

        if size <= 0 or (self.min_size and size < self.min_size):
            return SizingResult(
                ok=False,
                reason=(
                    f"computed size {size:g} is below the market minimum "
                    f"{self.min_size:g} (risk budget {risk_amount:.2f})"
                ),
                risk_amount=risk_amount,
                stop_distance=stop_distance,
                leverage=leverage,
            )

        notional = size * entry
        if self.min_notional and notional < self.min_notional:
            return SizingResult(
                ok=False,
                reason=(
                    f"notional {notional:.2f} is below the market minimum "
                    f"{self.min_notional:.2f} (risk budget {risk_amount:.2f})"
                ),
                size=size,
                risk_amount=risk_amount,
                stop_distance=stop_distance,
                leverage=leverage,
            )

        required_margin = notional / leverage
        if self.available_balance is not None and required_margin > self.available_balance:
            return SizingResult(
                ok=False,
                reason=(
                    f"required margin {required_margin:.2f} exceeds available "
                    f"balance {self.available_balance:.2f}"
                ),
                size=size,
                notional=notional,
                risk_amount=risk_amount,
                stop_distance=stop_distance,
                leverage=leverage,
            )

        return SizingResult(
            ok=True,
            size=size,
            notional=notional,
            risk_amount=risk_amount,
            stop_distance=stop_distance,
            required_margin=required_margin,
            leverage=leverage,
        )
