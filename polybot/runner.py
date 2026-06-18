"""Background bot runner with thread-safe shared state for the Streamlit UI.

Two modes:

- **paper**  — a self-contained concurrent simulator (no network, no keys).
  Several BTC/ETH 5m & 15m markets run at once in compressed time; the real
  strategy, risk manager and paper executor make and settle the trades.
- **live**   — launches the real `Engine` against Polymarket. With
  `execute_orders=False` it reads real markets/books but simulates fills
  (needs network only); with `execute_orders=True` it places real orders
  (needs a funded private key in `.env`).

The UI never blocks on the bot: it reads `runner.snapshot()` each refresh.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .executor import PaperExecutor
from .models import CryptoMarket, Position, Quote, Side
from .risk import RiskManager
from .strategy import build_strategy, fair_up_probability

SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass
class BotState:
    mode: str = "paper"
    running: bool = False
    error: Optional[str] = None
    started_at: float = 0.0
    bankroll: float = 200.0
    equity: float = 200.0
    peak: float = 200.0
    realized: float = 0.0
    exposure: float = 0.0
    entries: int = 0
    wins: int = 0
    losses: int = 0
    equity_curve: List[float] = field(default_factory=list)
    recent_trades: List[dict] = field(default_factory=list)
    scanned: List[dict] = field(default_factory=list)

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return (self.wins / self.settled) if self.settled else 0.0


# --------------------------------------------------------------------------
# Concurrent paper simulator
# --------------------------------------------------------------------------
class _SimMarket:
    """One synthetic up/down market progressing in compressed real time."""

    def __init__(self, cfg: Config, rng: random.Random, symbol: str,
                 duration_min: int, annual_vol: float, lag: float, spread: float):
        self.cfg = cfg
        self.rng = rng
        self.symbol = symbol
        self.duration_min = duration_min
        self.lag = lag
        self.spread = spread
        self.vol_per_sec = annual_vol / math.sqrt(SECONDS_PER_YEAR)
        self.open_px = {"BTC": 100_000.0, "ETH": 3_500.0, "SOL": 180.0}.get(symbol, 100.0)
        self.px = self.open_px
        self.market_prob = 0.5
        self.sim_elapsed = 0.0
        self.duration = duration_min * 60
        mid = rng.randrange(1_000_000)
        self.market = CryptoMarket(
            condition_id=f"sim-{symbol}-{mid}",
            question=f"{symbol} up or down ({duration_min}m)",
            slug=f"{symbol.lower()}-{duration_min}m-{mid}",
            symbol=symbol, up_token_id=f"UP-{mid}", down_token_id=f"DN-{mid}",
            start_time=0.0, end_time=float(self.duration), tick_size=0.01, min_size=5.0,
        )
        self.entered = False
        self.position: Optional[Position] = None

    def advance(self, sim_dt: float) -> None:
        z = self.rng.gauss(0.0, 1.0)
        self.px *= math.exp(self.vol_per_sec * math.sqrt(sim_dt) * z)
        self.sim_elapsed += sim_dt
        sec_left = max(0.0, self.duration - self.sim_elapsed)
        fair_up = fair_up_probability(self.px, self.open_px, sec_left, self.vol_per_sec)
        self.market_prob += (fair_up - self.market_prob) * self.lag + self.rng.gauss(0.0, 0.008)
        self.market_prob = min(0.99, max(0.01, self.market_prob))

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.duration - self.sim_elapsed)

    @property
    def expired(self) -> bool:
        return self.sim_elapsed >= self.duration

    def quotes(self):
        mp = self.market_prob
        up_q = Quote(self.market.up_token_id,
                     best_bid=max(0.01, mp - self.spread / 2),
                     best_ask=min(0.99, mp + self.spread / 2), ask_size=1000, bid_size=1000)
        dn_q = Quote(self.market.down_token_id,
                     best_bid=max(0.01, (1 - mp) - self.spread / 2),
                     best_ask=min(0.99, (1 - mp) + self.spread / 2), ask_size=1000, bid_size=1000)
        return up_q, dn_q

    def fair_up(self) -> float:
        return fair_up_probability(self.px, self.open_px, self.seconds_left, self.vol_per_sec)


class BotRunner:
    def __init__(self, cfg: Config, mode: str = "paper", execute_orders: bool = False,
                 time_scale: float = 12.0):
        self.cfg = cfg
        self.mode = mode
        self.execute_orders = execute_orders
        self.time_scale = time_scale
        self.state = BotState(mode=mode, bankroll=cfg.bankroll_usd,
                              equity=cfg.bankroll_usd, peak=cfg.bankroll_usd)
        self.state.equity_curve.append(cfg.bankroll_usd)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._engine = None  # live Engine handle

    # ----- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        with self._lock:
            self.state.running = True
            self.state.error = None
            self.state.started_at = time.time()
        target = self._run_live if self.mode == "live" else self._run_paper
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self.state.running = False

    def snapshot(self) -> BotState:
        """Return a shallow copy safe for the UI to read."""
        with self._lock:
            s = self.state
            copy = BotState(**{k: getattr(s, k) for k in s.__dataclass_fields__})
            copy.equity_curve = list(s.equity_curve)
            copy.recent_trades = list(s.recent_trades)
            copy.scanned = list(s.scanned)
            return copy

    # ----- paper loop ------------------------------------------------------
    def _run_paper(self) -> None:
        rng = random.Random()
        strategy = build_strategy(self.cfg.strategy, self.cfg)
        risk = RiskManager(self.cfg)
        executor = PaperExecutor(self.cfg.state_dir)
        recent: deque = deque(maxlen=40)

        symbols = self.cfg.symbols or ["BTC", "ETH"]
        durations = self.cfg.durations_minutes or [5, 15]
        markets: List[_SimMarket] = []

        def spawn():
            sym = rng.choice(symbols)
            dur = rng.choice(durations)
            markets.append(_SimMarket(self.cfg, rng, sym, dur,
                                      annual_vol=0.60, lag=0.12, spread=0.02))

        for _ in range(5):
            spawn()

        last = time.time()
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
                now = time.time()
                sim_dt = (now - last) * self.time_scale
                last = now

                # independent markets: keep kill-switches disarmed so the demo
                # keeps trading (a live session keeps them armed). Closed
                # positions are kept so cumulative stats persist; they don't
                # count toward exposure (that filters on `open`).
                risk.consecutive_losses = 0
                risk.realized_pnl_today = 0.0

                scanned = []
                for m in markets:
                    m.advance(sim_dt)
                    up_q, dn_q = m.quotes()
                    decision = "watching"
                    in_window = (self.cfg.min_seconds_to_resolution <= m.seconds_left
                                 <= self.cfg.max_seconds_to_resolution)
                    if not m.entered and in_window:
                        sig = strategy.evaluate(m.market, m.open_px, m.px,
                                                m.vol_per_sec, up_q, dn_q, m.sim_elapsed)
                        if sig is not None and risk.can_enter(sig) is None:
                            avail = up_q.ask_size if sig.side is Side.UP else dn_q.ask_size
                            sig = risk.size_signal(sig, available_size=avail)
                            if sig.size > 0:
                                pos = executor.place(sig)
                                if pos:
                                    risk.register_entry(pos)
                                    m.entered = True
                                    m.position = pos
                                    decision = f"BUY {sig.side.value} {sig.size:.0f}@{sig.price:.3f}"
                                    recent.appendleft({
                                        "time": time.time(), "symbol": m.symbol,
                                        "duration_min": m.duration_min, "side": sig.side.value,
                                        "size": sig.size, "price": sig.price,
                                        "status": "open", "pnl": 0.0,
                                    })
                    scanned.append({
                        "symbol": m.symbol, "duration_min": m.duration_min,
                        "seconds_left": m.seconds_left, "spot": m.px, "open": m.open_px,
                        "fair_up": m.fair_up(), "up_ask": up_q.best_ask,
                        "down_ask": dn_q.best_ask, "decision": decision,
                    })

                # settle expired markets and respawn
                still: List[_SimMarket] = []
                for m in markets:
                    if not m.expired:
                        still.append(m)
                        continue
                    if m.position is not None:
                        final_up = m.px > m.open_px
                        executor.settle(m.position, final_up)
                        risk.register_settlement(m.position)
                        # update the matching recent-trade row
                        for r in recent:
                            if (r["status"] == "open" and r["symbol"] == m.symbol
                                    and abs(r["price"] - m.position.entry_price) < 1e-9):
                                r["status"] = "win" if m.position.pnl > 0 else "loss"
                                r["pnl"] = m.position.pnl
                                break
                    spawn()
                markets = still
                while len(markets) < 5:
                    spawn()

                self._commit_paper(risk, list(recent), scanned)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state.error = f"paper loop error: {exc}"
        finally:
            with self._lock:
                self.state.running = False

    def _commit_paper(self, risk: RiskManager, recent: List[dict], scanned: List[dict]) -> None:
        closed = [p for p in risk.open_positions if not p.open]
        wins = sum(1 for p in closed if p.pnl > 0)
        losses = sum(1 for p in closed if p.pnl < 0)
        realized = sum(p.pnl for p in closed)
        equity = self.cfg.bankroll_usd + realized
        with self._lock:
            s = self.state
            s.entries = len(risk.open_positions)
            s.wins, s.losses = wins, losses
            s.realized = realized
            s.equity = equity
            s.peak = max(s.peak, equity)
            s.exposure = risk.current_exposure()
            s.recent_trades = recent
            s.scanned = sorted(scanned, key=lambda d: d["seconds_left"])
            if not s.equity_curve or abs(s.equity_curve[-1] - equity) > 1e-9:
                s.equity_curve.append(equity)
                if len(s.equity_curve) > 1000:
                    s.equity_curve = s.equity_curve[-1000:]

    # ----- live loop -------------------------------------------------------
    def _run_live(self) -> None:
        from .engine import Engine

        cfg = self.cfg
        cfg.dry_run = not self.execute_orders
        if self.execute_orders and not cfg.private_key:
            with self._lock:
                self.state.error = ("Live order execution needs POLYMARKET_PRIVATE_KEY "
                                    "in .env. Switch off 'execute real orders' to run "
                                    "live data with simulated fills.")
                self.state.running = False
            return
        try:
            self._engine = Engine(cfg, observer=self._on_engine_snapshot)
            self._engine.run()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state.error = f"live engine error: {exc}"
        finally:
            with self._lock:
                self.state.running = False

    def _on_engine_snapshot(self, snap: dict) -> None:
        with self._lock:
            s = self.state
            s.entries = snap["entries"]
            s.wins, s.losses = snap["wins"], snap["losses"]
            s.realized = snap["realized"]
            s.equity = snap["equity"]
            s.peak = max(s.peak, snap["equity"])
            s.exposure = snap["exposure"]
            s.error = snap.get("halted")
            s.recent_trades = snap["recent_trades"]
            s.scanned = sorted(snap["scanned"], key=lambda d: d.get("seconds_left", 0))
            if not s.equity_curve or abs(s.equity_curve[-1] - snap["equity"]) > 1e-9:
                s.equity_curve.append(snap["equity"])
                if len(s.equity_curve) > 1000:
                    s.equity_curve = s.equity_curve[-1000:]
