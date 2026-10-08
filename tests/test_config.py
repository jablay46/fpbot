"""Tests for configuration loading and validation."""

from __future__ import annotations

import json

import pytest

from mfpbot.config import ConfigError, load_config


def test_defaults_and_env_override():
    cfg = load_config(
        env={"FP_API_KEY": "fp_test_abc", "FP_RISK_PER_PCT": "1.5", "FP_EMA_FAST": "5"},
        require_key=True,
    )
    assert cfg.environment == "sandbox"
    assert cfg.risk_per_trade_pct == 1.5
    assert cfg.ema_fast == 5


def test_missing_key_rejected_when_required():
    with pytest.raises(ConfigError):
        load_config(env={}, require_key=True)


def test_public_config_without_key():
    cfg = load_config(env={}, require_key=False)
    assert cfg.api_key == ""


def test_key_environment_mismatch_rejected():
    with pytest.raises(ConfigError):
        load_config(env={"FP_API_KEY": "fp_live_abc", "FP_ENV": "sandbox"}, require_key=True)


def test_invalid_key_prefix_rejected():
    with pytest.raises(ConfigError):
        load_config(env={"FP_API_KEY": "sk_whatever"}, require_key=True)


def test_invalid_market_id_rejected():
    with pytest.raises(ConfigError):
        load_config(env={"FP_API_KEY": "fp_test_abc", "FP_MARKET_ID": "BTCUSDT"}, require_key=True)


def test_ema_fast_must_be_below_slow():
    with pytest.raises(ConfigError):
        load_config(env={"FP_API_KEY": "fp_test_abc", "FP_EMA_FAST": "30", "FP_EMA_SLOW": "10"})


def test_bool_coercion():
    cfg = load_config(env={"FP_API_KEY": "fp_test_abc", "FP_DRY_RUN": "true"}, require_key=True)
    assert cfg.dry_run is True


def test_dry_run_state_path_is_separate():
    live = load_config(env={"FP_API_KEY": "fp_test_abc", "FP_STATE_FILE": "s.json"}, require_key=True)
    dry = load_config(
        env={"FP_API_KEY": "fp_test_abc", "FP_STATE_FILE": "s.json", "FP_DRY_RUN": "true"},
        require_key=True,
    )
    assert live.state_path == "s.json"
    assert dry.state_path == "s.json.dryrun"


def test_on_missing_room_defaults_to_bot_only_in_sandbox():
    cfg = load_config(env={"FP_API_KEY": "fp_test_abc", "FP_ENV": "sandbox"}, require_key=True)
    assert cfg.missing_room_policy == "bot-only"


def test_on_missing_room_defaults_to_halt_in_live():
    cfg = load_config(env={"FP_API_KEY": "fp_live_abc", "FP_ENV": "live"}, require_key=True)
    assert cfg.missing_room_policy == "halt"


def test_on_missing_room_explicit_override():
    cfg = load_config(
        env={"FP_API_KEY": "fp_test_abc", "FP_ENV": "sandbox", "FP_ON_MISSING_ROOM": "halt"},
        require_key=True,
    )
    assert cfg.missing_room_policy == "halt"


def test_on_missing_room_invalid_rejected():
    with pytest.raises(ConfigError):
        load_config(
            env={"FP_API_KEY": "fp_test_abc", "FP_ON_MISSING_ROOM": "whatever"},
            require_key=True,
        )


def test_config_file_and_env_precedence(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"risk_per_trade_pct": 2.0, "ema_fast": 3}), encoding="utf-8")
    cfg = load_config(
        env={"FP_API_KEY": "fp_test_abc", "FP_RISK_PER_PCT": "0.25"},
        config_file=str(path),
        require_key=True,
    )
    assert cfg.ema_fast == 3          # from file
    assert cfg.risk_per_trade_pct == 0.25  # env wins


def test_unknown_config_field_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"not_a_field": 1}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(env={"FP_API_KEY": "fp_test_abc"}, config_file=str(path), require_key=True)


def test_symbols_parsed_from_env():
    cfg = load_config(
        env={"FP_API_KEY": "fp_test_abc",
             "FP_SYMBOLS": "binance|BTCUSDT, binance|ETHUSDT ,hyperliquid|xyz:AAPL"},
        require_key=True,
    )
    assert cfg.market_ids == ["binance|BTCUSDT", "binance|ETHUSDT", "hyperliquid|xyz:AAPL"]


def test_symbols_defaults_to_market_id():
    cfg = load_config(env={"FP_API_KEY": "fp_test_abc", "FP_MARKET_ID": "binance|SOLUSDT"}, require_key=True)
    assert cfg.market_ids == ["binance|SOLUSDT"]


