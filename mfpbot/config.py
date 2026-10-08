"""Configuration loading from environment variables and optional YAML/JSON file."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger("mfpbot.config")

ENVIRONMENTS = {
    "live": "https://developers.myfundedperpetuals.com",
    "sandbox": "https://sandbox.myfundedperpetuals.com",
}

MARKET_STREAM_URL = "wss://api-stream.myfundedperpetuals.com/v1/market-data"

# Prefix -> environment, used to catch a key pointed at the wrong host early.
_KEY_ENVIRONMENTS = {"fp_live_": "live", "fp_test_": "sandbox"}


class ConfigError(ValueError):
    """Raised when configuration is missing or inconsistent."""


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    api_key: str = ""
    environment: str = "sandbox"
    account_id: str = ""
    market_id: str = "binance|BTCUSDT"
    symbols: list[str] = field(default_factory=list)

    strategy: str = "ema_cross"
    timeframe: str = "15m"
    ema_fast: int = 12
    ema_slow: int = 26
    atr_period: int = 14

    risk_per_trade_pct: float = 0.5
    atr_stop_mult: float = 2.0
    take_profit_rr: float = 2.0
    leverage: float = 2.0
    margin_mode: str = "cross"
    max_daily_loss_pct: float = 2.0
    max_daily_trades: int = 6
    min_daily_room_pct: float = 0.5
    # Bot-side cumulative drawdown guard, independent of the API's room figures
    # (which are null on a live challenge account). 0 = disabled.
    max_total_drawdown_pct: float = 0.0
    # "starting" measures drawdown from the account starting balance (static);
    # "peak" measures it from the highest equity ever observed (trailing).
    drawdown_basis: str = "starting"
    # Explicit acknowledgement to run live+bot-only with no cumulative guard.
    ack_no_drawdown_guard: bool = False
    # Cap total position margin as a percent of equity across all markets.
    max_margin_pct: float = 50.0
    # Allowed quote-vs-candle-close drift, as a multiple of ATR (bounded by the
    # absolute cap in bot.py).
    max_entry_drift_atr: float = 0.5
    # "bot" closes only positions this bot opened; "account" closes everything.
    flatten_scope: str = "bot"
    # "halt" or "bot-only"; empty means choose by environment (live -> halt).
    on_missing_room: str = ""

    poll_seconds: float = 15.0
    log_level: str = "INFO"
    state_file: str = "bot_state.json"
    dry_run: bool = False

    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def base_url(self) -> str:
        try:
            return ENVIRONMENTS[self.environment]
        except KeyError:
            raise ConfigError(
                f"Unknown environment {self.environment!r}. "
                f"Choose one of {sorted(ENVIRONMENTS)}."
            ) from None

    @property
    def market_stream_url(self) -> str:
        return MARKET_STREAM_URL

    @property
    def state_path(self) -> str:
        """State file to use; a dry run gets its own so it never touches live state."""
        return self.state_file + ".dryrun" if self.dry_run else self.state_file

    @property
    def missing_room_policy(self) -> str:
        """Effective policy when the account risk snapshot has null room figures.

        Defaults to ``halt`` on live (fail closed) and ``bot-only`` on sandbox.
        """
        if self.on_missing_room:
            return self.on_missing_room
        return "halt" if self.environment == "live" else "bot-only"

    @property
    def market_ids(self) -> list[str]:
        """Effective list of market IDs to trade (multi-asset)."""
        if self.symbols:
            return list(self.symbols)
        return [self.market_id]

    def validate(self, *, require_key: bool = True) -> None:
        if require_key:
            if not self.api_key:
                raise ConfigError("FP_API_KEY is required.")
            prefix_env = next(
                (env for prefix, env in _KEY_ENVIRONMENTS.items() if self.api_key.startswith(prefix)),
                None,
            )
            if prefix_env is None:
                raise ConfigError(
                    "FP_API_KEY does not look like a MyFundedPerps key "
                    "(expected a fp_live_ or fp_test_ prefix)."
                )
        else:
            prefix_env = None
        if prefix_env is not None and prefix_env != self.environment:
            raise ConfigError(
                f"FP_ENV is {self.environment!r} but the key is a {prefix_env!r} key. "
                f"Set FP_ENV={prefix_env}."
            )
        if "|" not in self.market_id:
            raise ConfigError("FP_MARKET_ID must look like 'provider|COIN', e.g. 'binance|BTCUSDT'.")
        market_ids = self.market_ids
        if not market_ids:
            raise ConfigError("At least one market is required (FP_SYMBOLS or FP_MARKET_ID).")
        if len(market_ids) > 32:
            raise ConfigError("At most 32 markets can be streamed on one connection.")
        if len(set(market_ids)) != len(market_ids):
            raise ConfigError("FP_SYMBOLS contains duplicate market IDs.")
        for mid in market_ids:
            if "|" not in mid:
                raise ConfigError(f"market ID {mid!r} must look like 'provider|COIN'.")
        if self.ema_fast >= self.ema_slow:
            raise ConfigError("FP_EMA_FAST must be smaller than FP_EMA_SLOW.")
        if self.risk_per_trade_pct <= 0:
            raise ConfigError("FP_RISK_PER_PCT must be greater than zero.")
        if self.leverage <= 0:
            raise ConfigError("FP_LEVERAGE must be greater than zero.")
        if self.margin_mode not in {"cross", "isolated"}:
            raise ConfigError("FP_MARGIN_MODE must be 'cross' or 'isolated'.")
        if self.max_daily_loss_pct <= 0:
            raise ConfigError("FP_MAX_DAILY_LOSS_PCT must be greater than zero.")
        if self.flatten_scope not in {"bot", "account"}:
            raise ConfigError("FP_FLATTEN_SCOPE must be 'bot' or 'account'.")
        if self.on_missing_room and self.on_missing_room not in {"halt", "bot-only"}:
            raise ConfigError("FP_ON_MISSING_ROOM must be 'halt' or 'bot-only'.")
        if not 0.0 <= self.max_total_drawdown_pct < 100.0:
            raise ConfigError("FP_MAX_TOTAL_DRAWDOWN_PCT must be >= 0 and < 100.")
        if self.drawdown_basis not in {"starting", "peak"}:
            raise ConfigError("FP_DRAWDOWN_BASIS must be 'starting' or 'peak'.")
        # Fail closed on the one configuration that removes every cumulative
        # drawdown guard: a live, non-dry-run bot-only run with no bot-side cap.
        # The API reports null room figures on challenge accounts, so nothing
        # else bounds a losing streak. A dry run never sends orders and a
        # sandbox account is not real money, so those only warn.
        if (
            self.missing_room_policy == "bot-only"
            and self.max_total_drawdown_pct == 0.0
            and not self.ack_no_drawdown_guard
        ):
            if self.environment == "live" and not self.dry_run:
                raise ConfigError(
                    "Live account with FP_ON_MISSING_ROOM=bot-only has no cumulative "
                    "drawdown guard: the API reports null room figures, so the bot-side "
                    "daily cap resets every UTC day. Set FP_MAX_TOTAL_DRAWDOWN_PCT to a "
                    "positive percent (e.g. 10), or set FP_ACK_NO_DRAWDOWN_GUARD=true to "
                    "accept the risk explicitly."
                )
            log.warning(
                "FP_ON_MISSING_ROOM=bot-only without FP_MAX_TOTAL_DRAWDOWN_PCT: no "
                "cumulative drawdown guard; only the per-day bot caps apply"
            )
        if not 0 < self.max_margin_pct <= 100:
            raise ConfigError("FP_MAX_MARGIN_PCT must be between 0 and 100.")
        if self.max_entry_drift_atr <= 0:
            raise ConfigError("FP_MAX_ENTRY_DRIFT_ATR must be greater than zero.")
        if self.poll_seconds <= 0:
            raise ConfigError("FP_POLL_SECONDS must be greater than zero.")


# Map config field name -> environment variable name.
_ENV_KEYS = {
    "api_key": "FP_API_KEY",
    "environment": "FP_ENV",
    "account_id": "FP_ACCOUNT_ID",
    "market_id": "FP_MARKET_ID",
    "symbols": "FP_SYMBOLS",
    "strategy": "FP_STRATEGY",
    "timeframe": "FP_TIMEFRAME",
    "ema_fast": "FP_EMA_FAST",
    "ema_slow": "FP_EMA_SLOW",
    "atr_period": "FP_ATR_PERIOD",
    "risk_per_trade_pct": "FP_RISK_PER_PCT",
    "atr_stop_mult": "FP_ATR_STOP_MULT",
    "take_profit_rr": "FP_TP_RR",
    "leverage": "FP_LEVERAGE",
    "margin_mode": "FP_MARGIN_MODE",
    "max_daily_loss_pct": "FP_MAX_DAILY_LOSS_PCT",
    "max_daily_trades": "FP_MAX_DAILY_TRADES",
    "min_daily_room_pct": "FP_MIN_DAILY_ROOM_PCT",
    "max_total_drawdown_pct": "FP_MAX_TOTAL_DRAWDOWN_PCT",
    "drawdown_basis": "FP_DRAWDOWN_BASIS",
    "ack_no_drawdown_guard": "FP_ACK_NO_DRAWDOWN_GUARD",
    "max_margin_pct": "FP_MAX_MARGIN_PCT",
    "max_entry_drift_atr": "FP_MAX_ENTRY_DRIFT_ATR",
    "flatten_scope": "FP_FLATTEN_SCOPE",
    "on_missing_room": "FP_ON_MISSING_ROOM",
    "poll_seconds": "FP_POLL_SECONDS",
    "log_level": "FP_LOG_LEVEL",
    "state_file": "FP_STATE_FILE",
    "dry_run": "FP_DRY_RUN",
}

_FLOAT_FIELDS = {
    "risk_per_trade_pct",
    "atr_stop_mult",
    "take_profit_rr",
    "leverage",
    "max_daily_loss_pct",
    "min_daily_room_pct",
    "max_total_drawdown_pct",
    "max_margin_pct",
    "max_entry_drift_atr",
    "poll_seconds",
}
_INT_FIELDS = {"ema_fast", "ema_slow", "atr_period", "max_daily_trades"}
_BOOL_FIELDS = {"dry_run", "ack_no_drawdown_guard"}
_LIST_FIELDS = {"symbols"}


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _LIST_FIELDS:
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [part.strip() for part in str(value).split(",") if part.strip()]
    if name in _FLOAT_FIELDS:
        return float(value)
    if name in _INT_FIELDS:
        return int(value)
    if name in _BOOL_FIELDS:
        return value if isinstance(value, bool) else _env_bool(str(value))
    return str(value)


def load_dotenv_file(path: str | os.PathLike[str] = ".env") -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (without overriding)."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _load_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ConfigError("PyYAML is required to read YAML config files.") from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ConfigError("Config file must contain a mapping of field names to values.")
    return data


def load_config(
    env: dict[str, str] | None = None,
    config_file: str | os.PathLike[str] | None = None,
    *,
    require_key: bool = True,
) -> Config:
    """Build a Config from defaults, an optional file, and environment variables.

    Precedence (low to high): defaults < config file < environment variables.
    Set ``require_key=False`` for public, unauthenticated commands.
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = {}

    if config_file:
        file_values = _load_file(config_file)
        valid = {f.name for f in fields(Config) if not f.name.startswith("_")}
        unknown = set(file_values) - valid
        if unknown:
            raise ConfigError(f"Unknown config fields: {sorted(unknown)}")
        values.update(file_values)

    for name, env_name in _ENV_KEYS.items():
        raw = env.get(env_name)
        if raw is None or raw == "":
            continue
        coerced = _coerce(name, raw)
        if coerced is not None:
            values[name] = coerced

    cfg = Config(**values)
    cfg.validate(require_key=require_key)
    return cfg
