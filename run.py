#!/usr/bin/env python3
"""polybot CLI — discover markets, run the bot (paper or live), or check setup.

Examples
--------
  python run.py discover                 # list tradable short-term crypto markets
  python run.py run                      # run the bot (paper by default)
  python run.py run --live               # run with REAL funds (requires .env key)
  python run.py setup                    # derive API creds + check balance
"""

from __future__ import annotations

import argparse
import logging
import sys

from polybot.config import load_config
from polybot.engine import Engine
from polybot.gamma import GammaClient
from polybot.logging_setup import setup_logging

log = logging.getLogger("polybot")


def cmd_discover(cfg) -> int:
    gamma = GammaClient(cfg.gamma_host)
    markets = gamma.discover(cfg.symbols, cfg.durations_minutes, cfg.duration_tolerance_seconds)
    if not markets:
        print("No matching short-term crypto markets found right now.")
        return 0
    import time
    now = time.time()
    markets.sort(key=lambda m: m.end_time)
    print(f"{'SYMBOL':6} {'DUR':>4} {'T-LEFT':>7}  QUESTION")
    for m in markets:
        ttl = m.seconds_to_resolution(now)
        print(f"{m.symbol:6} {m.duration_minutes:>3}m {ttl:>6.0f}s  {m.question[:70]}")
    print(f"\n{len(markets)} market(s).")
    return 0


def cmd_setup(cfg) -> int:
    from polybot.clob import ClobGateway

    if not cfg.private_key:
        print("No POLYMARKET_PRIVATE_KEY in .env — set it to enable live trading.")
        return 1
    gw = ClobGateway(cfg)
    gw.connect(require_auth=True)
    print("Authenticated OK.")
    try:
        ba = gw.balance_allowance()
        print(f"Collateral balance/allowance: {ba}")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read balance/allowance: {exc}")
    return 0


def cmd_run(cfg) -> int:
    Engine(cfg).run()
    return 0


def cmd_simulate(cfg, n: int, seed: int) -> int:
    from polybot.simulate import run_simulation

    print(f"Simulating {n} synthetic crypto candles (seed={seed}, strategy={cfg.strategy})...\n")
    res = run_simulation(cfg, n_markets=n, seed=seed)
    print("\n================ SIMULATION SUMMARY ================")
    print(f" markets simulated : {res.n_markets}")
    print(f" entries taken     : {res.entries}  ({res.entries / max(res.n_markets,1):.0%} of markets)")
    print(f" wins / losses     : {res.wins} / {res.losses}   (hit rate {res.hit_rate:.1%})")
    print(f" total notional    : ${res.notional:.2f}")
    print(f" realized PnL      : ${res.pnl:+.2f}   (ROI {res.roi:+.1%})")
    print("===================================================")
    print("\nNote: synthetic data — illustrates the engine, NOT a forward return estimate.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="polybot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "discover", "setup", "simulate", "dashboard"], nargs="?", default="run")
    parser.add_argument("--config", default="config.yaml", help="path to config YAML")
    parser.add_argument("--n", type=int, default=50, help="simulate: number of synthetic markets")
    parser.add_argument("--seed", type=int, default=7, help="simulate/dashboard: RNG seed")
    parser.add_argument("--seconds", type=float, default=60.0, help="dashboard: how long to run")
    parser.add_argument("--live", action="store_true", help="trade with REAL funds")
    parser.add_argument("--dry-run", action="store_true", help="force paper mode")
    parser.add_argument("--log-level", default=None, help="override log level")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.live and args.dry_run:
        print("Choose only one of --live / --dry-run.", file=sys.stderr)
        return 2
    if args.live:
        cfg.dry_run = False
    if args.dry_run:
        cfg.dry_run = True
    if args.log_level:
        cfg.log_level = args.log_level

    setup_logging(cfg.log_level)

    if not cfg.dry_run:
        log.warning("LIVE MODE — real orders will be placed with real funds.")

    if args.command == "dashboard":
        from polybot.dashboard import run_dashboard
        run_dashboard(cfg, seconds=args.seconds, seed=(args.seed if args.seed != 7 else None))
        return 0
    if args.command == "simulate":
        return cmd_simulate(cfg, args.n, args.seed)
    return {
        "run": cmd_run,
        "discover": cmd_discover,
        "setup": cmd_setup,
    }[args.command](cfg)


if __name__ == "__main__":
    raise SystemExit(main())
