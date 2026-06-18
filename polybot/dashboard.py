"""Live trading dashboard.

Runs the real engine (strategy -> risk -> paper executor -> settlement) and
streams a live feed of decisions plus a refreshing stats panel with an ASCII
equity curve, so you can watch the bot operate in real time.

In this offline sandbox it is fed by the market simulator (no internet), but it
exercises exactly the same strategy/risk/execution code that runs live. On a
networked machine, `python run.py run` drives the identical logic against real
Polymarket markets.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .executor import PaperExecutor
from .models import CryptoMarket, Quote, Side
from .risk import RiskManager
from .strategy import build_strategy, fair_up_probability

SECONDS_PER_YEAR = 365 * 24 * 3600
_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(values: List[float], width: int = 48) -> str:
    if not values:
        return ""
    vals = values[-width:]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return _SPARK[3] * len(vals)
    out = []
    for v in vals:
        idx = int((v - lo) / (hi - lo) * (len(_SPARK) - 1))
        out.append(_SPARK[idx])
    return "".join(out)


@dataclass
class DashState:
    bankroll: float
    equity: float
    peak: float
    markets: int = 0
    entries: int = 0
    wins: int = 0
    losses: int = 0
    realized: float = 0.0
    equity_curve: List[float] = field(default_factory=list)


def _panel(st: DashState, started: float) -> str:
    elapsed = time.time() - started
    n_settled = st.wins + st.losses
    hit = (st.wins / n_settled * 100) if n_settled else 0.0
    dd = (st.equity - st.peak)
    roi = (st.equity / st.bankroll - 1) * 100
    bar = _sparkline(st.equity_curve)
    return "\n".join([
        "┌" + "─" * 66 + "┐",
        f"│ POLYBOT LIVE DASHBOARD   (simulated feed — sandbox is offline)     │",
        "├" + "─" * 66 + "┤",
        f"│ uptime {elapsed:6.0f}s   markets {st.markets:<5} entries {st.entries:<5}        │",
        f"│ wins {st.wins:<4} losses {st.losses:<4} hit-rate {hit:5.1f}%                    │",
        f"│ equity ${st.equity:8.2f}   realized PnL ${st.realized:+8.2f} (ROI {roi:+5.1f}%) │",
        f"│ peak ${st.peak:8.2f}   drawdown ${dd:+8.2f}                       │",
        f"│ equity ▏{bar:<48}▏ │",
        "└" + "─" * 66 + "┘",
    ])


def _play_market(
    cfg: Config, strategy, risk: RiskManager, executor: PaperExecutor,
    st: DashState, rng: random.Random, duration_min: int, annual_vol: float,
    market_lag: float, spread: float, step_sleep: float,
) -> None:
    open_px = 100_000.0
    px = open_px
    vol_per_sec = annual_vol / math.sqrt(SECONDS_PER_YEAR)
    dt = max(1.0, cfg.poll_interval_seconds)
    duration = duration_min * 60
    mkt_id = rng.randrange(1_000_000)
    market = CryptoMarket(
        condition_id=f"sim-{mkt_id}", question=f"BTC {duration_min}m up or down",
        slug=f"btc-{duration_min}m-{mkt_id}", symbol="BTC",
        up_token_id="UP", down_token_id="DOWN",
        start_time=0.0, end_time=float(duration), tick_size=0.01, min_size=5.0,
    )

    # Each market is independent: reset per-market counters + session halts so
    # the live demo keeps trading. (Live runs keep kill-switches armed.)
    risk.trades_per_market.clear()
    risk.consecutive_losses = 0
    risk.realized_pnl_today = 0.0
    risk.open_positions = [p for p in risk.open_positions if p.open]

    st.markets += 1
    print(f"\n▶ market #{st.markets:<4} BTC {duration_min}m  open=${open_px:,.0f}  "
          f"scanning for edge…", flush=True)

    market_prob = 0.5
    t = 0.0
    position = None
    traded = False

    while t < duration:
        z = rng.gauss(0.0, 1.0)
        px *= math.exp(vol_per_sec * math.sqrt(dt) * z)
        t += dt
        sec_left = duration - t
        fair_up = fair_up_probability(px, open_px, max(0.0, sec_left), vol_per_sec)
        market_prob += (fair_up - market_prob) * market_lag + rng.gauss(0.0, 0.008)
        market_prob = min(0.99, max(0.01, market_prob))

        up_q = Quote("UP", best_bid=max(0.01, market_prob - spread / 2),
                     best_ask=min(0.99, market_prob + spread / 2), ask_size=1000, bid_size=1000)
        dn_q = Quote("DOWN", best_bid=max(0.01, (1 - market_prob) - spread / 2),
                     best_ask=min(0.99, (1 - market_prob) + spread / 2), ask_size=1000, bid_size=1000)

        in_window = cfg.min_seconds_to_resolution <= sec_left <= cfg.max_seconds_to_resolution
        if not traded and in_window:
            sig = strategy.evaluate(market, open_px, px, vol_per_sec, up_q, dn_q, t)
            if sig is not None and risk.can_enter(sig) is None:
                avail = up_q.ask_size if sig.side is Side.UP else dn_q.ask_size
                sig = risk.size_signal(sig, available_size=avail)
                if sig.size > 0:
                    position = executor.place(sig)
                    if position:
                        risk.register_entry(position)
                        traded = True
                        st.entries += 1
                        print(f"  ⮕ ENTER {sig.side.value:<4} {sig.size:>3.0f}@{sig.price:.3f}  "
                              f"fair={sig.fair_value:.3f} edge={sig.edge:+.3f}  "
                              f"spot=${px:,.0f}  {sec_left:.0f}s left", flush=True)
        if step_sleep:
            time.sleep(step_sleep)

    final_up = px > open_px
    if position is not None:
        executor.settle(position, final_up)
        risk.register_settlement(position)
        st.realized += position.pnl
        st.equity += position.pnl
        st.peak = max(st.peak, st.equity)
        if position.pnl > 0:
            st.wins += 1
        elif position.pnl < 0:
            st.losses += 1
        result = "WIN " if position.pnl > 0 else "LOSS"
        arrow = "UP" if final_up else "DOWN"
        print(f"  ⮕ CLOSE {arrow:<4} → {result} pnl=${position.pnl:+6.2f}  "
              f"equity=${st.equity:,.2f}", flush=True)
    else:
        print("  ⮕ no edge found — skipped", flush=True)
    st.equity_curve.append(st.equity)


def run_dashboard(
    cfg: Config, seconds: float = 60.0, seed: Optional[int] = None,
    annual_vol: float = 0.60, market_lag: float = 0.12, spread: float = 0.02,
    step_sleep: float = 0.004,
) -> DashState:
    rng = random.Random(seed if seed is not None else time.time_ns())
    strategy = build_strategy(cfg.strategy, cfg)
    risk = RiskManager(cfg)
    executor = PaperExecutor(cfg.state_dir)

    st = DashState(bankroll=cfg.bankroll_usd, equity=cfg.bankroll_usd, peak=cfg.bankroll_usd)
    st.equity_curve.append(st.equity)

    started = time.time()
    print(_panel(st, started), flush=True)
    i = 0
    while time.time() - started < seconds:
        duration_min = cfg.durations_minutes[i % len(cfg.durations_minutes)]
        _play_market(cfg, strategy, risk, executor, st, rng,
                     duration_min, annual_vol, market_lag, spread, step_sleep)
        i += 1
        if i % 6 == 0:
            print("\n" + _panel(st, started), flush=True)

    print("\n" + _panel(st, started), flush=True)
    print("\nSession ended.", flush=True)

    # persist the equity curve so it can be charted / reviewed afterwards
    try:
        import csv, os
        os.makedirs(cfg.state_dir, exist_ok=True)
        with open(os.path.join(cfg.state_dir, "equity_curve.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["market_index", "equity"])
            for idx, eq in enumerate(st.equity_curve):
                w.writerow([idx, f"{eq:.2f}"])
    except OSError:
        pass
    return st
