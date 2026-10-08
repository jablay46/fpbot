"""Historical bar loading for the backtester.

Accepts the two shapes you will actually have on disk:

* a raw MyFundedPerps candle event (``openTime``/``open``/``high``/``low``/
  ``close``/``volume``); the archiver writes these verbatim;
* a normalized row (``open_time`` and the same OHLCV keys).

JSONL and CSV are both supported; the format is chosen by file extension.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Bar:
    """One closed OHLCV bar. Duck-compatible with ``Candle`` for strategies."""

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_final(self) -> bool:
        return True

    @property
    def close_time(self) -> int:
        return self.open_time


def _row_to_bar(row: dict) -> "Bar":
    def pick(*keys: str):
        for key in keys:
            if key in row and row[key] not in (None, ""):
                return row[key]
        raise KeyError(f"missing any of {keys} in bar row: {sorted(row)}")

    return Bar(
        open_time=int(pick("open_time", "openTime", "time", "timestamp")),
        open=float(pick("open", "o")),
        high=float(pick("high", "h")),
        low=float(pick("low", "l")),
        close=float(pick("close", "c")),
        volume=float(pick("volume", "v")),
    )


def _row_group(row: dict) -> tuple[str, str, str]:
    """``(provider, symbol, interval)`` identity of a row ("" when absent)."""
    return (
        str(row.get("provider") or ""),
        str(row.get("symbol") or row.get("coin") or ""),
        str(row.get("interval") or ""),
    )


def _check_single_group(rows: list[dict], path: str | Path) -> None:
    """Refuse a dataset that mixes symbols, providers or intervals.

    Merging two assets' bars into one series silently corrupts every indicator
    and trade the backtester computes; archive and backtest one market file at
    a time instead.
    """
    groups = {_row_group(row) for row in rows}
    if len(groups) > 1:
        pretty = ", ".join("|".join(g) or "?" for g in sorted(groups))
        raise ValueError(f"{path}: bars mix several (provider|symbol|interval) groups: {pretty}")


def _load_jsonl(path: Path) -> tuple[list["Bar"], set[tuple[str, str, str]]]:
    rows: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    _check_single_group(rows, path)
    return [_row_to_bar(row) for row in rows], {_row_group(row) for row in rows}


def _load_csv(path: Path) -> tuple[list["Bar"], set[tuple[str, str, str]]]:
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    _check_single_group(rows, path)
    return [_row_to_bar(row) for row in rows], {_row_group(row) for row in rows}


def load_bars(path: str | Path) -> list["Bar"]:
    """Load bars from a JSONL or CSV file, sorted by ``open_time``."""
    p = Path(path)
    if p.suffix.lower() == ".csv":
        bars, _ = _load_csv(p)
    elif p.suffix.lower() in (".jsonl", ".ndjson", ".json"):
        bars, _ = _load_jsonl(p)
    else:
        raise ValueError(f"unsupported bar file extension: {p.suffix!r}")
    bars.sort(key=lambda b: b.open_time)
    return bars


def load_many(paths: Iterable[str | Path]) -> list["Bar"]:
    """Load and merge several files (e.g. contiguous archiver shards), de-duped.

    Every file must cover the same (provider, symbol, interval); merging
    different markets is refused rather than silently mixed.
    """
    by_time: dict[int, Bar] = {}
    groups: set[tuple[str, str, str]] = set()
    for path in paths:
        p = Path(path)
        if p.suffix.lower() == ".csv":
            bars, file_groups = _load_csv(p)
        elif p.suffix.lower() in (".jsonl", ".ndjson", ".json"):
            bars, file_groups = _load_jsonl(p)
        else:
            raise ValueError(f"unsupported bar file extension: {p.suffix!r}")
        groups |= file_groups
        if len(groups) > 1:
            pretty = ", ".join("|".join(g) or "?" for g in sorted(groups))
            raise ValueError(f"bar files mix several (provider|symbol|interval) groups: {pretty}")
        for bar in bars:
            by_time[bar.open_time] = bar
    return sorted(by_time.values(), key=lambda b: b.open_time)


def save_bars_jsonl(bars: Sequence["Bar"], path: str | Path) -> None:
    with Path(path).open("w") as fh:
        for b in bars:
            fh.write(json.dumps({
                "open_time": b.open_time, "open": b.open,
                "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
            }) + "\n")
