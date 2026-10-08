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


def _load_jsonl(path: Path) -> list["Bar"]:
    bars: list["Bar"] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            bars.append(_row_to_bar(json.loads(line)))
    return bars


def _load_csv(path: Path) -> list["Bar"]:
    with path.open(newline="") as fh:
        return [_row_to_bar(row) for row in csv.DictReader(fh)]


def load_bars(path: str | Path) -> list["Bar"]:
    """Load bars from a JSONL or CSV file, sorted by ``open_time``."""
    p = Path(path)
    if p.suffix.lower() == ".csv":
        bars = _load_csv(p)
    elif p.suffix.lower() in (".jsonl", ".ndjson", ".json"):
        bars = _load_jsonl(p)
    else:
        raise ValueError(f"unsupported bar file extension: {p.suffix!r}")
    bars.sort(key=lambda b: b.open_time)
    return bars


def load_many(paths: Iterable[str | Path]) -> list["Bar"]:
    """Load and merge several files (e.g. contiguous archiver shards), de-duped."""
    by_time: dict[int, Bar] = {}
    for path in paths:
        for bar in load_bars(path):
            by_time[bar.open_time] = bar
    return sorted(by_time.values(), key=lambda b: b.open_time)


def save_bars_jsonl(bars: Sequence["Bar"], path: str | Path) -> None:
    with Path(path).open("w") as fh:
        for b in bars:
            fh.write(json.dumps({
                "open_time": b.open_time, "open": b.open,
                "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
            }) + "\n")
