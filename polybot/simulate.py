"""Offline simulation / mini-backtester.

Drives the *real* strategy, risk manager, and paper executor over synthetic
BTC candles so you can see end-to-end behaviour with no network or keys.

Model
-----
- Price follows a zero-drift geometric Brownian motion at the configured
  annual volatility, stepped every `poll_interval_seconds`.
- The "market" quote is a *lagging, noisy* estimate of the true fair value:
  it reacts toward fair with factor `market_lag` per step plus small noise, and
  is shown with a bid/ask `spread`. The bot's edge comes purely from the market
  being slow to reprice a move the price has already made — an honest demo, not
  a rigged one (the market is unbiased, just lagged).
- At candle close the market resolves up iff final price > open price, and the
  paper executor books the real win/loss.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from typing import List, Optional

from .config import Config
from .executor import PaperExecutor
from .models import CryptoMarket, Position, Quote, Side
from .risk import RiskManager
from .strategy import build_strategy, fair_up_probability

log = logging.getLogger("polybot.simulate")

SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass
class SimResult:
    n_markets: int = 0
    entries: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    notional: float = 0.0

    @property
    def hit_rate(self) -> float:
        return self.wins / self.entries if self.entries else 0.0

    @property
    def roi(self) -> float:
        return self.pnl / self.notional if self.notional else 0.0


def simulate_one_market(
    cfg: Config,
    strategy,
    risk: RiskManager,
    executor: PaperExecutor,
    rng: random.Random,
    duration_min: int,
    annual_vol: float,
    market_lag: float,
    spread: float,
) -> Optional[Position]:
    open_px = 100_000.0
    px = open_px
    vol_per_sec = annual_vol / math.sqrt(SECONDS_PER_YEAR)
    dt = max(1.0, cfg.poll_interval_seconds)
    duration = duration_min * 60

    market = CryptoMarket(
        condition_id=f"sim-{rng.randrange(1_000_000_000)}",
        question=f"BTC {duration_min}m up or down (sim)",
        slug=f"sim-btc-{duration_min}m",
        symbol="BTC",
        up_token_id="UP",
        down_token_id="DOWN",
        start_time=0.0,
        end_time=float(duration),
        tick_size=0.01,
        min_size=5.0,
    )

    market_prob = 0.5
    t = 0.0
    position: Optional[Position] = None
    traded = False

    while t < duration:
        z = rng.gauss(0.0, 1.0)
        px *= math.exp(vol_per_sec * math.sqrt(dt) * z)  # zero-drift GBM step
        t += dt
        seconds_remaining = duration - t

        true_fair = fair_up_probability(px, open_px, max(0.0, seconds_remaining), vol_per_sec)
        # market lags toward the truth, plus a little noise; unbiased but slow.
        market_prob += (true_fair - market_prob) * market_lag + rng.gauss(0.0, 0.008)
        market_prob = min(0.99, max(0.01, market_prob))

        up_q = Quote("UP",
                     best_bid=max(0.01, market_prob - spread / 2),
                     best_ask=min(0.99, market_prob + spread / 2),
                     ask_size=1000, bid_size=1000)
        dn_q = Quote("DOWN",
                     best_bid=max(0.01, (1 - market_prob) - spread / 2),
                     best_ask=min(0.99, (1 - market_prob) + spread / 2),
                     ask_size=1000, bid_size=1000)

        in_window = (cfg.min_seconds_to_resolution <= seconds_remaining
                     <= cfg.max_seconds_to_resolution)
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

    # settle at candle close
    final_up = px > open_px
    if position is not None:
        executor.settle(position, final_up)
        risk.register_settlement(position)
    return position


def run_simulation(
    cfg: Config,
    n_markets: int = 50,
    seed: int = 7,
    annual_vol: float = 0.60,
    market_lag: float = 0.45,
    spread: float = 0.02,
) -> SimResult:
    rng = random.Random(seed)
    strategy = build_strategy(cfg.strategy, cfg)
    risk = RiskManager(cfg)
    executor = PaperExecutor(cfg.state_dir)

    res = SimResult()
    for i in range(n_markets):
        # alternate through the configured durations
        duration_min = cfg.durations_minutes[i % len(cfg.durations_minutes)]
        # Each synthetic market is independent, so reset the per-market trade
        # counter AND the session kill-switches (consecutive losses / daily
        # loss). Otherwise a loss streak halts all further entries and the
        # backtest measures the kill-switch instead of the strategy's edge.
        # (In live trading those kill-switches stay armed across the session.)
        risk.trades_per_market.clear()
        risk.consecutive_losses = 0
        risk.realized_pnl_today = 0.0
        risk.open_positions = [p for p in risk.open_positions if p.open]
        pos = simulate_one_market(
            cfg, strategy, risk, executor, rng,
            duration_min, annual_vol, market_lag, spread,
        )
        res.n_markets += 1
        if pos is not None:
            res.entries += 1
            res.notional += pos.cost
            res.pnl += pos.pnl
            if pos.pnl > 0:
                res.wins += 1
            elif pos.pnl < 0:
                res.losses += 1

    return res