def test_duplicate_symbols_rejected():
    with pytest.raises(ConfigError):
        load_config(
            env={"FP_API_KEY": "fp_test_abc", "FP_SYMBOLS": "binance|BTCUSDT,binance|BTCUSDT"},
            require_key=True,
        )


def test_too_many_symbols_rejected():
    symbols = ",".join(f"binance|SYM{i}USDT" for i in range(33))
    with pytest.raises(ConfigError):
        load_config(env={"FP_API_KEY": "fp_test_abc", "FP_SYMBOLS": symbols}, require_key=True)


def test_max_total_drawdown_pct_range():
    cfg = load_config(
        env={"FP_API_KEY": "fp_test_abc", "FP_MAX_TOTAL_DRAWDOWN_PCT": "10"},
        require_key=True,
    )
    assert cfg.max_total_drawdown_pct == 10.0
    with pytest.raises(ConfigError):
        load_config(
            env={"FP_API_KEY": "fp_test_abc", "FP_MAX_TOTAL_DRAWDOWN_PCT": "-1"},
            require_key=True,
        )
    with pytest.raises(ConfigError):
        load_config(
            env={"FP_API_KEY": "fp_test_abc", "FP_MAX_TOTAL_DRAWDOWN_PCT": "100"},
            require_key=True,
        )


def test_drawdown_basis_validated():
    cfg = load_config(
        env={"FP_API_KEY": "fp_test_abc", "FP_DRAWDOWN_BASIS": "peak"}, require_key=True
    )
    assert cfg.drawdown_basis == "peak"
    with pytest.raises(ConfigError):
        load_config(
            env={"FP_API_KEY": "fp_test_abc", "FP_DRAWDOWN_BASIS": "whatever"},
            require_key=True,
        )


def test_live_bot_only_without_drawdown_guard_refuses_start():
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            env={
                "FP_API_KEY": "fp_live_abc", "FP_ENV": "live",
                "FP_ON_MISSING_ROOM": "bot-only", "FP_MAX_TOTAL_DRAWDOWN_PCT": "0",
            },
            require_key=True,
        )
    assert "drawdown" in str(excinfo.value).lower()


def test_recommended_defaults():
    cfg = load_config(env={"FP_API_KEY": "fp_test_abc"}, require_key=True)
    assert cfg.strategy == "donchian_breakout"
    assert cfg.regime_adx_min == 20.0
    assert cfg.trend_ema == 200
    # The default cumulative guard matches the firm's static 3% Select floor.
    assert cfg.max_total_drawdown_pct == 3.0


def test_live_bot_only_is_allowed_by_the_default_drawdown_guard():
    # The recommended default (3%) means a live bot-only run no longer needs an
    # explicit acknowledgement to start.
    cfg = load_config(
        env={"FP_API_KEY": "fp_live_abc", "FP_ENV": "live", "FP_ON_MISSING_ROOM": "bot-only"},
        require_key=True,
    )
    assert cfg.max_total_drawdown_pct == 3.0


def test_live_bot_only_with_drawdown_guard_accepted():
    cfg = load_config(
        env={
            "FP_API_KEY": "fp_live_abc", "FP_ENV": "live",
            "FP_ON_MISSING_ROOM": "bot-only", "FP_MAX_TOTAL_DRAWDOWN_PCT": "10",
        },
        require_key=True,
    )
    assert cfg.max_total_drawdown_pct == 10.0


def test_live_bot_only_with_ack_accepted():
    cfg = load_config(
        env={
            "FP_API_KEY": "fp_live_abc", "FP_ENV": "live",
            "FP_ON_MISSING_ROOM": "bot-only", "FP_ACK_NO_DRAWDOWN_GUARD": "true",
        },
        require_key=True,
    )
    assert cfg.ack_no_drawdown_guard is True


def test_live_dry_run_bot_only_without_guard_warns_not_raises():
    cfg = load_config(
        env={
            "FP_API_KEY": "fp_live_abc", "FP_ENV": "live",
            "FP_ON_MISSING_ROOM": "bot-only", "FP_DRY_RUN": "true",
        },
        require_key=True,
    )
    assert cfg.missing_room_policy == "bot-only"


def test_sandbox_bot_only_without_guard_only_warns(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="mfpbot.config"):
        cfg = load_config(
            env={"FP_API_KEY": "fp_test_abc", "FP_ENV": "sandbox", "FP_ON_MISSING_ROOM": "bot-only"},
            require_key=True,
        )
    assert cfg.missing_room_policy == "bot-only"


def test_symbols_from_config_file_list(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"symbols": ["binance|BTCUSDT", "binance|ETHUSDT"]}), encoding="utf-8")
    cfg = load_config(env={"FP_API_KEY": "fp_test_abc"}, config_file=str(path), require_key=True)
    assert cfg.market_ids == ["binance|BTCUSDT", "binance|ETHUSDT"]
