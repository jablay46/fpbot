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


def day_key(now: Optional[datetime] = None, tz: str = "UTC") -> str:
    """Calendar-day key (``YYYY-MM-DD``) in the given IANA timezone.

    The firm resets the daily loss limit at midnight ``America/New_York``, so
    the live bot guards that boundary — a UTC day would be 4-5h off around each
    reset. Falls back to UTC when the zone is unavailable.
    """
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone).strftime("%Y-%m-%d")


def _utc_day(now: Optional[datetime] = None) -> str:
    return day_key(now, "UTC")


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: Optional[float] = None
    entries_today: int = 0
    halted: bool = False
    halt_reason: str = ""
    # Day the "account room missing" warning was last logged (once per day).
    missing_room_warned_day: str = ""
    # Highest equity ever observed, for the trailing drawdown basis.
    peak_equity: Optional[float] = None
    # Cumulative-drawdown halt. Unlike ``halted`` it survives day rollover and
    # restarts; only an explicit operator reset clears it.
    total_drawdown_halted: bool = False
    total_drawdown_reason: str = ""
    # Early-warning flags so the 50%/80% thresholds warn once, even across restarts.
    dd_warned_50: bool = False
    dd_warned_80: bool = False

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
        max_total_drawdown_pct: float = 0.0,
        drawdown_basis: str = "starting",
        day_tz: str = "UTC",
    ) -> None:
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss_pct = max_daily_loss_pct
        self.min_daily_room_pct = min_daily_room_pct
        self.starting_balance = starting_balance
        self.day_tz = day_tz
        # "halt": a null room is treated as a hard stop (fail closed).
        # "bot-only": continue with bot-side caps and warn once per day.
        self.missing_room_policy = missing_room_policy
        self.max_total_drawdown_pct = max_total_drawdown_pct
        self.drawdown_basis = drawdown_basis

    def check_total_drawdown(self, state: RiskState, equity: float) -> Decision:
        """Bot-side cumulative drawdown guard, independent of the API room.

        Tracks the running equity peak (for the trailing basis) and compares the
        current drawdown to ``max_total_drawdown_pct``. Returns
        ``flatten=True`` when the limit is breached so the caller halts and
        flattens exactly like the daily kill switch. A zero limit disables the
        guard but still updates the peak.
        """
        if state.peak_equity is None or equity > state.peak_equity:
            state.peak_equity = equity
        if self.max_total_drawdown_pct <= 0:
            return Decision(True)
        if state.total_drawdown_halted:
            return Decision(False, state.total_drawdown_reason or "total drawdown", flatten=True)

        if self.drawdown_basis == "peak":
            basis = state.peak_equity
        else:
            basis = self.starting_balance
        if not basis or basis <= 0:
            return Decision(True)
        drawdown_pct = max(0.0, (basis - equity) / basis * 100.0)

        if drawdown_pct >= 0.8 * self.max_total_drawdown_pct and not state.dd_warned_80:
            state.dd_warned_80 = True
            log.warning(
                "total drawdown %.2f%% has reached 80%% of the %.2f%% limit "
                "(basis=%s)",
                drawdown_pct, self.max_total_drawdown_pct, self.drawdown_basis,
            )
        elif drawdown_pct >= 0.5 * self.max_total_drawdown_pct and not state.dd_warned_50:
            state.dd_warned_50 = True
            log.warning(
                "total drawdown %.2f%% has reached 50%% of the %.2f%% limit "
                "(basis=%s)",
                drawdown_pct, self.max_total_drawdown_pct, self.drawdown_basis,
            )

        if drawdown_pct >= self.max_total_drawdown_pct:
            reason = (
                f"total drawdown {drawdown_pct:.2f}% >= limit "
                f"{self.max_total_drawdown_pct}% (basis={self.drawdown_basis})"
            )
            return Decision(False, reason, flatten=True)
        return Decision(True)

    def _room_available(self, state: RiskState, account_risk: dict) -> bool:
        """False when the account reports no usable room and the policy is halt.

        A competition account can report ``daily_loss_room`` / ``max_drawdown_room``
        as null. Fail closed only when *both* are absent — a partial snapshot still
        carries a usable guard, and :meth:`check_kill` enforces whichever figure is
        present. The operator can opt into bot-only limits to always continue.
        """
        daily_room = account_risk.get("daily_loss_room")
        dd_room = account_risk.get("max_drawdown_room")
        if self.missing_room_policy != "halt":
            # Only warn when the snapshot is actually missing its room figures;
            # a healthy snapshot must not cry wolf once a day.
            if (
                self.missing_room_policy == "bot-only"
                and daily_room is None
                and dd_room is None
                and state.missing_room_warned_day != state.day
            ):
                log.warning(
                    "account risk snapshot has no room figures; relying on bot-side caps only"
                )
                state.missing_room_warned_day = state.day
            return True
        if daily_room is None and dd_room is None:
            log.error(
                "account risk snapshot is missing all room figures (daily_loss_room=None, "
                "max_drawdown_room=None); refusing new entries (FP_ON_MISSING_ROOM=halt)"
            )
            return False
        return True

    def roll_day(self, state: RiskState, equity: float, now: Optional[datetime] = None) -> RiskState:
        today = day_key(now, self.day_tz)
        if state.day != today:
            state.day = today
            state.day_start_equity = equity
            state.entries_today = 0
            state.halted = False
            state.halt_reason = ""
            log.warning(
                "new day %s (%s): daily loss baseline set to current equity %s "
                "(first observation of the day, not exactly midnight), so the daily "
                "figure is approximate",
                today, self.day_tz, equity,
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
        if state.total_drawdown_halted:
            return Decision(False, state.total_drawdown_reason or "total drawdown halted")
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
