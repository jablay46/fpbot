"""Tests for the REST client against a real local HTTP stub server."""

from __future__ import annotations

import pytest

from mfpbot.client import ApiError, MfpClient


@pytest.fixture
def client(stub_server):
    base_url, state = stub_server
    c = MfpClient("fp_test_abc", base_url, max_retries=3, sleep=lambda _s: None)
    return c, state


def test_list_accounts_unwraps_data(client):
    c, _ = client
    accounts = c.list_accounts()
    assert accounts[0]["id"] == "acct-1"


def test_auth_header_sent(client):
    c, state = client
    c.list_accounts()
    assert state.requests[-1]["headers"]["Authorization"] == "Bearer fp_test_abc"


def test_market_id_path_is_encoded(client):
    c, state = client
    c.get_market("binance|BTCUSDT")
    assert state.requests[-1]["path"] == "/v1/markets/binance%7CBTCUSDT"


def test_quote_passes_side_and_size(client):
    c, state = client
    quote = c.get_quote("binance|BTCUSDT", side="buy", size=0.001)
    assert quote["mid"] == 100.1
    assert "side=buy" in state.requests[-1]["path"]
    assert "size=0.001" in state.requests[-1]["path"]


def test_place_order_sends_idempotency_key(client):
    c, state = client
    order = c.place_order(
        {"account_id": "acct-1", "market_id": "binance|BTCUSDT", "side": "buy",
         "size": 0.01, "leverage": 2, "margin_mode": "cross"},
        idempotency_key="idem-1",
    )
    assert order["id"] == "order-1"
    assert state.requests[-1]["headers"]["Idempotency-Key"] == "idem-1"


def test_retry_on_503_then_success(client):
    c, state = client
    state.fail_times["/v1/accounts"] = 2
    accounts = c.list_accounts()
    assert accounts[0]["id"] == "acct-1"
    # two failed attempts plus one success
    assert len([r for r in state.requests if r["path"] == "/v1/accounts"]) == 3


def test_error_envelope_is_parsed(client):
    c, _ = client
    with pytest.raises(ApiError) as excinfo:
        c._request("GET", "/v1/error")
    err = excinfo.value
    assert err.status == 422
    assert err.code == "rule_violation"
    assert err.request_id == "req-err"
    assert err.details == [{"field": "size", "issue": "too big"}]
    assert err.is_retryable is False


def test_find_order_by_client_id_filters_and_verifies(client):
    c, state = client
    c.place_order(
        {"account_id": "acct-1", "market_id": "binance|BTCUSDT", "side": "buy",
         "size": 0.01, "leverage": 2, "margin_mode": "cross",
         "client_order_id": "coid-1"},
    )
    found = c.find_order_by_client_id("coid-1", account_id="acct-1")
    assert found is not None and found["client_order_id"] == "coid-1"
    # A filter that matches nothing returns None, never a stray order.
    assert c.find_order_by_client_id("coid-missing", account_id="acct-1") is None


def test_find_order_by_client_id_rejects_mismatch(client):
    """If the server ignores the filter, a non-matching order is not adopted."""
    c, state = client
    c.place_order(
        {"account_id": "acct-1", "market_id": "binance|BTCUSDT", "side": "buy",
         "size": 0.01, "leverage": 2, "margin_mode": "cross",
         "client_order_id": "coid-1"},
    )
    state.ignore_order_filter = True
    assert c.find_order_by_client_id("coid-other", account_id="acct-1") is None


def test_find_order_by_client_id_does_not_trust_the_first_row(client):
    """A misbehaving server that ignores the filter must not hide the real order.

    Returning the first row (or None on row[0] mismatch) would make the bot
    treat a live, unfilled order as never accepted and drop its pending marker —
    exactly the duplicate-entry risk. The lookup must scan for the id.
    """
    c, state = client
    for coid in ("coid-a", "coid-b", "coid-target"):
        c.place_order(
            {"account_id": "acct-1", "market_id": "binance|BTCUSDT", "side": "buy",
             "size": 0.01, "leverage": 2, "margin_mode": "cross",
             "client_order_id": coid},
        )
    state.ignore_order_filter = True  # every order comes back
    found = c.find_order_by_client_id("coid-target", account_id="acct-1")
    assert found is not None and found["client_order_id"] == "coid-target"


def test_find_order_by_client_id_pages_through_the_full_list(client):
    """A cursor-paginated server must still be scanned end to end."""
    c, state = client
    for i in range(5):
        c.place_order(
            {"account_id": "acct-1", "market_id": "binance|BTCUSDT", "side": "buy",
             "size": 0.01, "leverage": 2, "margin_mode": "cross",
             "client_order_id": f"coid-{i}"},
        )
    state.order_page_size = 2  # items + next_cursor
    found = c.find_order_by_client_id("coid-4", account_id="acct-1")
    assert found is not None and found["client_order_id"] == "coid-4"


def test_public_client_without_key(stub_server):
    base_url, state = stub_server
    c = MfpClient("", base_url, sleep=lambda _s: None)
    c.list_markets()
    assert "Authorization" not in state.requests[-1]["headers"]
