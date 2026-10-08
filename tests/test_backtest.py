"""Tests for the backtester: costs, engine mechanics, metrics, walk-forward."""

from __future__ import annotations

import json

from mfpbot.backtest import (
    BacktestConfig,
    Backtester,
    Bar,
    CostModel,
    Metrics,
    compute_metrics,
    load_bars,
    save_bars_jsonl,
    walk_forward,
)
from mfpbot.strategy import build_strategy
from tests.conftest import make_bars, random_walk_bars


class _FlipStrategy:
    """Signals long on the last bar only; deterministic single trade."""

    min_candles = 2

    def evaluate(self, candles, market):
        from mfpbot.strategy.base import Signal

        if len(candles) < 2:
            return None
        if len(candles) == 3:
            price = candles[-1].close
            return Signal(action="long", reason="test", stop_price=price - 5, take_profit_price=price + 10)
        return None


def test_costs_are_off_when_disabled():
    c = CostModel(apply_costs=False)
    assert c.entry_price(100.0, "long") == 100.0
    assert c.exit_price(100.0, "long") == 100.0
    assert c.commission(10_000) == 0.0
    assert c.swap(10_000, 24) == 0.0


def test_cost_model_moves_fills_against_us():
    c = CostModel(slippage_bps=2.0)
    # 2 bps of 100 = 0.02 adverse.
    assert c.entry_price(100.0, "long") == 100.02
    assert c.entry_price(100.0, "short") == 99.98
    assert c.exit_price(100.0, "long") == 99.98
    assert c.exit_price(100.0, "short") == 100.02


def test_cost_model_matches_published_crypto_rates():
    c = CostModel()
    # 0.03% of a 10,000 notional fill.
    assert abs(c.commission(10_000) - 3.0) < 1e-9
    # Crypto swap: 0.03% per day, 24 hourly charges of $0.125 each per 10k.
    assert abs(c.swap(10_000, 24) - 3.0) < 1e-9
    assert abs(c.swap(10_000, 1) - 0.125) < 1e-9


def test_engine_takes_the_take_profit_and_books_a_profit():
    strat = _FlipStrategy()
    bars = make_bars([100, 100, 100, 100, 115], wick=1.0)
    result = Backtester(strat, BacktestConfig(starting_equity=10_000, risk_per_trade_pct=1.0)).run(bars)
    assert result.metrics.trades == 1
    assert result.metrics.final_equity > 10_000
    assert result.trades[0].reason == "take_profit"


def test_engine_stop_first_when_both_levels_are_inside_one_bar():
    strat = _FlipStrategy()
    # After entry at 100, one bar spans both the 95 stop and the 110 target.
    bars = make_bars([100, 100, 100, 100], wick=1.0)
    bars.append(Bar(open_time=bars[-1].open_time + 60_000, open=100, high=112, low=90, close=100, volume=1.0))
    result = Backtester(strat, BacktestConfig(starting_equity=10_000)).run(bars)
    assert result.metrics.trades == 1
    assert result.trades[0].reason == "stop"


def test_engine_never_trades_before_min_candles():
    strat = build_strategy("donchian_breakout", donchian_period=20, atr_period=14)
    bars = make_bars([100] * 5)
    result = Backtester(strat, BacktestConfig()).run(bars)
    assert result.metrics.trades == 0


def test_daily_loss_guard_halts_entries_for_the_day():
    strat = _FlipStrategy()
    # A losing stop on the entry bar, all inside one UTC day.
    bars = make_bars([100, 100, 100, 100], wick=1.0)
    bars.append(Bar(open_time=bars[-1].open_time + 60_000, open=100, high=101, low=80, close=80, volume=1.0))
    cfg = BacktestConfig(starting_equity=10_000, risk_per_trade_pct=5.0, max_daily_loss_pct=1.0)
    result = Backtester(strat, cfg).run(bars)
    assert result.halt_reason == "daily loss"


