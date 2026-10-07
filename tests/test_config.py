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


def test_symbols_from_config_file_list(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"symbols": ["binance|BTCUSDT", "binance|ETHUSDT"]}), encoding="utf-8")
    cfg = load_config(env={"FP_API_KEY": "fp_test_abc"}, config_file=str(path), require_key=True)
    assert cfg.market_ids == ["binance|BTCUSDT", "binance|ETHUSDT"]
