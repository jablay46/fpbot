"""Command-line interface for the MyFundedPerps bot."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from .bot import Bot
from .client import ApiError, MfpClient
from .config import ConfigError, load_config, load_dotenv_file
from .state import StateLoadError, load_state, save_state
from .util import fmt


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_markets(args: argparse.Namespace) -> int:
    cfg = load_config(require_key=False)
    client = MfpClient("", cfg.base_url)
    markets = client.list_markets()
    needle = (args.filter or "").upper()
    rows = [m for m in markets if not needle or needle in m["market_id"].upper()]
    for m in sorted(rows, key=lambda x: x["market_id"]):
        print(
            f"{m['market_id']:<28} lev<={m.get('max_leverage'):<4} "
            f"step={m.get('size_step'):<10} min_notional={m.get('min_notional')}"
        )
    print(f"\n{len(rows)} market(s)")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    cfg = load_config()
    client = MfpClient(cfg.api_key, cfg.base_url)
    accounts = client.list_accounts()
    for a in accounts:
        print(f"{a['id']}  {a.get('name'):<24} {a.get('stage'):<10} {a.get('status'):<8} balance={fmt(a.get('balance'), 2)}")
    print(f"\n{len(accounts)} account(s)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config()
    client = MfpClient(cfg.api_key, cfg.base_url)
    info = client.get_api_info()
    print(f"API: {info.get('name')} {info.get('version')} ({info.get('environment')})")
    accounts = client.list_accounts()
    print(f"Authenticated. {len(accounts)} account(s) accessible.")
    catalog = {m["market_id"]: m for m in client.list_markets()}
    print(f"Configured markets ({len(cfg.market_ids)}):")
    for mid in cfg.market_ids:
        market = catalog.get(mid)
        if market is None:
            print(f"  {mid}: NOT TRADABLE")
            continue
        quote = client.get_quote(mid)
        print(
            f"  {mid}: provider={market.get('provider')} max_leverage={market.get('max_leverage')} "
            f"mid={fmt(quote.get('mid'))}"
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.dry_run:
        cfg.dry_run = True
    _setup_logging(cfg.log_level)
    try:
        bot = Bot(cfg)
    except StateLoadError as exc:
        # Refuse to start a live run from empty state: that would silently drop
        # owned positions, pending entries and daily counters.
        print(f"state error: {exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nstopped by user")
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    cfg = load_config()
    client = MfpClient(cfg.api_key, cfg.base_url)
    account_id = cfg.account_id
    if not account_id:
        accounts = client.list_accounts()
        active = [a for a in accounts if a.get("status") == "active"]
        account_id = (active or accounts)[0]["id"]
    positions = client.list_positions(account_id, status="open")
    if not positions:
        print("no open positions")
        return 0
    for p in positions:
        print(
            f"{p.get('market_id'):<28} {p.get('side'):<6} size={fmt(p.get('size'))} "
            f"entry={fmt(p.get('entry_price'))} lev={p.get('leverage')} "
            f"liq={fmt(p.get('liquidation_price'))}"
        )
    return 0


def cmd_reset_halt(args: argparse.Namespace) -> int:
    """Clear the persistent halts on the configured state file.

    A cumulative-drawdown halt (and a daily halt) is intentionally sticky, so
    resuming requires an explicit, acknowledged operator action.
    """
    cfg = load_config(require_key=False)
    path = cfg.state_path
    if not args.yes:
        print(
            f"refusing to reset halts in {path!r} without --yes.\n"
            "This clears the cumulative-drawdown halt and the daily halt so the bot "
            "resumes trading on this account. Re-run as:\n"
            "  python -m mfpbot reset-halt --yes",
            file=sys.stderr,
        )
        return 2
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < 60:
            print(
                f"warning: {path!r} was modified {age:.0f}s ago; the bot may be "
                "running. Stop it first, or this reset may be overwritten.",
                file=sys.stderr,
            )
    state = load_state(path)
    state.risk.halted = False
    state.risk.halt_reason = ""
    state.risk.total_drawdown_halted = False
    state.risk.total_drawdown_reason = ""
    save_state(path, state)
    print(f"cleared halts in {path}; the bot will trade again on restart")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    """Backtest one strategy (or compare several) over local candle files."""
    from .backtest import BacktestConfig, CostModel, Backtester, load_many, walk_forward
    from .strategy import STRATEGIES, build_strategy

    cfg = load_config(require_key=False)
    bars = load_many(args.bars)
    if not bars:
        print("no bars loaded", file=sys.stderr)
        return 2

    costs = CostModel(
        commission_pct=args.commission_pct,
        swap_daily_pct=args.swap_daily_pct,
        slippage_bps=args.slippage_bps,
        apply_costs=not args.no_costs,
    )
    bt_config = BacktestConfig(
        starting_equity=args.equity,
        risk_per_trade_pct=args.risk_pct,
        leverage=args.leverage,
        max_margin_pct=cfg.max_margin_pct,
        max_daily_loss_pct=args.max_daily_loss_pct,
        max_total_drawdown_pct=args.max_total_drawdown_pct,
        drawdown_basis=args.drawdown_basis,
        max_daily_trades=args.max_daily_trades,
        costs=costs,
    )

    overrides = {
        "fast": cfg.ema_fast, "slow": cfg.ema_slow, "atr_period": cfg.atr_period,
        "atr_stop_mult": cfg.atr_stop_mult, "take_profit_rr": cfg.take_profit_rr,
        "donchian_period": cfg.donchian_period, "regime_adx_min": cfg.regime_adx_min,
        "trend_ema": cfg.trend_ema, "supertrend_period": cfg.supertrend_period,
        "supertrend_mult": cfg.supertrend_mult,
    }

    names = args.strategy or list(STRATEGIES)
    for unknown in [n for n in names if n not in STRATEGIES]:
        print(f"unknown strategy {unknown!r}; available: {sorted(STRATEGIES)}", file=sys.stderr)
        return 2

    print(f"{len(bars)} bar(s), {bars[0].open_time}..{bars[-1].open_time}, "
          f"costs={'on' if not args.no_costs else 'off'}\n")
    header = f"{'strategy':<20}{'trades':>7}{'win%':>7}{'ret%':>9}{'maxDD%':>8}{'MAR':>7}{'PF':>7}{'exp$':>9}"
    print(header)
    print("-" * len(header))
    for name in names:
        factory = lambda n=name: build_strategy(n, **overrides)
        result = Backtester(factory(), bt_config, market={"market_id": args.market_id}).run(bars)
        m = result.metrics
        print(f"{name:<20}{m.trades:>7}{m.win_rate:>7.1f}{m.total_return_pct:>9.2f}"
              f"{m.max_drawdown_pct:>8.2f}{m.mar:>7.2f}{m.profit_factor:>7.2f}{m.expectancy:>9.2f}")

    if args.walk_forward > 1:
        print(f"\nwalk-forward ({args.walk_forward} folds), per strategy:")
        for name in names:
            factory = lambda n=name: build_strategy(n, **overrides)
            wf = walk_forward(factory, bars, bt_config, folds=args.walk_forward,
                              market={"market_id": args.market_id})
            print(f"  {name:<20} profitable_folds={wf.profitable_fraction:.0%} "
                  f"median_ret={wf.median_return_pct:+.2f}% worstDD={wf.worst_drawdown_pct:.2f}%")
            for fold in wf.folds:
                fm = fold.result.metrics
                print(f"      fold {fold.index}: ret={fm.total_return_pct:+.2f}% "
                      f"dd={fm.max_drawdown_pct:.2f}% trades={fm.trades}")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """Record closed candles from the public market stream to a JSONL file."""
    from .archiver import run_archive
    from .market_stream import MarketDataStream

    cfg = load_config(require_key=False)
    markets = [_parse_market_id(s) for s in args.symbols]
    stream = MarketDataStream.for_markets(
        cfg.market_stream_url, markets, interval=args.timeframe, history_limit=args.history_limit
    )
    try:
        written = asyncio.run(run_archive(stream, args.out, max_candles=args.max_candles))
    except KeyboardInterrupt:
        written = 0
    print(f"archived {written} new bar(s) to {args.out}")
    return 0


def _parse_market_id(value: str) -> dict:
    if "|" not in value:
        raise ConfigError(f"market {value!r} must look like 'provider|COIN'")
    provider, coin = value.split("|", 1)
    return {"market_id": value, "provider": provider, "coin": coin}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mfpbot", description="MyFundedPerps trading bot")
    parser.add_argument("--config-file", help="Optional JSON/YAML config file")
    sub = parser.add_subparsers(dest="command", required=True)

    p_markets = sub.add_parser("markets", help="List tradable markets (public)")
    p_markets.add_argument("--filter", help="Substring to filter market IDs")
    p_markets.set_defaults(func=cmd_markets)

    p_accounts = sub.add_parser("accounts", help="List accounts accessible with the API key")
    p_accounts.set_defaults(func=cmd_accounts)

    p_check = sub.add_parser("check", help="Verify credentials, account, market and quote")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="Run the trading bot")
    p_run.add_argument("--dry-run", action="store_true", help="Log intended orders without placing them")
    p_run.set_defaults(func=cmd_run)

    p_pos = sub.add_parser("positions", help="List open positions")
    p_pos.set_defaults(func=cmd_positions)

    p_reset = sub.add_parser("reset-halt", help="Clear a persistent halt in the state file")
    p_reset.add_argument("--yes", action="store_true", help="Confirm the halt reset")
    p_reset.set_defaults(func=cmd_reset_halt)

    p_bt = sub.add_parser("backtest", help="Backtest strategies over local candle files")
    p_bt.add_argument("--bars", nargs="+", required=True, help="JSONL/CSV candle files")
    p_bt.add_argument("--strategy", nargs="+", help="Strategy name(s); default: all")
    p_bt.add_argument("--market-id", default="binance|BTCUSDT", help="Market id used for metadata")
    p_bt.add_argument("--equity", type=float, default=100_000.0)
    p_bt.add_argument("--risk-pct", type=float, default=1.0, help="Risk per trade, percent of equity")
    p_bt.add_argument("--leverage", type=float, default=2.0)
    p_bt.add_argument("--max-daily-loss-pct", type=float, default=0.0)
    p_bt.add_argument("--max-total-drawdown-pct", type=float, default=0.0)
    p_bt.add_argument("--drawdown-basis", default="starting", choices=["starting", "peak"])
    p_bt.add_argument("--max-daily-trades", type=int, default=0)
    p_bt.add_argument("--commission-pct", type=float, default=0.03)
    p_bt.add_argument("--swap-daily-pct", type=float, default=0.03)
    p_bt.add_argument("--slippage-bps", type=float, default=1.2)
    p_bt.add_argument("--no-costs", action="store_true", help="Ignore fees/slippage/swap (gross)")
    p_bt.add_argument("--walk-forward", type=int, default=0, help="Number of folds (>1 to enable)")
    p_bt.set_defaults(func=cmd_backtest)

    p_ar = sub.add_parser("archive", help="Record live candles to a JSONL file")
    p_ar.add_argument("--symbols", nargs="+", required=True, help="market ids like binance|BTCUSDT")
    p_ar.add_argument("--timeframe", default="15m")
    p_ar.add_argument("--history-limit", type=int, default=300)
    p_ar.add_argument("--out", required=True, help="Output JSONL file")
    p_ar.add_argument("--max-candles", type=int, help="Stop after this many bars (for testing)")
    p_ar.set_defaults(func=cmd_archive)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv_file(".env")
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"API error: {exc} (code={exc.code}, request_id={exc.request_id})", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
