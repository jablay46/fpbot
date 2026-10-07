"""Persistent bot state so restarts keep daily counters and candle progress."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .risk.manager import RiskState

log = logging.getLogger("mfpbot.state")


@dataclass
class BotState:
    risk: RiskState = field(default_factory=RiskState)
    last_processed_open_time: dict[str, int] = field(default_factory=dict)
    last_entry_client_order_id: Optional[str] = None
    # Positions opened by this bot, so it never touches manual positions.
    owned_position_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotState":
        data = data or {}
        risk = RiskState.from_dict(data.get("risk", {}))
        raw_last = data.get("last_processed_open_time")
        if isinstance(raw_last, dict):
            last = {str(k): int(v) for k, v in raw_last.items()}
        else:
            # None or a legacy scalar cursor from the single-market format.
            last = {}
        owned = data.get("owned_position_ids") or []
        return cls(
            risk=risk,
            last_processed_open_time=last,
            last_entry_client_order_id=data.get("last_entry_client_order_id"),
            owned_position_ids=[str(p) for p in owned],
        )


def load_state(path: str | os.PathLike[str]) -> BotState:
    p = Path(path)
    if not p.exists():
        return BotState()
    try:
        return BotState.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, OSError) as exc:
        log.warning("could not read state file %s (%s); starting fresh", p, exc)
        return BotState()


def save_state(path: str | os.PathLike[str], state: BotState) -> None:
    """Persist state atomically, but never let a write failure stop the bot."""
    p = Path(path)
    payload = json.dumps(state.to_dict(), indent=2)
    tmp = p.with_name(p.name + ".tmp")
    try:
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, p)
        return
    except OSError as exc:
        log.warning("atomic state save failed (%s); trying direct write", exc)
    try:
        tmp.unlink(missing_ok=True)
        p.write_text(payload, encoding="utf-8")
    except OSError as exc:
        log.error("could not persist state to %s: %s", p, exc)
