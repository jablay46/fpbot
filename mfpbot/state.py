"""Persistent bot state so restarts keep daily counters and candle progress."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .risk.manager import RiskState

log = logging.getLogger("mfpbot.state")

# Serializes every write to a state file. ``save_state`` is called from the
# candle worker thread and the watchdog, so serialization and the swap must not
# interleave. The lock is never held across network I/O.
_SAVE_LOCK = threading.Lock()


class StateLoadError(RuntimeError):
    """Raised when an existing state file cannot be read and a fresh start is
    not allowed (live, non-dry-run)."""


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


def _read_state_file(p: Path) -> BotState:
    return BotState.from_dict(json.loads(p.read_text(encoding="utf-8")))


def load_state(
    path: str | os.PathLike[str],
    *,
    allow_fresh: bool = True,
    environment: str = "sandbox",
    dry_run: bool = False,
) -> BotState:
    """Load state, recovering from ``.bak`` and refusing a silent fresh start.

    A missing file is a genuine new state. An *existing* file that cannot be
    parsed falls back to the ``.bak`` copy. When both are unreadable, a live,
    non-dry-run bot must not silently lose ownership/pending/daily counters: it
    raises :class:`StateLoadError` unless ``allow_fresh`` (``FP_ALLOW_FRESH_STATE``)
    is set. Sandbox and dry runs warn and start fresh.
    """
    p = Path(path)
    if not p.exists():
        return BotState()
    try:
        return _read_state_file(p)
    except (ValueError, OSError) as exc:
        log.warning("state file %s is unreadable (%s); trying %s.bak", p, exc, p)
    bak = p.with_name(p.name + ".bak")
    if bak.exists():
        try:
            state = _read_state_file(bak)
            log.warning("recovered state from %s", bak)
            return state
        except (ValueError, OSError) as exc:
            log.warning("backup state file %s is also unreadable (%s)", bak, exc)
    if environment == "live" and not dry_run and not allow_fresh:
        raise StateLoadError(
            f"state file {p} and its backup are unreadable, and a fresh start is "
            "not allowed in live mode (it would drop owned positions, pending "
            "entries and daily counters). Restore the file or set "
            "FP_ALLOW_FRESH_STATE=true to start empty deliberately."
        )
    log.warning("starting from empty state: %s and its backup are unreadable", p)
    return BotState()


def _write_atomic(p: Path, payload: str) -> None:
    """Write ``payload`` next to ``p`` and atomically swap it into place."""
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent or "."))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_state(path: str | os.PathLike[str], state: BotState) -> None:
    """Persist state atomically under a lock; never let a failure stop the bot."""
    p = Path(path)
    payload = json.dumps(state.to_dict(), indent=2)
    with _SAVE_LOCK:
        try:
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            bak = p.with_name(p.name + ".bak")
            if p.exists():
                try:
                    shutil.copy2(p, bak)
                except OSError as exc:
                    log.debug("could not refresh %s: %s", bak, exc)
            _write_atomic(p, payload)
            return
        except OSError as exc:
            log.warning("atomic state save failed (%s); trying direct write", exc)
        # A non-atomic fallback, only after the atomic path failed (and only for
        # non-race errors, since the lock already excludes concurrent writers).
        try:
            p.write_text(payload, encoding="utf-8")
        except OSError as exc:
            log.error("could not persist state to %s: %s", p, exc)
