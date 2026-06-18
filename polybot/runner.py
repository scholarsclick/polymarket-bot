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


def _vol_per_sec(candles, tf_seconds: float, floor: float = 1e-6) -> float:
    """Per-second return volatility estimated from candle closes."""
    closes = [c.close for c in candles][-60:]
    if len(closes) < 5:
        return floor
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i] > 0 and closes[i - 1] > 0]
    if len(rets) < 4:
        return floor
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    per_candle = math.sqrt(var)
    return max(floor, per_candle / math.sqrt(max(1.0, tf_seconds)))


def _health_dict(h) -> dict:
    return {
        "ok": h.ok, "source": h.source, "last_error": h.last_error,
        "last_ok_ts": h.last_ok_ts, "age": h.age_seconds,
    }


def _analysis_dict(a) -> dict:
    return {
        "symbol": a.symbol, "timeframe": a.timeframe, "price": a.price,
        "ema9": a.ema9, "ema21": a.ema21, "rsi": a.rsi,
        "macd_hist": a.macd_hist, "atr": a.atr, "volume_change": a.volume_change,
        "body_pct": a.body_pct, "trend": a.trend,
        "bull_score": a.bull_score, "bear_score": a.bear_score,
        "signals": list(a.signals),
        "patterns": [f"{n}:{d}" for n, d in a.patterns],
        "structure": dict(a.structure),
        "ok": a.ok,
    }


def _trade_view(t: dict) -> dict:
    return {k: v for k, v in t.items() if k != "position"}


def make_early_closed_trade(t: dict, exit_price: float, spot_at_close: float,
                            now: float, exit_type: str, reason: str) -> dict:
    """Closed-trade record for an EARLY exit (sold back into the book at
    `exit_price`). PnL = size * (exit_price - entry_price). The win/loss
    `result` reflects whether the PREDICTION was correct (did the underlying
    move in the predicted direction vs the candle open), NOT merely whether we
    booked a profit — a profitable-but-wrong exit counts as a loss."""
    pnl = t["size"] * (exit_price - t["entry_price"])
    correct = ((t["side"] == "UP" and spot_at_close > t["candle_open"])
               or (t["side"] == "DOWN" and spot_at_close < t["candle_open"]))
    return {
        "entry_time": t["entry_time"], "close_time": now,
        "market": t["market"], "symbol": t["symbol"],
        "duration_min": t["duration_min"], "side": t["side"],
        "entry_price": t["entry_price"], "close_price": exit_price,
        "exit_price": exit_price, "pnl": pnl,
        "correct": correct, "result": "win" if correct else "loss",
        "reason_entry": t["reason_entry"], "reason_close": reason,
        "exit_type": exit_type,
    }


def _confidence_scale(confidence, cfg) -> float:
    """Scale factor for position sizing based on signal confidence."""
    if not getattr(cfg, "confidence_sizing", False):
        return 1.0
    base = max(1, getattr(cfg, "exit_confidence_min", 2))
    lo = getattr(cfg, "confidence_sizing_min_mult", 0.5)
    hi = getattr(cfg, "confidence_sizing_max_mult", 1.5)
    return max(lo, min(hi, (confidence or 0) / base))


def _entry_quality_block(spread, liquidity, cfg):
    """Return a reason string if a market's book is too wide/thin to trade."""
    if getattr(cfg, "max_spread", 0) and spread is not None and spread > cfg.max_spread:
        return f"wide spread {spread:.2f}>{cfg.max_spread:.2f}"
    if getattr(cfg, "min_liquidity", 0) and (liquidity or 0) < cfg.min_liquidity:
        return f"illiquid {liquidity:.0f}<{cfg.min_liquidity:.0f}"
    return None


def make_closed_trade(t: dict, exit_px: float, now: float) -> dict:
    """Build the closed-trade record (full lifecycle + reasons). Pure/testable."""
    resolved_up = exit_px > t["candle_open"]
    won = ((resolved_up and t["side"] == "UP")
           or (not resolved_up and t["side"] == "DOWN"))
    close_price = 1.0 if won else 0.0            # binary settlement value
    pnl = t["size"] * close_price - t["size"] * t["entry_price"]
    return {
        "entry_time": t["entry_time"], "close_time": now,
        "market": t["market"], "symbol": t["symbol"],
        "duration_min": t["duration_min"], "side": t["side"],
        "entry_price": t["entry_price"], "close_price": close_price,
        "exit_price": exit_px, "pnl": pnl, "correct": won,
        "result": "win" if won else "loss",
        "exit_type": "resolution",
        "reason_entry": t["reason_entry"],
        "reason_close": (f"market resolved {'UP' if resolved_up else 'DOWN'} "
                         f"(open {t['candle_open']:,.2f} → close {exit_px:,.2f}); "
                         f"{'WON' if won else 'LOST'}"),
        "resolved_up": resolved_up, "won": won,
    }


