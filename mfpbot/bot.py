"""Bot orchestration: reconcile state, evaluate signals, size and place orders."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from .client import ApiError, MfpClient
from .config import Config
from .market_stream import Candle, CandleSeries, MarketDataStream
from .risk.manager import RiskManager, RiskState
from .risk.sizing import PositionSizer
from .state import BotState, load_state, save_state
from .strategy import build_strategy
from .util import fmt, round_step

log = logging.getLogger("mfpbot.bot")

TERMINAL_ORDER_STATES = {"filled", "rejected", "canceled", "cancelled", "expired"}


class Bot:
    def __init__(
        self,
        config: Config,
        *,
        client: Optional[MfpClient] = None,
        state: Optional[BotState] = None,
    ) -> None:
        self.cfg = config
        self.client = client or MfpClient(config.api_key, config.base_url)
        self.state = state or load_state(config.state_file)
        self.strategy = build_strategy(
            config.strategy,
            fast=config.ema_fast,
            slow=config.ema_slow,
            atr_period=config.atr_period,
            atr_stop_mult=config.atr_stop_mult,
            take_profit_rr=config.take_profit_rr,
        )
        self.account: Optional[dict[str, Any]] = None
        self.market: Optional[dict[str, Any]] = None
        self.risk: Optional[RiskManager] = None
        self.series = CandleSeries(max_len=1000)

    # -- setup ------------------------------------------------------------

    def resolve_account(self) -> dict[str, Any]:
        accounts = self.client.list_accounts()
        if not accounts:
            raise RuntimeError("no accounts are accessible with this API key")
        if self.cfg.account_id:
            for acct in accounts:
                if acct["id"] == self.cfg.account_id:
                    self.account = self.client.get_account(acct["id"])
                    break
            else:
                raise RuntimeError(f"account {self.cfg.account_id!r} is not accessible with this key")
        else:
            active = [a for a in accounts if a.get("status") == "active"]
            chosen = active[0] if active else accounts[0]
            self.account = self.client.get_account(chosen["id"])
        return self.account

    def resolve_market(self) -> dict[str, Any]:
        self.market = self.client.get_market(self.cfg.market_id)
        return self.market

    def _build_risk_manager(self) -> RiskManager:
        starting = float(self.account.get("starting_balance") or 0.0)
        return RiskManager(
            max_daily_trades=self.cfg.max_daily_trades,
            max_daily_loss_pct=self.cfg.max_daily_loss_pct,
            min_daily_room_pct=self.cfg.min_daily_room_pct,
            starting_balance=starting,
        )

    # -- helpers ----------------------------------------------------------

    def _account_risk(self) -> dict[str, Any]:
        self.account = self.client.get_account(self.account["id"])
        return self.account.get("risk") or {}

    def _equity(self, account_risk: dict[str, Any]) -> float:
        equity = account_risk.get("equity")
        if equity is None:
            equity = self.account.get("balance") or 0.0
        return float(equity)

    def _open_position(self) -> Optional[dict[str, Any]]:
        positions = self.client.list_positions(self.account["id"], status="open")
        for pos in positions:
            if pos.get("market_id") == self.cfg.market_id and pos.get("status") == "open":
                return pos
        return positions[0] if positions else None

    def _mid_price(self, side: str, size: float | None = None) -> Optional[float]:
        quote = self.client.get_quote(self.cfg.market_id, side=side, size=size)
        for key in ("mid", "reference_price", "ask" if side == "buy" else "bid"):
            value = quote.get(key)
            if value:
                return float(value)
        return None

    def _size_step(self) -> float:
        return float(self.market.get("size_step") or 0.0)

    # -- trading actions --------------------------------------------------

    def _place_entry(self, side: str, size: float, entry: float, stop: float, tp: float) -> Optional[dict[str, Any]]:
        client_order_id = f"mfpbot:{self.cfg.market_id}:{uuid.uuid4().hex[:12]}"
        order = {
            "client_order_id": client_order_id,
            "type": "market",
            "account_id": self.account["id"],
            "market_id": self.cfg.market_id,
            "side": side,
            "size": size,
            "expected_price": entry,
            "leverage": self.cfg.leverage,
            "margin_mode": self.cfg.margin_mode,
            "take_profit_price": tp,
            "stop_loss_price": stop,
        }
        if self.cfg.dry_run:
            log.info("[dry-run] would place %s %s %s @~%s (SL %s, TP %s)",
                     side, fmt(size), self.cfg.market_id, fmt(entry), fmt(stop), fmt(tp))
            return {"id": "dry-run", "status": "filled", "client_order_id": client_order_id}

        try:
            created = self.client.place_order(order)
        except ApiError as exc:
            log.error("order rejected: %s (code=%s rule=%s)", exc, exc.code, (exc.details or {}))
            return None

        self.state.last_entry_client_order_id = client_order_id
        return self._await_order(created)

    def _await_order(self, created: dict[str, Any], timeout: float = 20.0) -> Optional[dict[str, Any]]:
        status = created.get("status")
        order_id = created.get("id")
        deadline = time.monotonic() + timeout
        delay = 0.5
        while status not in TERMINAL_ORDER_STATES and order_id and time.monotonic() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.5, 3.0)
            try:
                current = self.client.get_order(order_id)
            except ApiError as exc:
                log.warning("could not poll order %s: %s", order_id, exc)
                continue
            status = current.get("status")
            created = current
        if status == "filled":
            log.info("order filled: %s %s %s", created.get("side"), fmt(created.get("filled_size")), self.cfg.market_id)
            return created
        log.warning("order %s ended in status %s", order_id, status)
        return None

    def _close_position(self, position: dict[str, Any]) -> bool:
        if self.cfg.dry_run:
            log.info("[dry-run] would close position %s", position["id"])
            return True
        try:
            self.client.close_position(position["id"], client_order_id=f"mfpbot-close:{uuid.uuid4().hex[:12]}")
        except ApiError as exc:
            log.error("failed to close position %s: %s", position["id"], exc)
            return False
        log.info("close submitted for position %s (%s)", position["id"], position.get("side"))
        return True

    def _flatten(self) -> None:
        if self.cfg.dry_run:
            log.info("[dry-run] would flatten account %s", self.account["id"])
            return
        key = self.client.new_idempotency_key()
        for _ in range(20):
            try:
                result = self.client.close_all_positions(self.account["id"], idempotency_key=key)
            except ApiError as exc:
                log.error("flatten failed: %s", exc)
                return
            if not isinstance(result, dict) or result.get("status") == "completed":
                log.info("flatten completed")
                return
            # status == "running": more targets remain; repeat with the same key.
            time.sleep(2.0)
        log.warning("flatten did not report completion within retry budget")

    # -- per-candle logic -------------------------------------------------

    def on_closed_candle(self, candle: Candle) -> None:
        account_risk = self._account_risk()
        status = self.account.get("status")
        if status in {"failed", "closed"}:
            log.error("account %s is %s; stopping trading", self.account["id"], status)
            self.state.risk.halted = True
            self.state.risk.halt_reason = f"account {status}"
            save_state(self.cfg.state_file, self.state)
            raise SystemExit(0)

        equity = self._equity(account_risk)
        self.risk.roll_day(self.state.risk, equity)

        position = self._open_position()
        signal = self.strategy.evaluate(self.series.closed, self.market)

        log.info(
            "candle %s close=%s | equity=%s | daily_loss_room=%s | position=%s | signal=%s",
            candle.interval,
            fmt(candle.close),
            fmt(equity, 2),
            fmt(account_risk.get("daily_loss_room"), 2),
            position.get("side") if position else "flat",
            signal.action if signal else "-",
        )

        if position is not None:
            if signal is not None and self._is_opposite(signal.action, position.get("side")):
                log.info("reversal signal (%s); closing %s position", signal.reason, position.get("side"))
                if self._close_position(position):
                    position = None
            else:
                return

        decision = self.risk.can_open(self.state.risk, equity, account_risk)
        if not decision.allowed:
            log.warning("no new entry: %s", decision.reason)
            if decision.flatten:
                self._flatten()
                self.risk.halt(self.state.risk, decision.reason)
                save_state(self.cfg.state_file, self.state)
            return

        if signal is None:
            return

        self._open_from_signal(signal, candle, account_risk, equity)

    @staticmethod
    def _is_opposite(action: str, position_side: Optional[str]) -> bool:
        if position_side is None:
            return False
        return (action == "long" and position_side == "short") or (
            action == "short" and position_side == "long"
        )

    def _open_from_signal(self, signal, candle: Candle, account_risk: dict, equity: float) -> None:
        side = "buy" if signal.action == "long" else "sell"
        entry = self._mid_price(side)
        if entry is None:
            log.warning("no quote available; skipping entry")
            return
        if signal.stop_price is None:
            log.warning("signal has no stop price; skipping entry")
            return

        # Re-anchor the ATR stop distance to the actual entry price.
        reference_close = candle.close
        distance = abs(reference_close - signal.stop_price)
        if distance <= 0:
            log.warning("signal has no usable stop distance; skipping entry")
            return
        if signal.action == "long":
            stop = entry - distance
            tp = entry + distance * self.cfg.take_profit_rr
        else:
            stop = entry + distance
            tp = entry - distance * self.cfg.take_profit_rr

        sizer = PositionSizer(
            risk_per_trade_pct=self.cfg.risk_per_trade_pct,
            leverage=self.cfg.leverage,
            min_notional=float(self.market.get("min_notional") or 0.0),
            min_size=float(self.market.get("min_size") or 0.0),
            size_step=self._size_step(),
            max_leverage=float(self.market.get("max_leverage") or self.cfg.leverage),
            available_balance=account_risk.get("available_balance"),
        )
        sizing = sizer.size(equity=equity, entry=entry, stop=stop)
        if not sizing.ok:
            log.warning("skipping entry: %s", sizing.reason)
            return

        log.info(
            "entry %s size=%s entry=%s stop=%s tp=%s risk=%s margin=%s (%s)",
            side, fmt(sizing.size), fmt(entry), fmt(stop), fmt(tp),
            fmt(sizing.risk_amount, 2), fmt(sizing.required_margin, 2), signal.reason,
        )
        filled = self._place_entry(side, sizing.size, entry, stop, tp)
        if filled is not None:
            self.risk.record_entry(self.state.risk)
            save_state(self.cfg.state_file, self.state)

    # -- run loop ---------------------------------------------------------

    async def run(self) -> None:
        self.resolve_account()
        self.market = self.resolve_market()
        self.risk = self._build_risk_manager()

        coin = self.market.get("coin")
        provider = self.market.get("provider")
        log.info(
            "starting bot: account=%s (%s) market=%s strategy=%s timeframe=%s env=%s dry_run=%s",
            self.account.get("name") or self.account["id"],
            self.account.get("status"),
            self.cfg.market_id,
            self.cfg.strategy,
            self.cfg.timeframe,
            self.cfg.environment,
            self.cfg.dry_run,
        )
        if self.cfg.environment == "live" and not self.cfg.dry_run:
            log.warning("LIVE environment: orders will be placed on a real challenge account")

        history = max(300, self.strategy.min_candles + 50)
        stream = MarketDataStream(
            self.cfg.market_stream_url,
            symbols=[coin],
            providers=[provider],
            interval=self.cfg.timeframe,
            history_limit=min(history, 1000),
        )

        async for candle in stream.candles():
            self.series.add(candle)
            if not candle.is_final:
                continue
            if self.state.last_processed_open_time == candle.open_time:
                continue
            try:
                self.on_closed_candle(candle)
            except SystemExit:
                return
            except Exception as exc:  # noqa: BLE001 - one bad candle must not kill the bot
                log.exception("error handling candle %s: %s", candle.open_time, exc)
            self.state.last_processed_open_time = candle.open_time
            save_state(self.cfg.state_file, self.state)
