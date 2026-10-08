"""Daily loss and trade-count guards, plus account-level kill switches.

Two layers protect the account:

* Bot guards (this class): a maximum number of entries per UTC day and a maximum
  daily loss measured against the day's starting equity.
* Account guards (from the API risk snapshot): remaining daily-loss and max
  drawdown room. The bot stops opening and optionally flattens when the room
  falls below a configurable fraction of the starting balance.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("mfpbot.risk")


def _utc_day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: Optional[float] = None
    entries_today: int = 0
    halted: bool = False
    halt_reason: str = ""
    # Day the "account room missing" warning was last logged (once per day).
    missing_room_warned_day: str = ""

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
        missing_room_policy: str = "bot-only",
    ) -> None:
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss_pct = max_daily_loss_pct
        self.min_daily_room_pct = min_daily_room_pct
        self.starting_balance = starting_balance
        # "halt": a null room is treated as a hard stop (fail closed).
        # "bot-only": continue with bot-side caps and warn once per day.
        self.missing_room_policy = missing_room_policy

    def _room_available(self, state: RiskState, account_risk: dict) -> bool:
        """False when the account reports no usable room and the policy is halt.

        A competition account can report ``daily_loss_room`` / ``max_drawdown_room``
        as null. Fail closed only when *both* are absent — a partial snapshot still
        carries a usable guard, and :meth:`check_kill` enforces whichever figure is
        present. The operator can opt into bot-only limits to always continue.
        """
        if self.missing_room_policy != "halt":
            if self.missing_room_policy == "bot-only" and state.missing_room_warned_day != state.day:
                log.warning(
                    "account risk snapshot has no room figures; relying on bot-side caps only"
                )
                state.missing_room_warned_day = state.day
            return True
        daily_room = account_risk.get("daily_loss_room")
        dd_room = account_risk.get("max_drawdown_room")
        if daily_room is None and dd_room is None:
            log.error(
                "account risk snapshot is missing all room figures (daily_loss_room=None, "
                "max_drawdown_room=None); refusing new entries (FP_ON_MISSING_ROOM=halt)"
            )
            return False
        return True

    def roll_day(self, state: RiskState, equity: float, now: Optional[datetime] = None) -> RiskState:
        today = _utc_day(now)
        if state.day != today:
            state.day = today
            state.day_start_equity = equity
            state.entries_today = 0
            state.halted = False
            state.halt_reason = ""
            log.warning(
                "new UTC day %s: daily loss baseline set to current equity %s "
                "(first candle of the day, not 00:00 UTC), so the daily figure is "
                "approximate",
                today, equity,
            )
        elif state.day_start_equity is None:
            state.day_start_equity = equity
            log.warning(
                "no stored equity baseline for %s; using current equity %s, so the "
                "daily loss figure may be inaccurate",
                today, equity,
            )
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
        if not self._room_available(state, account_risk):
            return Decision(False, "account risk snapshot has no room figures (fail closed)")
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