def test_drawdown_computed_from_starting_balance():
    curve = [(0, 10_000.0), (1, 12_000.0), (2, 9_000.0)]
    m = compute_metrics(curve, [-1000.0], starting_equity=10_000, periods_per_year=1)
    # Peak-to-trough on the curve: 12000 -> 9000 = 25%.
    assert abs(m.max_drawdown_pct - 25.0) < 1e-6
    assert m.trades == 1


def test_metrics_win_rate_and_profit_factor():
    curve = [(0, 10_000.0), (1, 11_000.0), (2, 10_500.0)]
    m = compute_metrics(curve, [1000.0, -500.0], starting_equity=10_000, periods_per_year=1)
    assert m.trades == 2 and m.wins == 1 and m.losses == 1
    assert abs(m.win_rate - 50.0) < 1e-9
    assert abs(m.profit_factor - 2.0) < 1e-9
    assert abs(m.expectancy - 250.0) < 1e-9


def test_walk_forward_splits_into_the_requested_folds():
    bars = random_walk_bars(400, seed=3)
    factory = lambda: build_strategy("donchian_breakout", donchian_period=10, atr_period=5)
    wf = walk_forward(factory, bars, BacktestConfig(starting_equity=10_000), folds=4)
    assert len(wf.folds) == 4
    assert 0.0 <= wf.profitable_fraction <= 1.0
    assert wf.worst_drawdown_pct >= 0.0


def test_compare_ranks_by_mar():
    from mfpbot.backtest import compare

    bars = random_walk_bars(600, seed=11)
    factories = {
        "ema_cross": lambda: build_strategy("ema_cross", fast=12, slow=26, atr_period=14),
        "donchian_breakout": lambda: build_strategy("donchian_breakout", donchian_period=20, atr_period=14),
    }
    ranked = compare(factories, bars, BacktestConfig(starting_equity=10_000))
    assert [name for name, _ in ranked] == sorted(
        [name for name, _ in ranked],
        key=lambda n: dict(ranked)[n].metrics.mar,
        reverse=True,
    )


def test_bar_file_roundtrip_jsonl(tmp_path):
    bars = make_bars([100, 101, 102, 103])
    path = tmp_path / "bars.jsonl"
    save_bars_jsonl(bars, path)
    loaded = load_bars(path)
    assert len(loaded) == 4
    assert loaded[0].open_time == bars[0].open_time
    assert loaded[-1].close == 103.0


