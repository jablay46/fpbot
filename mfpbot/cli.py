"""Command-line interface for the MyFundedPerps bot."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from .bot import Bot
from .client import ApiError, MfpClient
from .config import ConfigError, load_config, load_dotenv_file
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
    market = client.get_market(cfg.market_id)
    print(f"Market {cfg.market_id}: coin={market.get('coin')} provider={market.get('provider')} "
          f"max_leverage={market.get('max_leverage')}")
    quote = client.get_quote(cfg.market_id, side="buy", size=float(market.get("min_size") or 1))
    print(f"Quote mid={fmt(quote.get('mid'))} fillable={quote.get('fillable')}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.dry_run:
        cfg.dry_run = True
    _setup_logging(cfg.log_level)
    bot = Bot(cfg)
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
