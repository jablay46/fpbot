"""Shared test fixtures: a local HTTP stub of the MyFundedPerps API.

The stub is a real HTTP server, so the client's transport, header handling,
retry logic, and JSON unwrapping are exercised over the network stack rather
than through mocks.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from mfpbot.market_stream import Candle

MARKET = {
    "market_id": "binance|BTCUSDT",
    "provider": "binance",
    "symbol": "BTC",
    "coin": "BTCUSDT",
    "size_decimals": 3,
    "max_leverage": 10,
    "tick_size": 1e-08,
    "size_step": 0.001,
    "min_size": 0.001,
    "min_notional": 10,
    "contract_size": 1,
}

MARKET_ETH = {
    "market_id": "binance|ETHUSDT",
    "provider": "binance",
    "symbol": "ETH",
    "coin": "ETHUSDT",
    "size_decimals": 3,
    "max_leverage": 10,
    "tick_size": 1e-08,
    "size_step": 0.001,
    "min_size": 0.001,
    "min_notional": 10,
    "contract_size": 1,
}

MARKETS = [MARKET, MARKET_ETH]

# Quotes keyed by market ID (mid price).
QUOTES = {"binance|BTCUSDT": 100.1, "binance|ETHUSDT": 50.0}

ACCOUNT = {
    "id": "acct-1",
    "account_number": "MFP-1",
    "name": "Sandbox",
    "stage": "evaluation",
    "status": "active",
    "starting_balance": 100000.0,
    "balance": 100000.0,
    "created_at": 1_700_000_000_000,
}


def risk_snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "observed_at": 1_700_000_000_000,
        "marks_complete": True,
        "missing_markets": [],
        "equity": 100000.0,
        "unrealized_pnl": 0.0,
        "margin_used": 0.0,
        "available_balance": 100000.0,
        "gross_exposure": 0.0,
        "daily_loss_floor": 97000.0,
        "max_drawdown_floor": 95000.0,
        "daily_loss_room": 3000.0,
        "max_drawdown_room": 5000.0,
        "remaining_profit": 8000.0,
        "requirements": {
            "daily_loss_pct": 3.0,
            "max_drawdown_pct": 5.0,
            "profit_target_pct": 8.0,
            "consistency_pct": None,
            "minimum_trading_days": None,
        },
    }
    base.update(overrides)
    return base


@dataclass
class StubState:
    requests: list[dict[str, Any]] = field(default_factory=list)
    # Number of times to answer 503 before succeeding, keyed by path.
    fail_times: dict[str, int] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    risk: dict[str, Any] = field(default_factory=lambda: risk_snapshot())
    order_status: str = "filled"
    account_status: str = "active"

    def record(self, method: str, path: str, headers: dict[str, str], body: Any) -> None:
        self.requests.append({"method": method, "path": path, "headers": headers, "body": body})


class _Handler(BaseHTTPRequestHandler):
    state: StubState  # set on the server subclass

    def log_message(self, *args: Any) -> None:  # silence test output
        pass

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    def _send(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-Id", "req-123")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        body = self._read_body()
        headers = {k: v for k, v in self.headers.items()}
        self.state.record(method, self.path, headers, body)

        if self.state.fail_times.get(self.path, 0) > 0:
            self.state.fail_times[self.path] -= 1
            self._send(503, {"error": {"code": "unavailable", "message": "try later"}},
                       {"Retry-After": "0"})
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/v1":
            self._send(200, {"data": {"name": "MyFundedPerps API", "version": "v1", "environment": "sandbox"}})
        elif path == "/v1/accounts" and method == "GET":
            self._send(200, {"data": [ACCOUNT]})
        elif path == "/v1/accounts/acct-1":
            account = dict(ACCOUNT, status=self.state.account_status, risk=self.state.risk)
            self._send(200, {"data": account})
        elif path == "/v1/markets" and method == "GET":
            self._send(200, {"data": MARKETS})
        elif path.startswith("/v1/markets/") and path.endswith("/quote"):
            market_id = self._market_id_from_path(path[: -len("/quote")])
            mid = QUOTES.get(market_id)
            if mid is None:
                self._send(404, {"error": {"code": "not_found", "message": "no market"}})
                return
            self._send(200, {"data": {
                "status": "ok", "market_id": market_id, "provider": "binance",
                "symbol": market_id.split("|")[1], "coin": market_id.split("|")[1],
                "bid": mid - 0.1, "ask": mid + 0.1, "mid": mid,
                "time": 1_700_000_000_000, "fillable": True,
            }})
        elif path.startswith("/v1/markets/"):
            market_id = self._market_id_from_path(path)
            for m in MARKETS:
                if m["market_id"] == market_id:
                    self._send(200, {"data": m})
                    return
            self._send(404, {"error": {"code": "not_found", "message": "no market"}})
        elif path == "/v1/positions" and method == "GET":
            self._send(200, {"data": self.state.positions})
        elif path.endswith("/close-all-positions") and method == "POST":
            self._send(200, {"data": {"status": "completed", "operation_id": "op-close"}})
        elif path.endswith("/cancel-all-orders") and method == "POST":
            self._send(200, {"data": {"status": "completed", "operation_id": "op-cancel"}})
        elif path == "/v1/orders" and method == "POST":
            order = dict(body or {})
            seq = len(self.state.orders) + 1
            order.update({"id": f"order-{seq}", "status": self.state.order_status,
                          "filled_size": (body or {}).get("size"),
                          "position_id": f"pos-{seq}"})
            self.state.orders.append(order)
            self._send(201, {"data": order})
        elif path.startswith("/v1/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            for o in self.state.orders:
                if o["id"] == order_id:
                    self._send(200, {"data": o})
                    return
            self._send(404, {"error": {"code": "not_found", "message": "no order"}})
        elif path.startswith("/v1/positions/") and path.endswith("/close"):
            self._send(200, {"data": {"id": "close-1", "status": "filled"}})
        elif path == "/v1/error":
            self._send(422, {"error": {
                "code": "rule_violation", "message": "blocked by rule",
                "request_id": "req-err", "details": [{"field": "size", "issue": "too big"}],
            }})
        else:
            self._send(404, {"error": {"code": "not_found", "message": f"no route for {path}"}})

    @staticmethod
    def _market_id_from_path(path: str) -> str:
        from urllib.parse import unquote

        return unquote(path[len("/v1/markets/"):])

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


@pytest.fixture
def stub_server():
    state = StubState()
    handler = type("Handler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_candles(
    closes: list[float],
    *,
    start_time: int = 1_700_000_000_000,
    interval_ms: int = 60_000,
    symbol: str = "BTCUSDT",
    provider: str = "binance",
) -> list[Candle]:
    candles = []
    for i, close in enumerate(closes):
        open_time = start_time + i * interval_ms
        candles.append(
            Candle(
                provider=provider,
                symbol=symbol,
                interval="1m",
                open_time=open_time,
                close_time=open_time + interval_ms - 1,
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1.0,
                is_final=True,
            )
        )
    return candles
