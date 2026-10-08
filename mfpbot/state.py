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
class PendingEntry:
    """An entry order that was sent but whose outcome is not yet confirmed.

    Persisted before the request so a lost reply (or a crash mid-send) can be
    reconciled by client_order_id instead of being assumed failed.
    """

    market_id: str
    client_order_id: str
    idempotency_key: str
    sent_at: float
    pre_position_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingEntry":
        return cls(
            market_id=str(data["market_id"]),
            client_order_id=str(data["client_order_id"]),
            idempotency_key=str(data.get("idempotency_key") or ""),
            sent_at=float(data.get("sent_at") or 0.0),
            pre_position_ids=[str(p) for p in data.get("pre_position_ids") or []],
        )


@dataclass
class BotState:
    risk: RiskState = field(default_factory=RiskState)
    last_processed_open_time: dict[str, int] = field(default_factory=dict)
    last_entry_client_order_id: Optional[str] = None
    # Positions opened by this bot, so it never touches manual positions.
    owned_position_ids: list[str] = field(default_factory=list)
    # Entry orders awaiting confirmation, keyed by client_order_id. A collection
    # (not a single slot) so an unresolved entry in one market cannot be clobbered
    # by a later entry in another market.
    pending_entries: dict[str, PendingEntry] = field(default_factory=dict)

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
        pending: dict[str, PendingEntry] = {}
        raw_entries = data.get("pending_entries")
        if isinstance(raw_entries, dict):
            for key, value in raw_entries.items():
                if isinstance(value, dict):
                    entry = PendingEntry.from_dict(value)
                    pending[str(key)] = entry
        # Migrate the legacy single-slot format.
        raw_pending = data.get("pending_entry")
        if isinstance(raw_pending, dict):
            entry = PendingEntry.from_dict(raw_pending)
            pending.setdefault(entry.client_order_id, entry)
        return cls(
            risk=risk,
            last_processed_open_time=last,
            last_entry_client_order_id=data.get("last_entry_client_order_id"),
            owned_position_ids=[str(p) for p in owned],
            pending_entries=pending,
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
