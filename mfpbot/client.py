"""Synchronous REST client for the MyFundedPerps developer API (v1, beta).

Wraps authentication, retries, rate-limit handling, and idempotency so the rest
of the bot can call plain methods.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Any
from urllib.parse import quote

import requests

log = logging.getLogger("mfpbot.client")

RETRY_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = 20.0
MAX_RETRIES = 5


class ApiError(RuntimeError):
    """An error returned by the MyFundedPerps API or the transport layer."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.details = details

    @property
    def is_retryable(self) -> bool:
        return self.status in RETRY_STATUSES or self.status is None


def _encode_market_id(market_id: str) -> str:
    # Market IDs contain a pipe; encode it for use in a path segment.
    return quote(market_id, safe="")


class MfpClient:
    """Thin, dependency-light client over the MyFundedPerps REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
        max_retries: int = MAX_RETRIES,
        sleep=time.sleep,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "mfpbot/0.1",
            }
        )
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"

    # -- transport --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        retries: int | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        attempts = (self.max_retries if retries is None else retries) + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    self._backoff(attempt, None)
                    continue
                raise ApiError(f"Request to {url} failed: {exc}") from exc

            if resp.status_code in RETRY_STATUSES and attempt < attempts - 1:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue

            return self._handle(resp)

        # Unreachable: the loop either returns or raises.
        raise ApiError(f"Request to {url} failed after retries") from last_exc

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 0.0
        else:
            delay = 0.0
        base = min(2.0 ** attempt, 30.0) + random.uniform(0, 0.5)
        self._sleep(max(delay, base))

    def _handle(self, resp: requests.Response) -> Any:
        request_id = resp.headers.get("X-Request-Id")
        text = resp.text
        payload: Any = None
        if text:
            try:
                payload = resp.json()
            except ValueError:
                payload = None

        if 200 <= resp.status_code < 300:
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload

        code = None
        message = f"HTTP {resp.status_code}"
        details = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            code = err.get("code")
            message = err.get("message") or message
            details = err.get("details")
            request_id = err.get("request_id") or request_id
        elif text:
            message = f"HTTP {resp.status_code}: {text[:300]}"

        raise ApiError(
            message,
            status=resp.status_code,
            code=code,
            request_id=request_id,
            details=details,
        )

    @staticmethod
    def new_idempotency_key() -> str:
        return str(uuid.uuid4())

    # -- general ----------------------------------------------------------

    def get_api_info(self) -> dict[str, Any]:
        return self._request("GET", "/v1")

    def list_restricted_countries(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/restricted-countries")

    # -- accounts ---------------------------------------------------------

    def list_accounts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/accounts")

    def get_account(self, account_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/accounts/{quote(account_id, safe='')}")

    def get_trading_policy(self, account_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/accounts/{quote(account_id, safe='')}/trading-policy")

    def cancel_all_orders(self, account_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/accounts/{quote(account_id, safe='')}/cancel-all-orders",
            idempotency_key=idempotency_key or self.new_idempotency_key(),
        )

    def close_all_positions(self, account_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/accounts/{quote(account_id, safe='')}/close-all-positions",
            idempotency_key=idempotency_key or self.new_idempotency_key(),
        )

    # -- markets ----------------------------------------------------------

    def list_markets(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/markets")

    def get_market(self, market_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/markets/{_encode_market_id(market_id)}")

    def get_quote(
        self,
        market_id: str,
        *,
        side: str | None = None,
        size: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if side is not None:
            params["side"] = side
        if size is not None:
            params["size"] = size
        return self._request("GET", f"/v1/markets/{_encode_market_id(market_id)}/quote", params=params)

    # -- positions --------------------------------------------------------

    def list_positions(self, account_id: str, *, status: str = "open") -> list[dict[str, Any]]:
        return self._request("GET", "/v1/positions", params={"account_id": account_id, "status": status})

    def get_position(self, position_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/positions/{quote(position_id, safe='')}")

    def close_position(
        self,
        position_id: str,
        *,
        size: float | None = None,
        client_order_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if size is not None:
            body["size"] = size
        if client_order_id is not None:
            body["client_order_id"] = client_order_id
        return self._request(
            "POST",
            f"/v1/positions/{quote(position_id, safe='')}/close",
            body=body,
            idempotency_key=idempotency_key or self.new_idempotency_key(),
        )

    def set_exit_orders(self, position_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PUT", f"/v1/positions/{quote(position_id, safe='')}/exit-orders", body=body
        )

    # -- orders -----------------------------------------------------------

    def place_order(
        self,
        order: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/orders",
            body=order,
            idempotency_key=idempotency_key or self.new_idempotency_key(),
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/orders/{quote(order_id, safe='')}")

    def list_orders(
        self,
        *,
        account_id: str | None = None,
        status: str | None = None,
        client_order_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if account_id:
            params["account_id"] = account_id
        if status:
            params["status"] = status
        if client_order_id:
            params["client_order_id"] = client_order_id
        if limit is not None:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/v1/orders", params=params)

    def find_order_by_client_id(self, client_order_id: str, *, account_id: str | None = None) -> dict[str, Any] | None:
        data = self.list_orders(client_order_id=client_order_id, account_id=account_id)
        rows = data if isinstance(data, list) else data.get("data", [])
        return rows[0] if rows else None

    def modify_order(self, order_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/v1/orders/{quote(order_id, safe='')}", body=body)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/orders/{quote(order_id, safe='')}")

    def list_fills(self, account_id: str, *, limit: int | None = None, cursor: str | None = None) -> Any:
        params: dict[str, Any] = {"account_id": account_id}
        if limit is not None:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/v1/fills", params=params)

    # -- strategy orders --------------------------------------------------

    def create_twap(self, body: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/twap-orders", body=body, idempotency_key=idempotency_key or self.new_idempotency_key()
        )

    def list_twap(self, account_id: str, *, status: str | None = None, limit: int | None = None, cursor: str | None = None) -> Any:
        params: dict[str, Any] = {"account_id": account_id}
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/v1/twap-orders", params=params)

    def stop_twap(self, twap_order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/twap-orders/{quote(twap_order_id, safe='')}")

    def create_scaled(self, body: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/scaled-orders", body=body, idempotency_key=idempotency_key or self.new_idempotency_key()
        )

    def get_scaled(self, scaled_order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/scaled-orders/{quote(scaled_order_id, safe='')}")

    def cancel_scaled(self, scaled_order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/scaled-orders/{quote(scaled_order_id, safe='')}")
