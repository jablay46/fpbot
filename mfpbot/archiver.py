"""Archive closed candles from the market stream to disk.

The REST API has no historical-candle endpoint, so a backtest against MFP's own
prices needs a local record. This appends each *final* candle as one JSONL line
(normalized to the same keys :func:`mfpbot.backtest.data.load_bars` reads) and
skips bars already written, so it is safe to start, stop and resume.

Run it alongside the bot (or on its own) to build the dataset, then point the
backtester at the file:

    python -m mfpbot archive --symbols binance|BTCUSDT --timeframe 15m --out btc.jsonl
    python -m mfpbot backtest --bars btc.jsonl --strategy donchian_breakout
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from .market_stream import Candle, MarketDataStream

log = logging.getLogger("mfpbot.archive")


class CandleArchiver:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._seen: set[tuple[str, str, str, int]] = set()
        self._fh = None

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            for key in _iter_bar_keys(self.path):
                self._seen.add(key)
        self._fh = self.path.open("a")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def write(self, candle: Candle) -> bool:
        """Append a final candle once. Returns True when a new row was written."""
        if not candle.is_final:
            return False
        if self._fh is None:
            self._open()  # loads the existing bar keys into ``_seen`` first
        key = (candle.provider, candle.symbol, candle.interval, candle.open_time)
        if key in self._seen:
            return False
        self._fh.write(json.dumps({
            "open_time": candle.open_time,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "provider": candle.provider,
            "symbol": candle.symbol,
            "interval": candle.interval,
        }) + "\n")
        self._fh.flush()
        self._seen.add(key)
        return True


def _iter_bar_keys(path: Path):
    """Yield ``(provider, symbol, interval, open_time)`` for each valid row."""
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "open_time" in row:
                yield (
                    str(row.get("provider") or ""),
                    str(row.get("symbol") or ""),
                    str(row.get("interval") or ""),
                    int(row["open_time"]),
                )


async def run_archive(
    stream: MarketDataStream,
    path: str | Path,
    *,
    max_candles: Optional[int] = None,
) -> int:
    """Consume the stream and archive bars until stopped or ``max_candles`` hit."""
    archiver = CandleArchiver(path)
    written = 0
    try:
        async for candle in stream.candles():
            if archiver.write(candle):
                written += 1
                if written % 50 == 0:
                    log.info("archived %d bar(s) to %s", written, path)
            if max_candles is not None and written >= max_candles:
                break
    finally:
        archiver.close()
    return written
