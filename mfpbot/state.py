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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotState":
        data = data or {}
        risk = RiskState.from_dict(data.get("risk", {}))
        raw_last = data.get("last_processed_open_time")
        if isinstance(raw_last, dict):
            last = {str(k): int(v) for k, v in raw_last.items()}
        elif raw_last is None:
            last = {}
        else:
            # Migration from the single-market format.
            last = {}
        return cls(
            risk=risk,
            last_processed_open_time=last,
            last_entry_client_order_id=data.get("last_entry_client_order_id"),
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
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, p)
