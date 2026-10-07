"""Daily loss and trade-count guards, plus account-level kill switches.

Two layers protect the account:

* Bot guards (this class): a maximum number of entries per UTC day and a maximum
  daily loss measured against the day's starting equity.
* Account guards (from the API risk snapshot): remaining daily-loss and max
  drawdown room. The bot stops opening and optionally flattens when the room
  falls below a configurable fraction of the starting balance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _utc_day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: Optional[float] = None
    entries_today: int = 0
    halted: bool = False
    halt_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RiskState":
        fields = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    flatten: bool = False


class RiskManager:
    def __init__(
        self,
        *,
        max_daily_trades: int,
        max_daily_loss_pct: float,
        min_daily_room_pct: float,
        starting_balance: float,
    ) -> None:
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss_pct = max_daily_loss_pct
        self.min_daily_room_pct = min_daily_room_pct
        self.starting_balance = starting_balance

    def roll_day(self, state: RiskState, equity: float, now: Optional[datetime] = None) -> RiskState:
        today = _utc_day(now)
        if state.day != today:
            state.day = today
            state.day_start_equity = equity
            state.entries_today = 0
            state.halted = False
            state.halt_reason = ""
        elif state.day_start_equity is None:
            state.day_start_equity = equity
        return state

    def check_kill(self, state: RiskState, equity: float, account_risk: dict) -> Decision:
        """Account-protecting kill switch, independent of any position.

        Runs before the per-market position branch so a bot-owned position can
        never shield the account from the daily loss cap or a room floor. Returns
        ``flatten=True`` when positions should be closed and the bot halted.
        """
        start = state.day_start_equity
        if start and start > 0:
            loss_pct = max(0.0, (start - equity) / start * 100.0)
            if loss_pct >= self.max_daily_loss_pct:
                reason = f"bot daily loss cap reached ({loss_pct:.2f}% >= {self.max_daily_loss_pct}%)"
                return Decision(False, reason, flatten=True)

        room_floor = self.starting_balance * (self.min_daily_room_pct / 100.0)
        daily_room = account_risk.get("daily_loss_room")
        dd_room = account_risk.get("max_drawdown_room")
        if daily_room is not None and daily_room <= room_floor:
            return Decision(
                False,
                f"account daily loss room {daily_room:.2f} below floor {room_floor:.2f}",
                flatten=True,
            )
        if dd_room is not None and dd_room <= room_floor:
            return Decision(
                False,
                f"account max drawdown room {dd_room:.2f} below floor {room_floor:.2f}",
                flatten=True,
            )
        return Decision(True)

    def can_enter(self, state: RiskState, equity: float, account_risk: dict) -> Decision:
        """Whether a new entry is allowed right now.

        Only the halted flag and the daily trade-count limit live here; the
        loss/room checks belong to :meth:`check_kill` so they also run when a
        position is already open.
        """
        if state.halted:
            return Decision(False, state.halt_reason or "halted for the day")
        if state.entries_today >= self.max_daily_trades:
            return Decision(False, f"daily trade limit reached ({self.max_daily_trades})")
        return Decision(True)

    def can_open(self, state: RiskState, equity: float, account_risk: dict) -> Decision:
        """Combined check: kill switch first, then entry permission."""
        kill = self.check_kill(state, equity, account_risk)
        if not kill.allowed:
            return kill
        return self.can_enter(state, equity, account_risk)

    def record_entry(self, state: RiskState) -> None:
        state.entries_today += 1

    def halt(self, state: RiskState, reason: str) -> None:
        state.halted = True
        state.halt_reason = reason
