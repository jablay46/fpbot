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
from urllib.parse import parse_qs, unquote, urlparse

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
    # Mid price served per market; tests mutate this to move the quote.
    quotes: dict[str, float] = field(default_factory=lambda: dict(QUOTES))
    order_status: str = "filled"
    account_status: str = "active"
    # When true, drop every POST /v1/orders response after recording the order,
    # simulating the order being accepted but the reply lost (client retries all
    # fail too).
    drop_order_response: bool = False
    # Number of GET /v1/orders responses to answer 503 (lookup also failing).
    fail_order_lookup_times: int = 0
    # Number of extra polls during which a closed position still appears open.
    slow_close_polls: int = 0
    # Number of close requests to reject (422, non-retryable) before accepting.
    fail_close_times: int = 0
    # When true, GET /v1/accounts records but drops the response.
    drop_account_response: bool = False
    # Simulated current time in ms for the account risk snapshot.
    now_ms: int = 1_700_000_000_000
    # Positions already closed by id; used to model the close-lag window.
    closed_position_ids: list[str] = field(default_factory=list)
    # Monotonic counter so exchange position ids never collide with ones a test
    # seeded directly into ``positions``.
    position_seq: int = 0

    def record(self, method: str, path: str, headers: dict[str, str], body: Any) -> None:
        self.requests.append({"method": method, "path": path, "headers": headers, "body": body})

    def open_positions(self) -> list[dict[str, Any]]:
        """Positions as the API would report them, honouring the close lag."""
        rows = []
        for pos in self.positions:
            if pos.get("status") != "open":
                continue
            if pos["id"] in self.closed_position_ids:
                # A closed position can linger for a few polls on the real API.
                if self.slow_close_polls > 0:
                    self.slow_close_polls -= 1
                    rows.append(pos)
                continue
            rows.append(pos)
        return rows


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
            if self.state.drop_account_response:
                return
            self._send(200, {"data": account})
        elif path == "/v1/markets" and method == "GET":
            self._send(200, {"data": MARKETS})
        elif path.startswith("/v1/markets/") and path.endswith("/quote"):
            market_id = self._market_id_from_path(path[: -len("/quote")])
            mid = self.state.quotes.get(market_id)
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
            self._send(200, {"data": self.state.open_positions()})
        elif path.endswith("/close-all-positions") and method == "POST":
            self._send(200, {"data": {"status": "completed", "operation_id": "op-close"}})
        elif path.endswith("/cancel-all-orders") and method == "POST":
            self._send(200, {"data": {"status": "completed", "operation_id": "op-cancel"}})
        elif path == "/v1/orders" and method == "POST":
            order = dict(body or {})
            # Idempotency: a repeat of the same client_order_id returns the same
            # order instead of opening a second position.
            existing = next(
                (o for o in self.state.orders if o.get("client_order_id") == order.get("client_order_id")),
                None,
            )
            if existing is None:
                seq = len(self.state.orders) + 1
                used = {p["id"] for p in self.state.positions}
                self.state.position_seq += 1
                position_id = f"pos-{self.state.position_seq}"
                while position_id in used:
                    self.state.position_seq += 1
                    position_id = f"pos-{self.state.position_seq}"
                order.update({"id": f"order-{seq}", "status": self.state.order_status,
                              "filled_size": (body or {}).get("size"),
                              "position_id": position_id})
                self.state.orders.append(order)
                # Mirror the live API: a filled entry opens a position, which the
                # bot must then discover and adopt by market id.
                if self.state.order_status == "filled":
                    self.state.positions.append({
                        "id": position_id, "account_id": order.get("account_id", "acct-1"),
                        "market_id": order.get("market_id"), "provider": "binance",
                        "symbol": order.get("market_id", "").split("|")[-1][:3],
                        "coin": order.get("market_id", "").split("|")[-1],
                        "side": "long" if order.get("side") == "buy" else "short",
                        "size": order.get("size"), "entry_price": order.get("expected_price") or 100.0,
                        "leverage": order.get("leverage") or 2.0,
                        "margin_mode": order.get("margin_mode", "cross"),
                        "status": "open", "opened_at": seq,
                    })
                existing = order
            if self.state.drop_order_response:
                # The order was accepted (and recorded) but every reply is lost,
                # so the client exhausts its retries and raises.
                return
            self._send(201, {"data": existing})
        elif path == "/v1/orders" and method == "GET":
            if self.state.fail_order_lookup_times > 0:
                self.state.fail_order_lookup_times -= 1
                self._send(503, {"error": {"code": "unavailable", "message": "try later"}})
                return
            rows = list(self.state.orders)
            wanted = query.get("client_order_id", [None])[0]
            if wanted:
                rows = [o for o in rows if o.get("client_order_id") == wanted]
            self._send(200, {"data": rows})
        elif path.startswith("/v1/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            if self.state.fail_order_lookup_times > 0:
                self.state.fail_order_lookup_times -= 1
                self._send(503, {"error": {"code": "unavailable", "message": "try later"}})
                return
            for o in self.state.orders:
                if o["id"] == order_id:
                    self._send(200, {"data": o})
                    return
            self._send(404, {"error": {"code": "not_found", "message": "no order"}})
        elif path.startswith("/v1/positions/") and path.endswith("/close"):
            if self.state.fail_close_times > 0:
                self.state.fail_close_times -= 1
                self._send(422, {"error": {"code": "rejected", "message": "close rejected"}})
                return
            position_id = unquote(path[len("/v1/positions/"): -len("/close")])
            if position_id not in self.state.closed_position_ids:
                self.state.closed_position_ids.append(position_id)
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
    interval: str = "1m",
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
                interval=interval,
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