def test_load_bars_reads_raw_mfp_candle_events(tmp_path):
    path = tmp_path / "live.jsonl"
    rows = [
        {"openTime": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3, "isFinal": True},
        {"openTime": 2, "open": 1.5, "high": 2.5, "low": 1, "close": 2.0, "volume": 4, "isFinal": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    loaded = load_bars(path)
    assert len(loaded) == 2 and loaded[1].close == 2.0


def test_cost_presets_match_the_published_schedules():
    from mfpbot.backtest.costs import ASSET_CLASS_COSTS, CostModel

    assert set(ASSET_CLASS_COSTS) == {"crypto", "tradfi", "forex"}
    fx = CostModel.for_asset_class("forex")
    assert abs(fx.commission(10_000) - 0.25) < 1e-9
    assert abs(fx.swap(10_000, 24) - 0.50) < 1e-9
    assert abs(fx.entry_price(100.0, "long") - 100.0005) < 1e-9
    tf = CostModel.for_asset_class("tradfi")
    assert abs(tf.commission(10_000) - 0.50) < 1e-9
    assert abs(tf.swap(10_000, 24) - 1.50) < 1e-9
    crypto = CostModel.for_asset_class("crypto", slippage_bps=6.0)
    assert crypto.slippage_bps == 6.0 and crypto.commission_pct == 0.03
    try:
        CostModel.for_asset_class("beanie-babies")
    except ValueError as exc:
        assert "asset class" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_engine_arms_breakeven_then_stops_at_entry_plus():
    strat = _FlipStrategy()
    # Entry at 100 (stop 95, TP 110). Bar 3 runs to 108, arming the +1R
    # breakeven; bar 4 falls back through the 100.5 breakeven stop.
    bars = make_bars([100, 100, 100, 107, 101], wick=1.0)
    cfg = BacktestConfig(starting_equity=10_000, breakeven_at_r=1.0, breakeven_plus_r=0.1)
    result = Backtester(strat, cfg).run(bars)
    assert result.metrics.trades == 1
    assert result.trades[0].reason == "breakeven_stop"
    assert result.trades[0].exit > 100.0  # scratched above entry, not a full -1R


def test_engine_trailing_stop_locks_in_profit():
    strat = _FlipStrategy()
    # Entry at 100, trend to 108, then a drop to 100. The 1xATR trail ratchets
    # up behind the rally and fills well above entry.
    bars = make_bars([100, 100, 100, 104, 108, 106, 100], wick=1.0)
    cfg = BacktestConfig(starting_equity=10_000, atr_period=2, trail_atr_mult=1.0)
    result = Backtester(strat, cfg).run(bars)
    assert result.metrics.trades == 1
    assert result.trades[0].reason == "trailing_stop"
    assert result.trades[0].exit > 100.0
    assert result.trades[0].pnl > 0


def test_engine_stop_wins_when_one_bar_spans_old_stop_and_breakeven_trigger():
    strat = _FlipStrategy()
    # Entry at 100. The next bar spikes to 106 (a +1R trigger) but also
    # crashes through the 95 stop: with no intrabar path, the stop wins.
    bars = make_bars([100, 100, 100], wick=1.0)
    bars.append(Bar(open_time=bars[-1].open_time + 60_000, open=100, high=106, low=90,
                    close=92, volume=1.0))
    cfg = BacktestConfig(starting_equity=10_000, breakeven_at_r=1.0)
    result = Backtester(strat, cfg).run(bars)
    assert result.trades[0].reason == "stop"


def test_engine_skips_entries_the_live_sizer_would_reject():
    strat = _FlipStrategy()
    bars = make_bars([100, 100, 100, 100, 115], wick=1.0)
    cfg = BacktestConfig(starting_equity=10_000, risk_per_trade_pct=1.0)
    result = Backtester(strat, cfg, market={"market_id": "x", "min_notional": 1e9}).run(bars)
    assert result.metrics.trades == 0
    result = Backtester(strat, cfg, market={"market_id": "x"}).run(bars)
    assert result.metrics.trades == 1


def test_bar_day_buckets_follow_the_configured_timezone():
    from datetime import datetime, timezone
    from mfpbot.backtest.engine import _bar_day

    # 00:30 UTC and 23:00 UTC are different UTC days but the same ET day.
    a = int(datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
    b = int(datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _bar_day(a, "America/New_York") == _bar_day(b, "America/New_York")
    assert _bar_day(a, "UTC") != _bar_day(b, "UTC")


def test_loader_rejects_mixed_symbol_files(tmp_path):
    import json

    from mfpbot.backtest import load_many

    def write(name, symbol):
        path = tmp_path / name
        rows = [
            {"open_time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5,
             "volume": 3, "provider": "binance", "symbol": symbol, "interval": "15m"},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows))
        return path

    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(
        "\n".join(json.dumps({
            "open_time": i, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3,
            "provider": "binance", "symbol": sym, "interval": "15m",
        }) for i, sym in enumerate(["BTCUSDT", "ETHUSDT"]))
    )
    try:
        load_bars(mixed)
    except ValueError as exc:
        assert "mix" in str(exc)
    else:
        raise AssertionError("expected ValueError for a mixed file")
    try:
        load_many([write("a.jsonl", "BTCUSDT"), write("b.jsonl", "ETHUSDT")])
    except ValueError as exc:
        assert "mix" in str(exc)
    else:
        raise AssertionError("expected ValueError for mixed files")
    # Same symbol across shards still merges.
    merged = load_many([write("a.jsonl", "BTCUSDT"), write("c.jsonl", "BTCUSDT")])
    assert len(merged) == 1
