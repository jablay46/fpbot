"""Multi-asset bot orchestration.

Streams candles for every configured market on one WebSocket connection,
evaluates the strategy per market, and sizes each entry from the account's
current risk snapshot. Portfolio-level daily guards and one position per market
keep exposure controlled.
"""

from __future__ import annotations

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
from .util import fmt

log = logging.getLogger("mfpbot.bot")

TERMINAL_ORDER_STATES = {"filled", "rejected", "canceled", "cancelled", "expired"}
# Assume the exchange could not have moved more than this fraction between the
# last closed candle and the quote; used to skip entries on a stale/broken feed.
MAX_ENTRY_DRIFT = 0.05


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
        self.risk: Optional[RiskManager] = None
        self.markets: dict[str, dict[str, Any]] = {}
        self.series: dict[str, CandleSeries] = {}
        # Per-symbol rotation cursor so one symbol cannot starve the others
        # when several signals fire at the same time.
        self._rotation = 0

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

    def resolve_markets(self) -> dict[str, dict[str, Any]]:
        catalog = {m["market_id"]: m for m in self.client.list_markets()}
        missing = [mid for mid in self.cfg.market_ids if mid not in catalog]
        if missing:
            raise RuntimeError(
                f"market(s) not tradable: {', '.join(missing)}. "
                f"Run 'python -m mfpbot markets' to list valid IDs."
            )
        self.markets = {mid: catalog[mid] for mid in self.cfg.market_ids}
        self.series = {mid: CandleSeries(max_len=1000) for mid in self.cfg.market_ids}
        return self.markets

    def _build_risk_manager(self) -> RiskManager:
        starting = float(self.account.get("starting_balance") or 0.0)
        return RiskManager(
            max_daily_trades=self.cfg.max_daily_trades,
            max_daily_loss_pct=self.cfg.max_daily_loss_pct,
            min_daily_room_pct=self.cfg.min_daily_room_pct,
            starting_balance=starting,
        )

    # -- helpers ----------------------------------------------------------

    def _refresh_account(self) -> dict[str, Any]:
        self.account = self.client.get_account(self.account["id"])
        return self.account.get("risk") or {}

    def _equity(self, account_risk: dict[str, Any]) -> float:
        equity = account_risk.get("equity")
        if equity is None:
            equity = self.account.get("balance") or 0.0
        return float(equity)

    def _open_positions(self) -> list[dict[str, Any]]:
        positions = self.client.list_positions(self.account["id"], status="open")
        return [p for p in positions if p.get("status") == "open"]

    def _position_for(self, positions: list[dict[str, Any]], market_id: str) -> Optional[dict[str, Any]]:
        for pos in positions:
            if pos.get("market_id") == market_id:
                return pos
        return None

    def _mid_price(self, market_id: str, side: str) -> Optional[float]:
        quote = self.client.get_quote(market_id, side=side)
        for key in ("mid", "reference_price", "ask" if side == "buy" else "bid"):
            value = quote.get(key)
            if value:
                return float(value)
        return None

    # -- trading actions --------------------------------------------------

    def _place_entry(
        self, market_id: str, side: str, size: float, entry: float, stop: float, tp: float
    ) -> Optional[dict[str, Any]]:
        client_order_id = f"mfpbot:{market_id}:{uuid.uuid4().hex[:12]}"
        order = {
            "client_order_id": client_order_id,
            "type": "market",
            "account_id": self.account["id"],
            "market_id": market_id,
            "side": side,
            "size": size,
            "expected_price": entry,
            "leverage": self.cfg.leverage,
            "margin_mode": self.cfg.margin_mode,
            "take_profit_price": tp,
            "stop_loss_price": stop,
        }
        if self.cfg.dry_run:
            log.info(
                "[dry-run] %s: would place %s %s @~%s (SL %s, TP %s)",
                market_id, side, fmt(size), fmt(entry), fmt(stop), fmt(tp),
            )
            return {"id": "dry-run", "status": "filled", "client_order_id": client_order_id}

        try:
            created = self.client.place_order(order)
        except ApiError as exc:
            log.error("%s: order rejected: %s (code=%s rule=%s)", market_id, exc, exc.code, exc.details)
            return None

        self.state.last_entry_client_order_id = client_order_id
        return self._await_order(market_id, created)

    def _await_order(self, market_id: str, created: dict[str, Any], timeout: float = 20.0) -> Optional[dict[str, Any]]:
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
                log.warning("%s: could not poll order %s: %s", market_id, order_id, exc)
                continue
            status = current.get("status")
            created = current
        if status == "filled":
            log.info("%s: order filled %s %s", market_id, created.get("side"), fmt(created.get("filled_size")))
            return created
        log.warning("%s: order %s ended in status %s", market_id, order_id, status)
        return None

    def _close_position(self, market_id: str, position: dict[str, Any]) -> bool:
        if self.cfg.dry_run:
            log.info("[dry-run] %s: would close position %s", market_id, position["id"])
            return True
        try:
            self.client.close_position(
                position["id"], client_order_id=f"mfpbot-close:{uuid.uuid4().hex[:12]}"
            )
        except ApiError as exc:
            log.error("%s: failed to close position %s: %s", market_id, position["id"], exc)
            return False
        log.info("%s: close submitted for position %s (%s)", market_id, position["id"], position.get("side"))
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
            time.sleep(2.0)
        log.warning("flatten did not report completion within retry budget")

    # -- per-candle logic -------------------------------------------------

    def on_closed_candle(self, market_id: str, candle: Candle) -> None:
        account_risk = self._refresh_account()
        status = self.account.get("status")
        if status in {"failed", "closed"}:
            log.error("account %s is %s; stopping trading", self.account["id"], status)
            self.state.risk.halted = True
            self.state.risk.halt_reason = f"account {status}"
            save_state(self.cfg.state_file, self.state)
            raise SystemExit(0)

        equity = self._equity(account_risk)
        self.risk.roll_day(self.state.risk, equity)

        positions = self._open_positions()
        position = self._position_for(positions, market_id)
        series = self.series[market_id]
        signal = self.strategy.evaluate(series.closed, self.markets[market_id])

        log.info(
            "%s candle=%s close=%s equity=%s daily_room=%s pos=%s signal=%s",
            market_id, candle.interval, fmt(candle.close), fmt(equity, 2),
            fmt(account_risk.get("daily_loss_room"), 2),
            position.get("side") if position else "flat",
            signal.action if signal else "-",
        )

        if position is not None:
            if signal is not None and self._is_opposite(signal.action, position.get("side")):
                log.info("%s: reversal (%s); closing %s position", market_id, signal.reason, position.get("side"))
                if self._close_position(market_id, position):
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

        self._open_from_signal(market_id, signal, candle, account_risk, equity, positions)

    @staticmethod
    def _is_opposite(action: str, position_side: Optional[str]) -> bool:
        if position_side is None:
            return False
        return (action == "long" and position_side == "short") or (
            action == "short" and position_side == "long"
        )

    def _open_from_signal(
        self,
        market_id: str,
        signal,
        candle: Candle,
        account_risk: dict,
        equity: float,
        positions: list[dict[str, Any]],
    ) -> None:
        market = self.markets[market_id]
        side = "buy" if signal.action == "long" else "sell"
        entry = self._mid_price(market_id, side)
        if entry is None:
            log.warning("%s: no quote available; skipping entry", market_id)
            return
        if signal.stop_price is None:
            log.warning("%s: signal has no stop price; skipping entry", market_id)
            return
        if abs(entry - candle.close) / candle.close > MAX_ENTRY_DRIFT:
            log.warning(
                "%s: quote %s is more than %.0f%% away from candle close %s; skipping entry",
                market_id, fmt(entry), MAX_ENTRY_DRIFT * 100, fmt(candle.close),
            )
            return

        # Re-anchor the ATR stop distance to the actual entry price.
        distance = abs(candle.close - signal.stop_price)
        if distance <= 0:
            log.warning("%s: signal has no usable stop distance; skipping entry", market_id)
            return
        if signal.action == "long":
            stop = entry - distance
            tp = entry + distance * self.cfg.take_profit_rr
        else:
            stop = entry + distance
            tp = entry - distance * self.cfg.take_profit_rr

        # Margin is reserved across the whole account, so account for positions
        # already open in other markets when sizing a new one.
        other_margin = sum(
            float(p.get("size") or 0.0) * float(p.get("entry_price") or 0.0) / float(p.get("leverage") or 1.0)
            for p in positions
            if p.get("market_id") != market_id
        )
        available = account_risk.get("available_balance")
        if available is not None:
            available = max(0.0, float(available) - other_margin)

        sizer = PositionSizer(
            risk_per_trade_pct=self.cfg.risk_per_trade_pct,
            leverage=self.cfg.leverage,
            min_notional=float(market.get("min_notional") or 0.0),
            min_size=float(market.get("min_size") or 0.0),
            size_step=float(market.get("size_step") or 0.0),
            max_leverage=float(market.get("max_leverage") or self.cfg.leverage),
            available_balance=available,
        )
        sizing = sizer.size(equity=equity, entry=entry, stop=stop)
        if not sizing.ok:
            log.warning("%s: skipping entry: %s", market_id, sizing.reason)
            return

        log.info(
            "%s: entry %s size=%s entry=%s stop=%s tp=%s risk=%s margin=%s (%s)",
            market_id, side, fmt(sizing.size), fmt(entry), fmt(stop), fmt(tp),
            fmt(sizing.risk_amount, 2), fmt(sizing.required_margin, 2), signal.reason,
        )
        filled = self._place_entry(market_id, side, sizing.size, entry, stop, tp)
        if filled is not None:
            self.risk.record_entry(self.state.risk)
            save_state(self.cfg.state_file, self.state)

    # -- run loop ---------------------------------------------------------

    def _ordered_markets(self) -> list[str]:
        ids = list(self.cfg.market_ids)
        if not ids:
            return ids
        self._rotation %= len(ids)
        return ids[self._rotation:] + ids[: self._rotation]

    async def run(self) -> None:
        self.resolve_account()
        self.resolve_markets()
        self.risk = self._build_risk_manager()

        log.info(
            "starting multi-asset bot: account=%s (%s) markets=%d strategy=%s timeframe=%s env=%s dry_run=%s",
            self.account.get("name") or self.account["id"],
            self.account.get("status"),
            len(self.markets),
            self.cfg.strategy,
            self.cfg.timeframe,
            self.cfg.environment,
            self.cfg.dry_run,
        )
        for mid in self.cfg.market_ids:
            log.info("  market: %s", mid)
        if self.cfg.environment == "live" and not self.cfg.dry_run:
            log.warning("LIVE environment: orders will be placed on a real challenge account")

        history = min(max(300, self.strategy.min_candles + 50), 1000)
        stream = MarketDataStream.for_markets(
            self.cfg.market_stream_url,
            list(self.markets.values()),
            interval=self.cfg.timeframe,
            history_limit=history,
        )

        async for candle in stream.candles():
            market_id = self._market_id_for(candle)
            if market_id is None:
                continue
            self.series[market_id].add(candle)
            if not candle.is_final:
                continue
            if self.state.last_processed_open_time.get(market_id) == candle.open_time:
                continue
            try:
                self.on_closed_candle(market_id, candle)
            except SystemExit:
                return
            except Exception as exc:  # noqa: BLE001 - one bad candle must not kill the bot
                log.exception("%s: error handling candle %s: %s", market_id, candle.open_time, exc)
            self.state.last_processed_open_time[market_id] = candle.open_time
            self._rotation += 1
            save_state(self.cfg.state_file, self.state)

    def _market_id_for(self, candle: Candle) -> Optional[str]:
        """Map a streamed candle (provider + venue symbol) back to a market ID."""
        for mid, market in self.markets.items():
            if market["coin"] == candle.symbol and market["provider"] == candle.provider:
                return mid
        return None