@dataclass
class BotState:
    mode: str = "paper"
    running: bool = False
    error: Optional[str] = None
    warning: Optional[str] = None
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

    # live-data fields (live mode)
    timeframe: str = "5m"
    data_available: bool = False
    simulator_active: bool = False                              # TRUE only in simulator test mode
    prices: Dict[str, dict] = field(default_factory=dict)        # sym -> {price, ts, source}
    spot_health: dict = field(default_factory=dict)
    candle_health: dict = field(default_factory=dict)
    polymarket_health: dict = field(default_factory=dict)
    analyses: Dict[str, dict] = field(default_factory=dict)      # sym -> indicator/pattern snapshot
    open_trades: List[dict] = field(default_factory=list)
    closed_trades: List[dict] = field(default_factory=list)
    opportunities: List[dict] = field(default_factory=list)      # opportunity log (newest first)
    unrealized: float = 0.0                                       # sum of open-trade uPnL
    exit_perf: List[dict] = field(default_factory=list)          # performance by exit type
    hold_vs_early: dict = field(default_factory=dict)            # early-exit vs hold-to-resolution
    model_stats: dict = field(default_factory=dict)             # self-learning model status
    debug: dict = field(default_factory=dict)                    # raw source statuses for the debug panel

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
                 time_scale: float = 12.0, timeframe: str = "5m", min_confidence: int = 3):
        self.cfg = cfg
        self.mode = mode
        self.execute_orders = execute_orders
        self.time_scale = time_scale
        self.timeframe = timeframe
        self.min_confidence = min_confidence
        self.state = BotState(mode=mode, bankroll=cfg.bankroll_usd,
                              equity=cfg.bankroll_usd, peak=cfg.bankroll_usd,
                              timeframe=timeframe)
        self.state.equity_curve.append(cfg.bankroll_usd)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._force_discover = threading.Event()
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

    def request_market_refresh(self) -> None:
        """Force a Polymarket re-discovery on the next loop (manual button)."""
        self._force_discover.set()

    def snapshot(self) -> BotState:
        """Return a shallow copy safe for the UI to read."""
        with self._lock:
            s = self.state
            copy = BotState(**{k: getattr(s, k) for k in s.__dataclass_fields__})
            copy.equity_curve = list(s.equity_curve)
            copy.recent_trades = list(s.recent_trades)
            copy.scanned = list(s.scanned)
            copy.prices = dict(s.prices)
            copy.analyses = dict(s.analyses)
            copy.open_trades = list(s.open_trades)
            copy.closed_trades = list(s.closed_trades)
            copy.opportunities = list(s.opportunities)
            copy.exit_perf = list(s.exit_perf)
            copy.hold_vs_early = dict(s.hold_vs_early)
            copy.model_stats = dict(s.model_stats)
            copy.spot_health = dict(s.spot_health)
            copy.candle_health = dict(s.candle_health)
            copy.polymarket_health = dict(s.polymarket_health)
            copy.debug = dict(s.debug)
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
            s.simulator_active = True            # simulator test mode only
            s.debug = {"simulator_active": True, "note": "SIMULATOR TEST MODE — synthetic prices"}
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

    # ----- live loop (REAL data) ------------------------------------------
    def _run_live(self) -> None:
        from .analysis import analyze, decide_opportunity
        from .clob import ClobGateway
        from .executor import LiveExecutor, PaperExecutor
        from .exits import evaluate_exit, exit_levels
        from .gamma import GammaClient
        from .learning import TradeLearner, featurize
        from .marketdata import MarketDataFeed

        cfg = self.cfg
        cfg.dry_run = not self.execute_orders
        if self.execute_orders and not cfg.private_key:
            with self._lock:
                self.state.error = ("Live order execution needs POLYMARKET_PRIVATE_KEY "
                                    "in .env. Switch off 'execute real orders' to run "
                                    "live data with simulated fills.")
                self.state.running = False
            return

        symbols = [s for s in (cfg.symbols or ["BTC", "ETH"]) if s.upper() in ("BTC", "ETH", "SOL")]
        tf = self.timeframe
        tf_seconds = {"1m": 60, "5m": 300, "15m": 900}.get(tf, 300)
        feed = MarketDataFeed(sources=[s for s in cfg.price_sources if s in ("binance", "coinbase")]
                              or ["binance", "coinbase"])
        gamma = GammaClient(cfg.gamma_host)
        gateway = ClobGateway(cfg)
        try:
            gateway.connect(require_auth=self.execute_orders)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state.error = f"CLOB connect failed: {exc}"
                self.state.running = False
            return

        strategy = build_strategy(cfg.strategy, cfg)
        risk = RiskManager(cfg)
        executor = LiveExecutor(gateway) if self.execute_orders else PaperExecutor(cfg.state_dir)
        learner = TradeLearner(cfg.state_dir, lr=cfg.learning_lr,
                               min_samples=cfg.learning_min_samples)

        open_trades: Dict[str, dict] = {}     # condition_id -> trade dict
        closed_trades: deque = deque(maxlen=200)
        opp_log: deque = deque(maxlen=120)
        traded_markets: set = set()           # one entry per market window
        cf_watch: list = []                   # early-exited trades awaiting resolution
        cf_stats = {"count": 0, "early_total": 0.0, "hold_total": 0.0}  # hold-vs-early
        markets_cache: List = []
        discovery_report: dict = {}
        last_discover = 0.0
        trade_seq = 0
        poll = max(1.0, min(5.0, cfg.poll_interval_seconds))

        try:
            while not self._stop.is_set():
                time.sleep(poll)
                now = time.time()

                # 1) live spot + candles (REAL, validated). No fake fallback.
                prices, candles, analyses = {}, {}, {}
                debug = {"simulator_active": False, "spot_raw": {}, "candles": {}}
                data_ok = True
                for sym in symbols:
                    sr = feed.get_validated_spot(sym)          # dual-source + sanity + divergence
                    cs = feed.get_candles(sym, tf, limit=120)
                    debug["spot_raw"][sym] = {
                        "chosen": sr.price, "source": sr.source,
                        "raw": sr.raw, "rejected": sr.rejected, "error": sr.error,
                    }
                    debug["candles"][sym] = {
                        "ok": bool(cs), "count": len(cs) if cs else 0,
                        "source": feed.candle_health.source,
                        "last_close_time": cs[-1].close_time if cs else None,
                        "error": None if cs else feed.candle_health.last_error,
                    }
                    if sr.price is None or not cs:
                        data_ok = False
                        continue
                    prices[sym] = {"price": sr.price, "ts": now, "source": sr.source}
                    candles[sym] = cs
                    analyses[sym] = analyze(sym, tf, cs)

                if not data_ok or not prices:
                    # req 9 & 10: stop trading, warn, NEVER use a fake/simulator price
                    bad = []
                    for sym in symbols:
                        d = debug["spot_raw"].get(sym, {})
                        if d.get("rejected"):
                            bad.append(f"{sym} rejected ({d['rejected']})")
                        elif d.get("error"):
                            bad.append(f"{sym} spot down")
                        if not debug["candles"].get(sym, {}).get("ok"):
                            bad.append(f"{sym} candles down")
                    self._commit_live(
                        risk, feed, prices, analyses, open_trades, closed_trades,
                        opp_log, tf, data_available=False, debug=debug, poly_health={},
                        warning="Live data unavailable — trading paused. " + "; ".join(bad))
                    continue

                # 2) discover Polymarket BTC/ETH 5m & 15m markets (+ diagnostics)
                poly_health = {"discover_ok": True, "markets": len(markets_cache),
                               "book_ok": None, "error": None}
                forced = self._force_discover.is_set()
                if forced or now - last_discover > cfg.market_refresh_seconds or not markets_cache:
                    try:
                        markets_cache, discovery_report = gamma.discover_with_report(
                            symbols, cfg.durations_minutes, cfg.duration_tolerance_seconds)
                        poly_health["discover_ok"] = (discovery_report.get("http_status") == 200
                                                      or bool(discovery_report.get("markets_returned")))
                        poly_health["markets"] = len(markets_cache)
                        # enrich discovered markets with live book data for the panel
                        discovery_report["discovered_markets"] = self._enrich_discovered(
                            gateway, markets_cache, now)
                    except Exception as exc:  # noqa: BLE001
                        markets_cache = markets_cache or []
                        discovery_report = {"error": str(exc)}
                        poly_health["discover_ok"] = False
                        poly_health["error"] = str(exc)
                        self._set_warning(f"market discovery failed: {exc}")
                    last_discover = now
                    self._force_discover.clear()
                poly_health["discovery"] = discovery_report

                risk.consecutive_losses = 0  # independent windows for the demo
                risk.realized_pnl_today = 0.0

                # 3) evaluate each market
                scanned = []
                for m in markets_cache:
                    if m.symbol not in analyses or m.end_time <= now:
                        continue
                    sec_left = m.seconds_to_resolution(now)
                    if sec_left > cfg.max_seconds_to_resolution:
                        continue
                    cs = candles[m.symbol]
                    spot = prices[m.symbol]["price"]
                    candle_open = feed.candle_open_at(cs, m.start_time) or cs[-1].open
                    vol_ps = _vol_per_sec(cs, tf_seconds)
                    try:
                        up_q = gateway.get_quote(m.up_token_id)
                        dn_q = gateway.get_quote(m.down_token_id)
                        poly_health["book_ok"] = True
                    except Exception as exc:  # noqa: BLE001
                        poly_health["book_ok"] = False
                        poly_health["error"] = str(exc)
                        continue

                    # real Polymarket YES/NO prices, spread and liquidity
                    yes_price = up_q.best_ask
                    no_price = dn_q.best_ask
                    spread = (up_q.best_ask - up_q.best_bid) if (up_q.best_ask and up_q.best_bid) else None
                    liquidity = sum(v for v in (up_q.ask_size, up_q.bid_size,
                                                dn_q.ask_size, dn_q.bid_size) if v)

                    a = analyses[m.symbol]
                    opp = decide_opportunity(
                        a, m.question, m.duration_minutes, sec_left,
                        candle_open, spot, vol_ps, up_q.best_ask, dn_q.best_ask,
                        cfg.min_edge, min_confidence=self.min_confidence,
                        avoid_rsi_extremes=cfg.avoid_rsi_extremes,
                        rsi_overbought=cfg.rsi_overbought, rsi_oversold=cfg.rsi_oversold)
                    # self-learning: features + model probability for this setup
                    features = model_prob = None
                    if opp.side is not None:
                        features = featurize(a, opp.side.value, opp.edge, sec_left,
                                             m.duration_minutes)
                        if cfg.learning_enabled:
                            model_prob = learner.prob(features)

                    opp_row = {
                        "time": now, "symbol": m.symbol, "market": m.question[:48],
                        "duration_min": m.duration_minutes, "seconds_left": sec_left,
                        "expiry": m.end_time, "yes_price": yes_price, "no_price": no_price,
                        "spread": spread, "liquidity": liquidity,
                        "action": opp.action, "side": opp.side.value if opp.side else "—",
                        "confidence": opp.confidence, "edge": opp.edge, "fair": opp.fair,
                        "model": model_prob, "reason": opp.reason,
                    }
                    scanned.append(opp_row)

                    # --- manage an open trade on this market (early exits) ---
                    if m.condition_id in open_trades:
                        t = open_trades[m.condition_id]
                        q = up_q if t["side"] == "UP" else dn_q
                        mark = q.best_bid if q.best_bid is not None else q.mid  # sellable price
                        if mark is not None:
                            t["mark"] = mark
                            t["peak"] = max(t.get("peak", t["entry_price"]), mark)
                            t["upnl"] = t["size"] * (mark - t["entry_price"])
                            t["trail"] = exit_levels(t["entry_price"], t["peak"], cfg)["trail"]
                            decision = evaluate_exit(
                                side=t["side"], entry_price=t["entry_price"], mark=mark,
                                peak_mark=t["peak"], seconds_left=sec_left,
                                vol_per_sec=vol_ps, entry_vol=t.get("entry_vol"),
                                trend=a.trend, confidence=abs(a.bull_score - a.bear_score),
                                cfg=cfg)
                            if decision.should_exit:
                                t = open_trades.pop(m.condition_id)
                                if self.execute_orders:
                                    try:
                                        gateway.sell_marketable(t["position"].token_id, mark, t["size"])
                                    except Exception as exc:  # noqa: BLE001
                                        poly_health["error"] = f"sell failed: {exc}"
                                    t["position"].open = False
                                    t["position"].pnl = t["size"] * (mark - t["entry_price"])
                                else:
                                    executor.close_at(t["position"], mark, decision.type)
                                risk.register_settlement(t["position"])
                                rec = make_early_closed_trade(t, mark, spot, now,
                                                              decision.type, decision.reason)
                                if t.get("features") is not None:
                                    learner.record_outcome(t["features"], rec["correct"])
                                closed_trades.appendleft(rec)
                                cf_watch.append({"end_time": t["end_time"],
                                                 "candle_open": t["candle_open"], "side": t["side"],
                                                 "size": t["size"], "entry_price": t["entry_price"],
                                                 "symbol": t["symbol"], "exit_type": decision.type,
                                                 "early_pnl": rec["pnl"]})
                                opp_log.appendleft({**opp_row, "action": f"EXIT {decision.type}"})

                    # --- entry (one per market window) ---
                    in_window = sec_left >= cfg.min_seconds_to_resolution
                    block = _entry_quality_block(spread, liquidity, cfg)
                    if (not block and model_prob is not None
                            and model_prob < cfg.learning_min_prob):
                        block = f"model P(correct) {model_prob:.0%}<{cfg.learning_min_prob:.0%}"
                    if block and opp.action == "enter":
                        opp_row["action"] = "no_trade"
                        opp_row["reason"] = f"skip: {block} | " + opp_row["reason"]
                    if (opp.action == "enter" and not block
                            and m.condition_id not in open_trades
                            and m.condition_id not in traded_markets and in_window
                            and risk.can_enter_basic() is None):
                        from .models import Signal
                        sig = Signal(market=m, side=opp.side, token_id=m.token_id(opp.side),
                                     fair_value=opp.fair, price=opp.market_price, edge=opp.edge,
                                     reason=opp.reason)
                        avail = up_q.ask_size if opp.side is Side.UP else dn_q.ask_size
                        scale = _confidence_scale(opp.confidence, cfg)
                        if model_prob is not None and cfg.learning_size_weight:
                            scale *= max(0.5, min(1.5, model_prob / 0.5))  # 0.5→0.5x, 0.75→1.5x
                        sig = risk.size_signal(sig, available_size=avail, confidence_scale=scale)
                        if sig.size > 0:
                            pos = executor.place(sig)
                            if pos:
                                risk.register_entry(pos)
                                traded_markets.add(m.condition_id)
                                trade_seq += 1
                                open_trades[m.condition_id] = {
                                    "id": trade_seq, "entry_time": now,
                                    "market": m.question[:48], "symbol": m.symbol,
                                    "duration_min": m.duration_minutes, "side": opp.side.value,
                                    "size": sig.size, "entry_price": sig.price,
                                    "mark": sig.price, "peak": sig.price, "upnl": 0.0,
                                    "entry_vol": vol_ps, "trail": None,
                                    "candle_open": candle_open, "end_time": m.end_time,
                                    "reason_entry": opp.reason, "position": pos,
                                    "features": features,
                                }
                                opp_log.appendleft({**opp_row, "action": "ENTER"})

                # 4) settle expired open trades
                for cid in [c for c, t in open_trades.items() if now >= t["end_time"]]:
                    t = open_trades.pop(cid)
                    exit_px = feed.get_spot(t["symbol"]) or prices.get(t["symbol"], {}).get("price")
                    if exit_px is None:
                        open_trades[cid] = t  # retry next loop
                        continue
                    rec = make_closed_trade(t, exit_px, now)
                    if isinstance(executor, PaperExecutor):
                        executor.settle(t["position"], rec["resolved_up"])
                    else:
                        t["position"].open = False
                        t["position"].pnl = rec["pnl"]
                    risk.register_settlement(t["position"])
                    if t.get("features") is not None:
                        learner.record_outcome(t["features"], rec["correct"])
                    closed_trades.appendleft({k: v for k, v in rec.items()
                                              if k not in ("resolved_up", "won")})

                # 5) mature counterfactuals: what would early exits have made if held?
                still_watch = []
                for cf in cf_watch:
                    if now < cf["end_time"]:
                        still_watch.append(cf)
                        continue
                    settle_px = feed.get_spot(cf["symbol"]) or prices.get(cf["symbol"], {}).get("price")
                    if settle_px is None:
                        still_watch.append(cf)
                        continue
                    resolved_up = settle_px > cf["candle_open"]
                    won = ((resolved_up and cf["side"] == "UP")
                           or (not resolved_up and cf["side"] == "DOWN"))
                    hold_pnl = cf["size"] * ((1.0 if won else 0.0) - cf["entry_price"])
                    cf_stats["count"] += 1
                    cf_stats["early_total"] += cf["early_pnl"]
                    cf_stats["hold_total"] += hold_pnl
                cf_watch[:] = still_watch

                self._commit_live(risk, feed, prices, analyses, open_trades, closed_trades,
                                  opp_log, tf, data_available=True, debug=debug,
                                  poly_health=poly_health, warning=None, scanned=scanned,
                                  cf_stats=cf_stats, model_stats=learner.stats())
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state.error = f"live loop error: {exc}"
        finally:
            with self._lock:
                self.state.running = False

    def _enrich_discovered(self, gateway, markets, now, cap: int = 16) -> list:
        """Attach live YES/NO prices + liquidity + expiry to discovered markets
        (for the discovery debug panel), for up to `cap` markets."""
        out = []
        for m in markets[:cap]:
            yes = no = liq = None
            try:
                uq = gateway.get_quote(m.up_token_id)
                dq = gateway.get_quote(m.down_token_id)
                yes, no = uq.best_ask, dq.best_ask
                liq = sum(v for v in (uq.ask_size, uq.bid_size, dq.ask_size, dq.bid_size) if v)
            except Exception:  # noqa: BLE001
                pass
            out.append({
                "title": m.question[:60], "symbol": m.symbol,
                "duration_min": m.duration_minutes, "expiry": m.end_time,
                "seconds_left": m.seconds_to_resolution(now),
                "yes": yes, "no": no, "liquidity": liq,
            })
        return out

    def _set_warning(self, msg: str) -> None:
        with self._lock:
            self.state.warning = msg

    def _commit_live(self, risk, feed, prices, analyses, open_trades, closed_trades,
                     opp_log, timeframe, data_available, warning, scanned=None,
                     debug=None, poly_health=None, cf_stats=None, model_stats=None) -> None:
        from .exits import exit_performance
        closed = list(closed_trades)
        wins = sum(1 for t in closed if t["result"] == "win")
        losses = sum(1 for t in closed if t["result"] == "loss")
        realized = sum(t["pnl"] for t in closed)
        unrealized = sum(t.get("upnl", 0.0) for t in open_trades.values())
        equity = self.cfg.bankroll_usd + realized
        with self._lock:
            s = self.state
            s.timeframe = timeframe
            s.data_available = data_available
            s.simulator_active = False           # NEVER the simulator in live mode
            s.warning = warning
            s.prices = dict(prices)
            s.spot_health = _health_dict(feed.spot_health)
            s.candle_health = _health_dict(feed.candle_health)
            if poly_health is not None:
                s.polymarket_health = dict(poly_health)
            if debug is not None:
                s.debug = dict(debug)
            s.analyses = {sym: _analysis_dict(a) for sym, a in analyses.items()}
            s.open_trades = [_trade_view(t) for t in open_trades.values()]
            s.closed_trades = closed
            if scanned is not None:
                s.scanned = sorted(scanned, key=lambda d: d["seconds_left"])
                s.opportunities = list(opp_log)
            s.entries = len(open_trades) + len(closed)
            s.wins, s.losses, s.realized = wins, losses, realized
            s.unrealized = unrealized
            s.exit_perf = exit_performance(closed)
            if model_stats is not None:
                s.model_stats = model_stats
            if cf_stats and cf_stats["count"]:
                s.hold_vs_early = {
                    "count": cf_stats["count"],
                    "early_total": cf_stats["early_total"],
                    "hold_total": cf_stats["hold_total"],
                    "better": ("early" if cf_stats["early_total"] >= cf_stats["hold_total"]
                               else "hold"),
                }
            s.equity = equity
            s.peak = max(s.peak, equity)
            s.exposure = risk.current_exposure()
            if not s.equity_curve or abs(s.equity_curve[-1] - equity) > 1e-9:
                s.equity_curve.append(equity)
                if len(s.equity_curve) > 1000:
                    s.equity_curve = s.equity_curve[-1000:]
