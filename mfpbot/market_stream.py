"""Public market-data WebSocket client for MyFundedPerps.

Streams candles and ticks from ``wss://api-stream.myfundedperpetuals.com``.
The feed is public and unauthenticated; never send a trading API key to it.

The client yields candle events through an async generator and reconnects with
exponential backoff. On reconnect it re-subscribes with a retained history
limit so gaps are backfilled from the server snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("mfpbot.stream")


@dataclass
class Candle:
    provider: str
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_final: bool

    @classmethod
    def from_event(cls, ev: dict) -> "Candle":
        return cls(
            provider=ev["provider"],
            symbol=ev["symbol"],
            interval=ev["interval"],
            open_time=int(ev["openTime"]),
            close_time=int(ev["closeTime"]),
            open=float(ev["open"]),
            high=float(ev["high"]),
            low=float(ev["low"]),
            close=float(ev["close"]),
            volume=float(ev.get("volume") or 0.0),
            is_final=bool(ev.get("isFinal")),
        )


@dataclass
class Tick:
    provider: str
    symbol: str
    kind: str
    price: float
    time: int

    @classmethod
    def from_event(cls, ev: dict) -> "Tick":
        return cls(
            provider=ev["provider"],
            symbol=ev["symbol"],
            kind=ev["kind"],
            price=float(ev["price"]),
            time=int(ev.get("time") or 0),
        )


class CandleSeries:
    """Keeps the most recent final candles for one market plus the forming bar."""

    def __init__(self, max_len: int = 500) -> None:
        self.max_len = max_len
        self._final: list[Candle] = []
        self._forming: Optional[Candle] = None

    def add(self, candle: Candle) -> None:
        if candle.is_final:
            if self._final and self._final[-1].open_time == candle.open_time:
                self._final[-1] = candle
            elif not self._final or candle.open_time > self._final[-1].open_time:
                self._final.append(candle)
            else:
                # Late/duplicate snapshot candle for an older bar.
                for i in range(len(self._final) - 1, -1, -1):
                    if self._final[i].open_time == candle.open_time:
                        self._final[i] = candle
                        break
            if self._final and self._forming and self._forming.open_time <= candle.open_time:
                self._forming = None
            if len(self._final) > self.max_len:
                del self._final[: len(self._final) - self.max_len]
        else:
            self._forming = candle

    @property
    def closed(self) -> list[Candle]:
        return list(self._final)

    @property
    def forming(self) -> Optional[Candle]:
        return self._forming

    @property
    def last_price(self) -> Optional[float]:
        if self._forming is not None:
            return self._forming.close
        if self._final:
            return self._final[-1].close
        return None

    def __len__(self) -> int:
        return len(self._final)


class MarketDataStream:
    """Reconnecting async generator over the public market-data WebSocket.

    Subscriptions are grouped by provider: each group is one ``sub`` frame.
    Grouping matters because a subscription that lists several providers
    resolves its symbols against the first provider, so mixing a venue's
    symbols with another venue's provider is rejected as an unknown market.
    One WebSocket connection can carry several groups.
    """

    def __init__(
        self,
        url: str,
        *,
        groups: list[dict],
        interval: str = "15m",
        history_limit: int = 300,
        max_backoff: float = 60.0,
    ) -> None:
        if not groups:
            raise ValueError("at least one subscription group is required")
        for group in groups:
            symbols = group.get("symbols") or []
            if not 1 <= len(symbols) <= 32:
                raise ValueError("each group must contain 1 to 32 symbols")
        self.url = url
        self.groups = groups
        self.interval = interval
        self.history_limit = history_limit
        self.max_backoff = max_backoff

    @classmethod
    def for_markets(
        cls,
        url: str,
        markets: list[dict],
        *,
        interval: str = "15m",
        history_limit: int = 300,
        max_backoff: float = 60.0,
    ) -> "MarketDataStream":
        """Build one subscription group per provider from market catalog rows."""
        by_provider: dict[str, list[str]] = {}
        for market in markets:
            provider = market["provider"]
            coin = market["coin"]
            bucket = by_provider.setdefault(provider, [])
            if coin not in bucket:
                bucket.append(coin)
        groups = [{"symbols": coins, "providers": [provider]} for provider, coins in by_provider.items()]
        return cls(url, groups=groups, interval=interval, history_limit=history_limit, max_backoff=max_backoff)

    def _subscribe_frame(self, request_id: int, group: dict) -> str:
        payload: dict = {"symbols": group["symbols"], "intervals": [self.interval], "historyLimit": self.history_limit}
        if group.get("providers"):
            payload["providers"] = group["providers"]
        return json.dumps({"op": "sub", "id": request_id, "channel": "candles", "payload": payload})

    async def candles(self) -> AsyncIterator[Candle]:
        """Yield candle events forever, reconnecting on transport failures."""
        next_id = 1
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.url,
                    open_timeout=20,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2 * 1024 * 1024,
                ) as ws:
                    for group in self.groups:
                        await ws.send(self._subscribe_frame(next_id, group))
                        next_id += 1
                    backoff = 1.0
                    log.info("market stream connected (%d subscription group(s), %s)",
                             len(self.groups), self.interval)
                    async for raw in ws:
                        frame = json.loads(raw)
                        op = frame.get("op")
                        if op == "events":
                            for ev in frame.get("events", []):
                                if ev.get("type") == "candle":
                                    yield Candle.from_event(ev)
                        elif op == "sub_err":
                            log.error("subscription rejected: %s", frame.get("error"))
                            raise RuntimeError(f"subscription rejected: {frame.get('error')}")
                        elif op == "draining":
                            log.warning("stream draining; reconnecting")
                            break
                        elif op == "end":
                            log.warning("subscription ended by server; reconnecting")
                            break
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                raise
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                log.warning("market stream disconnected (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
            except Exception as exc:  # noqa: BLE001 - keep the bot alive
                log.warning("market stream error (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
