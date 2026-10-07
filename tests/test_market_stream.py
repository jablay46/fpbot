"""Tests for the market-data stream helpers (no network)."""

from __future__ import annotations

import json

from mfpbot.market_stream import CandleSeries, MarketDataStream
from tests.conftest import make_candles


def test_for_markets_groups_by_provider():
    markets = [
        {"market_id": "binance|BTCUSDT", "provider": "binance", "coin": "BTCUSDT"},
        {"market_id": "binance|ETHUSDT", "provider": "binance", "coin": "ETHUSDT"},
        {"market_id": "hyperliquid|xyz:AAPL", "provider": "hyperliquid", "coin": "xyz:AAPL"},
    ]
    stream = MarketDataStream.for_markets("wss://example", markets, interval="5m")
    assert stream.groups == [
        {"symbols": ["BTCUSDT", "ETHUSDT"], "providers": ["binance"]},
        {"symbols": ["xyz:AAPL"], "providers": ["hyperliquid"]},
    ]
    frame = json.loads(stream._subscribe_frame(1, stream.groups[0]))
    assert frame["op"] == "sub"
    assert frame["channel"] == "candles"
    assert frame["payload"]["providers"] == ["binance"]
    assert frame["payload"]["intervals"] == ["5m"]


def test_for_markets_deduplicates_coins():
    markets = [
        {"market_id": "binance|BTCUSDT", "provider": "binance", "coin": "BTCUSDT"},
        {"market_id": "binance|BTCUSDT", "provider": "binance", "coin": "BTCUSDT"},
    ]
    stream = MarketDataStream.for_markets("wss://example", markets)
    assert stream.groups == [{"symbols": ["BTCUSDT"], "providers": ["binance"]}]


def test_candle_series_keeps_last_and_forming():
    series = CandleSeries()
    candles = make_candles([100, 101, 102])
    for candle in candles[:2]:
        series.add(candle)
    assert len(series) == 2
    assert series.last_price == 101
    # A non-final bar is tracked as the forming candle but not counted as closed.
    forming = candles[2]
    forming.is_final = False
    series.add(forming)
    assert len(series) == 2
    assert series.forming is forming
    assert series.last_price == 102


def test_candle_series_replaces_duplicate_open_time():
    series = CandleSeries()
    for candle in make_candles([100, 101]):
        series.add(candle)
    replacement = make_candles([999])[0]
    replacement.open_time = series.closed[-1].open_time
    series.add(replacement)
    assert len(series) == 2
    assert series.closed[-1].close == 999
