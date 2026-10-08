"""Multi-asset bot orchestration.

Streams candles for every configured market on one WebSocket connection,
evaluates the strategy per market, and sizes each entry from the account's
current risk snapshot. Portfolio-level daily guards and one position per market
keep exposure controlled.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Any, Optional

from .client import ApiError, MfpClient
from .config import Config
from .market_stream import Candle, CandleSeries, MarketDataStream
from .risk.manager import RiskManager, RiskState
from .risk.sizing import PositionSizer
from .state import BotState, PendingEntry, load_state, save_state
from .strategy import build_strategy
from .util import fmt, parse_interval_ms

log = logging.getLogger("mfpbot.bot")

TERMINAL_ORDER_STATES = {"filled", "rejected", "canceled", "cancelled", "expired"}
# Assume the exchange could not have moved more than this fraction between the
# last closed candle and the quote; used to skip entries on a stale/broken feed.
MAX_ENTRY_DRIFT = 0.05
# A candle is only acted on if its close is within this many intervals of now;
# older bars are history/backfill and must never trigger a live order.
FRESHNESS_INTERVALS = 1.5
# Retry delays (seconds) when a fresh candle hits a transient REST error.
FRESH_CANDLE_RETRY_DELAYS = (2.0, 5.0)


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
        self.state = state or load_state(config.state_path)
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
        # Injectable wall clock (seconds) so tests can control candle freshness.
        self.clock = time.time
        self._interval_ms = parse_interval_ms(config.timeframe)
        # Serializes account/position/state mutation between the candle worker
        # thread (P6 offloads the blocking REST work) and the poll watchdog.
        # Reentrant so a locked section may call helpers that also lock.
        self._lock = threading.RLock()

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
            missing_room_policy=self.cfg.missing_room_policy,
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
        idempotency_key = self.client.new_idempotency_key()
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

        # Record the intent (and the positions we must not mistake for ours)
        # before sending, so a lost reply or crash can be reconciled later.
        pending = PendingEntry(
            market_id=market_id,
            client_order_id=client_order_id,
            idempotency_key=idempotency_key,
            sent_at=self.clock(),
            pre_position_ids=self._market_position_ids(market_id),
        )
        self.state.pending_entry = pending
        self.state.last_entry_client_order_id = client_order_id
        save_state(self.cfg.state_path, self.state)

        try:
            created = self.client.place_order(order, idempotency_key=idempotency_key)
        except ApiError as exc:
            return self._recover_lost_order(market_id, pending, exc)

        return self._finish_entry(market_id, created, pending)

    def _recover_lost_order(
        self, market_id: str, pending: PendingEntry, exc: ApiError
    ) -> Optional[dict[str, Any]]:
        """Resolve an order whose reply was lost instead of assuming it failed."""
        log.warning("%s: order response lost (%s); reconciling by client_order_id", market_id, exc)
        try:
            found = self.client.find_order_by_client_id(pending.client_order_id)
        except ApiError as lookup_exc:
            log.error(
                "%s: cannot confirm order %s (%s); blocking new entries in this market",
                market_id, pending.client_order_id, lookup_exc,
            )
            # Keep pending_entry so the next candle / watchdog retries the lookup.
            save_state(self.cfg.state_path, self.state)
            return None
        if found is None:
            log.warning("%s: order %s was not accepted; treating as not sent", market_id, pending.client_order_id)
            self.state.pending_entry = None
            save_state(self.cfg.state_path, self.state)
            return None
        log.info("%s: reconciled order %s after a lost reply", market_id, pending.client_order_id)
        return self._finish_entry(market_id, found, pending)

    def _finish_entry(
        self, market_id: str, created: dict[str, Any], pending: PendingEntry
    ) -> Optional[dict[str, Any]]:
        """Await the fill, adopt the position, and clear the pending marker."""
        filled = self._await_order(market_id, created)
        if filled is not None:
            self._adopt_position(market_id, pending.pre_position_ids)
        self.state.pending_entry = None
        save_state(self.cfg.state_path, self.state)
        return filled

    def _reconcile_pending_entry(self) -> bool:
        """Resolve an outstanding entry from a previous send. Returns True if busy.

        Blocks new entries in that market until the outcome is known so a lost
        order can never be duplicated.
        """
        pending = self.state.pending_entry
        if pending is None:
            return False
        try:
            found = self.client.find_order_by_client_id(pending.client_order_id)
        except ApiError as exc:
            log.warning(
                "%s: still cannot confirm pending entry %s (%s); skipping new entries",
                pending.market_id, pending.client_order_id, exc,
            )
            return True
        if found is None:
            log.info("%s: pending entry %s was never accepted; clearing", pending.market_id, pending.client_order_id)
            self.state.pending_entry = None
            save_state(self.cfg.state_path, self.state)
            return False
        log.info("%s: resolving pending entry %s", pending.market_id, pending.client_order_id)
        filled = self._finish_entry(pending.market_id, found, pending)
        if filled is not None:
            self.risk.record_entry(self.state.risk)
            save_state(self.cfg.state_path, self.state)
        return True

    def _market_position_ids(self, market_id: str) -> list[str]:
        """IDs of currently open positions in one market (pre-entry snapshot)."""
        try:
            return [p["id"] for p in self._open_positions() if p.get("market_id") == market_id]
        except ApiError as exc:
            log.warning("%s: could not snapshot positions before entry: %s", market_id, exc)
            return []

    def _adopt_position(
        self, market_id: str, pre_position_ids: Optional[list[str]] = None, timeout: float = 8.0
    ) -> None:
        """Link the position our fill created so the bot manages it.

        Entry orders do not return a position_id, so match the freshly opened
        position on this market. Only positions that did not exist before the
        order are ours, which keeps a lagging closed position (reversal) or a
        manual position from being mis-adopted.
        """
        pre = set(pre_position_ids or [])
        deadline = time.monotonic() + timeout
        delay = 0.5
        while time.monotonic() < deadline:
            try:
                positions = self._open_positions()
            except ApiError as exc:
                log.warning("%s: could not adopt position: %s", market_id, exc)
                return
            for pos in positions:
                if (
                    pos.get("market_id") == market_id
                    and pos["id"] not in pre
                    and pos["id"] not in self.state.owned_position_ids
                ):
                    self._remember_position(pos["id"])
                    log.info("%s: adopted position %s (%s)", market_id, pos["id"], pos.get("side"))
                    save_state(self.cfg.state_path, self.state)
                    return
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        log.warning("%s: could not find the position from our fill; it still has broker-side SL/TP", market_id)

    def _remember_position(self, position_id: str) -> None:
        if position_id not in self.state.owned_position_ids:
            self.state.owned_position_ids.append(position_id)

    def _forget_position(self, position_id: str) -> None:
        if position_id in self.state.owned_position_ids:
            self.state.owned_position_ids.remove(position_id)

    def _owned_positions(self, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        owned = set(self.state.owned_position_ids)
        return [p for p in positions if p["id"] in owned]

    def _prune_owned(self, positions: list[dict[str, Any]]) -> None:
        """Drop tracked IDs for positions that no longer exist (TP/SL/manual close)."""
        live = {p["id"] for p in positions}
        self.state.owned_position_ids = [pid for pid in self.state.owned_position_ids if pid in live]

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

    def _wait_position_gone(self, position_id: str, timeout: float = 5.0) -> bool:
        """Poll until a closed position disappears from the open list."""
        deadline = time.monotonic() + timeout
        delay = 0.5
        while time.monotonic() < deadline:
            try:
                if not any(p["id"] == position_id for p in self._open_positions()):
                    return True
            except ApiError as exc:
                log.warning("could not confirm position %s is closed: %s", position_id, exc)
                return False
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        return False

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
        self._forget_position(position["id"])
        return True

    def _flatten(self) -> None:
        if self.cfg.dry_run:
            log.info("[dry-run] would flatten bot positions for account %s", self.account["id"])
            return
        if self.cfg.flatten_scope == "account":
            self._flatten_account()
            return
        positions = self._owned_positions(self._open_positions())
        if not positions:
            log.info("flatten: no bot-owned positions to close")
            return
        log.warning("flatten: closing %d bot-owned position(s)", len(positions))
        for position in positions:
            self._close_position(position.get("market_id"), position)

    def _flatten_account(self) -> None:
        key = self.client.new_idempotency_key()
        for _ in range(20):
            try:
                result = self.client.close_all_positions(self.account["id"], idempotency_key=key)
            except ApiError as exc:
                log.error("flatten failed: %s", exc)
                return
            if not isinstance(result, dict) or result.get("status") == "completed":
                log.info("flatten completed")
                self.state.owned_position_ids = []
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
            save_state(self.cfg.state_path, self.state)
            raise SystemExit(0)

        equity = self._equity(account_risk)
        self.risk.roll_day(self.state.risk, equity)

        # The account kill switch must run before the position branch: a
        # bot-owned position must never stop the daily loss / room floor guard
        # from flattening and halting.
        kill = self.risk.check_kill(self.state.risk, equity, account_risk)
        if not kill.allowed:
            log.error("kill switch: %s", kill.reason)
            if kill.flatten:
                self._flatten()
                self.risk.halt(self.state.risk, kill.reason)
                save_state(self.cfg.state_path, self.state)
            return

        # Resolve any entry left unconfirmed by a lost reply before acting.
        self._reconcile_pending_entry()

        positions = self._open_positions()
        self._prune_owned(positions)
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
            if position["id"] not in self.state.owned_position_ids:
                # Never touch positions this bot did not open.
                return
            if signal is not None and self._is_opposite(signal.action, position.get("side")):
                log.info("%s: reversal (%s); closing %s position", market_id, signal.reason, position.get("side"))
                if not self._close_position(market_id, position):
                    log.warning("%s: could not close %s; skipping reversal", market_id, position.get("id"))
                    return
                # Wait for the old position to leave the book so the new one can
                # be adopted unambiguously; otherwise skip rather than stack.
                if not self._wait_position_gone(position["id"]):
                    log.warning(
                        "%s: position %s still open after close; skipping reversal entry",
                        market_id, position["id"],
                    )
                    return
                position = None
            else:
                return

        decision = self.risk.can_enter(self.state.risk, equity, account_risk)
        if not decision.allowed:
            log.warning("no new entry: %s", decision.reason)
            return

        if signal is None:
            return

        if self.state.pending_entry is not None and self.state.pending_entry.market_id == market_id:
            log.warning("%s: an unconfirmed entry is still pending; not opening another", market_id)
            return

        if self._margin_headroom(positions, equity) <= 0:
            log.warning("%s: portfolio margin cap reached; skipping entry", market_id)
            return

        self._open_from_signal(market_id, signal, candle, account_risk, equity, positions)

    def _position_margin(self, position: dict[str, Any]) -> float:
        return (
            float(position.get("size") or 0.0)
            * float(position.get("entry_price") or 0.0)
            / float(position.get("leverage") or 1.0)
        )

    def _margin_headroom(self, positions: list[dict[str, Any]], equity: float) -> float:
        """Remaining margin budget before the portfolio cap is hit."""
        cap = equity * self.cfg.max_margin_pct / 100.0
        used = sum(self._position_margin(p) for p in positions)
        return cap - used

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

        # Re-anchor the ATR stop distance to the actual entry price.
        distance = abs(candle.close - signal.stop_price)
        if distance <= 0:
            log.warning("%s: signal has no usable stop distance; skipping entry", market_id)
            return

        # The quote must not have drifted far from the close that produced the
        # signal: cap by a fraction of ATR, and by an absolute ceiling.
        atr = distance / self.cfg.atr_stop_mult if self.cfg.atr_stop_mult else 0.0
        allowed_drift = min(MAX_ENTRY_DRIFT * candle.close, self.cfg.max_entry_drift_atr * atr)
        if abs(entry - candle.close) > allowed_drift:
            log.warning(
                "%s: quote %s drifted %s from candle close %s (allowed %s); skipping entry",
                market_id, fmt(entry), fmt(abs(entry - candle.close)),
                fmt(candle.close), fmt(allowed_drift),
            )
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
            self._position_margin(p) for p in positions if p.get("market_id") != market_id
        )
        available = account_risk.get("available_balance")
        if available is not None:
            available = max(0.0, float(available) - other_margin)
        # Never exceed the portfolio margin budget.
        headroom = self._margin_headroom(positions, equity)
        if available is not None:
            available = min(available, max(0.0, headroom))
        else:
            available = max(0.0, headroom)

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
            save_state(self.cfg.state_path, self.state)

    # -- kill-switch watchdog ---------------------------------------------

    def check_kill_now(self) -> bool:
        """Run the account kill switch once, independently of candles.

        Returns True when the kill switch fired (flatten + halt). Safe to call
        from any thread; state mutation is serialized by ``self._lock``.
        """
        with self._lock:
            if self.state.risk.halted:
                return True
            self._reconcile_pending_entry()
            account_risk = self._refresh_account()
            equity = self._equity(account_risk)
            self.risk.roll_day(self.state.risk, equity)
            kill = self.risk.check_kill(self.state.risk, equity, account_risk)
            if kill.allowed:
                return False
            log.error("watchdog kill switch: %s", kill.reason)
            if kill.flatten:
                self._flatten()
                self.risk.halt(self.state.risk, kill.reason)
                save_state(self.cfg.state_path, self.state)
            return True

    async def _watchdog(self) -> None:
        """Poll the account between candles so guards fire even with no signal."""
        interval = max(self.cfg.poll_seconds, 0.05)
        while True:
            await asyncio.sleep(interval)
            try:
                fired = await asyncio.to_thread(self.check_kill_now)
            except Exception as exc:  # noqa: BLE001 - watchdog must not die
                log.warning("watchdog check failed: %s", exc)
                continue
            if fired:
                log.warning("watchdog: kill switch active; stopping checks")
                return

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
        # A restart may leave an entry unconfirmed; resolve it before trading.
        self._reconcile_pending_entry()

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

        watchdog = asyncio.create_task(self._watchdog())
        try:
            async for candle in stream.candles():
                market_id = self._market_id_for(candle)
                if market_id is None:
                    continue
                try:
                    # REST calls and backoff sleeps block; run them off the event
                    # loop so the WebSocket pump and heartbeat keep flowing.
                    await asyncio.to_thread(self._process_candle, market_id, candle)
                except SystemExit:
                    return
                except Exception as exc:  # noqa: BLE001 - one bad candle must not kill the bot
                    log.exception("%s: error handling candle %s: %s", market_id, candle.open_time, exc)
        finally:
            watchdog.cancel()

    def _process_candle(self, market_id: str, candle: Candle) -> None:
        """Route one streamed candle to the series and, if fresh, the strategy.

        Only a recent candle is a live signal source. Older bars (the history
        snapshot replayed on every start/reconnect) are appended to the series
        and advance the cursor, but they never make REST calls or place orders —
        otherwise backfilled crossovers would trade on startup.
        """
        with self._lock:
            self.series[market_id].add(candle)
            if not candle.is_final:
                return
            last = self.state.last_processed_open_time.get(market_id)
            if last is not None and candle.open_time <= last:
                return
            if not self._is_fresh(candle):
                self.state.last_processed_open_time[market_id] = candle.open_time
                return
        self._handle_fresh_candle(market_id, candle)

    def _handle_fresh_candle(self, market_id: str, candle: Candle) -> None:
        """Handle one fresh candle, retrying transient REST errors.

        The processed cursor advances only after the candle is handled, so a
        failure leaves it eligible for a retry instead of being silently dropped.
        """
        delays = list(FRESH_CANDLE_RETRY_DELAYS)
        attempt = 0
        while True:
            try:
                with self._lock:
                    self.on_closed_candle(market_id, candle)
                    self.state.last_processed_open_time[market_id] = candle.open_time
                    self._rotation += 1
                    save_state(self.cfg.state_path, self.state)
                return
            except ApiError as exc:
                if attempt >= len(delays):
                    log.error(
                        "%s: giving up on candle %s after %d attempts: %s",
                        market_id, candle.open_time, attempt + 1, exc,
                    )
                    return
                delay = delays[attempt]
                attempt += 1
                log.warning(
                    "%s: transient error on candle %s (%s); retrying in %.0fs",
                    market_id, candle.open_time, exc, delay,
                )
                time.sleep(delay)

    def _is_fresh(self, candle: Candle) -> bool:
        """True when the candle closed recently enough to act on."""
        interval_ms = parse_interval_ms(candle.interval) or self._interval_ms
        if interval_ms <= 0:
            return False
        now_ms = int(self.clock() * 1000)
        return (now_ms - candle.close_time) <= FRESHNESS_INTERVALS * interval_ms

    def _market_id_for(self, candle: Candle) -> Optional[str]:
        """Map a streamed candle (provider + venue symbol) back to a market ID."""
        for mid, market in self.markets.items():
            if market["coin"] == candle.symbol and market["provider"] == candle.provider:
                return mid
        return None
