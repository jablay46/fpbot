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
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("mfpbot.stream")

# Request id reserved for app-level heartbeat pings; subscriptions use 1..n.
PING_REQUEST_ID = 9000
# Request id reserved for one-shot ``candles.history`` requests.
HISTORY_REQUEST_ID = 9001


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
        ping_interval: float = 20.0,
        ping_request_id: int = PING_REQUEST_ID,
        idle_timeout: float = 60.0,
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
        self.ping_interval = ping_interval
        self.ping_request_id = ping_request_id
        self.idle_timeout = idle_timeout

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

    @staticmethod
    def _ping_frame(request_id: int) -> str:
        return json.dumps({"op": "req", "id": request_id, "method": "ping"})

    async def _pump(self, ws, queue: "asyncio.Queue[Optional[str]]", last_seen: list) -> None:
        """Forward raw frames into the queue; signal EOF with None."""
        try:
            async for raw in ws:
                last_seen[0] = time.monotonic()
                await queue.put(raw)
        finally:
            await queue.put(None)

    async def _heartbeat(self, ws, request_id: int, last_seen: list) -> None:
        """Send the app-level ping the feed expects and watch for silence.

        Control frames are not used, so a half-open TCP connection would block
        the reader forever. If no frame arrives within ``idle_timeout`` we close
        the socket, which makes the pump end and the outer loop reconnect.
        """
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                await ws.send(self._ping_frame(request_id))
                if time.monotonic() - last_seen[0] > self.idle_timeout:
                    log.warning("market stream idle for %.0fs; forcing reconnect", self.idle_timeout)
                    await ws.close()
                    return
        except (ConnectionClosed, OSError, asyncio.TimeoutError):
            # The reader pump reports the disconnect; let it drive reconnection.
            return

    def _subscribe_frame(self, request_id: int, group: dict) -> str:
        payload: dict = {"symbols": group["symbols"], "intervals": [self.interval], "historyLimit": self.history_limit}
        if group.get("providers"):
            payload["providers"] = group["providers"]
        return json.dumps({"op": "sub", "id": request_id, "channel": "candles", "payload": payload})

    def _history_frame(
        self,
        request_id: int,
        provider: str,
        symbol: str,
        interval: str,
        limit: int,
        start_time: Optional[int],
        end_time: Optional[int],
        price_kind: Optional[str],
    ) -> str:
        payload: dict = {"provider": provider, "symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            payload["startTime"] = start_time
        if end_time is not None:
            payload["endTime"] = end_time
        if price_kind is not None:
            payload["priceKind"] = price_kind
        return json.dumps({"op": "req", "id": request_id, "method": "candles.history", "payload": payload})

    async def fetch_history(
        self,
        provider: str,
        symbol: str,
        *,
        interval: Optional[str] = None,
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        price_kind: Optional[str] = None,
    ) -> list[Candle]:
        """Fetch a historical candle window for one provider/symbol.

        Uses the ``candles.history`` request over a short-lived connection. The
        provider caps how much it returns ("bounded by provider availability and
        retention"), so page by moving ``end_time`` backwards through the window
        you want; an empty or short list is a normal outcome, not an error.
        """
        interval = interval or self.interval
        request_id = HISTORY_REQUEST_ID
        async with websockets.connect(
            self.url, open_timeout=20, ping_interval=None, max_size=4 * 1024 * 1024
        ) as ws:
            await ws.send(self._history_frame(request_id, provider, symbol, interval, limit, start_time, end_time, price_kind))
            candles: list[Candle] = []
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout)
                frame = json.loads(raw)
                if frame.get("id") is not None and frame.get("id") != request_id:
                    continue
                op = frame.get("op")
                if op in ("sub_ok", "events", "snapshot_end"):
                    continue
                if "result" in frame:
                    for ev in frame.get("result") or []:
                        if isinstance(ev, dict) and ev.get("type") == "candle":
                            candles.append(Candle.from_event(ev))
                        elif isinstance(ev, dict) and "openTime" in ev:
                            candles.append(Candle.from_event(ev))
                    return candles
                if op in ("error", "err", "req_err"):
                    error = frame.get("error")
                    raise RuntimeError(f"candles.history failed for {provider}|{symbol}: {error}")
                if op == "end":
                    return candles

    async def candles(self) -> AsyncIterator[Candle]:
        """Yield candle events forever, reconnecting on transport failures."""
        next_id = 1
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.url,
                    open_timeout=20,
                    ping_interval=None,
                    max_size=2 * 1024 * 1024,
                ) as ws:
                    for group in self.groups:
                        await ws.send(self._subscribe_frame(next_id, group))
                        next_id += 1
                    backoff = 1.0
                    log.info("market stream connected (%d subscription group(s), %s)",
                             len(self.groups), self.interval)
                    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
                    last_seen = [time.monotonic()]
                    pump = asyncio.create_task(self._pump(ws, queue, last_seen))
                    heartbeat = asyncio.create_task(
                        self._heartbeat(ws, self.ping_request_id, last_seen)
                    )
                    try:
                        while True:
                            raw = await queue.get()
                            if raw is None:
                                # Retrieve any transport error so the outer
                                # handler applies backoff instead of tight-looping.
                                await pump
                                break
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
                    finally:
                        pump.cancel()
                        heartbeat.cancel()
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
