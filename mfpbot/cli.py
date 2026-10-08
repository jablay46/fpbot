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
