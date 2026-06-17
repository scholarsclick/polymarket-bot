"""Main trading loop: discover markets, evaluate, size, execute, settle."""

from __future__ import annotations

import logging
import signal as signal_module
import time
from typing import Dict, List, Optional

from .clob import ClobGateway
from .config import Config
from .executor import LiveExecutor, PaperExecutor
from .gamma import GammaClient
from .models import CryptoMarket, Position, Quote
from .pricefeed import PriceFeed
from .risk import RiskManager
from .strategy import build_strategy

log = logging.getLogger("polybot.engine")


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.gamma = GammaClient(cfg.gamma_host)
        self.feed = PriceFeed(
            sources=cfg.price_sources,
            vol_window_seconds=cfg.vol_window_seconds,
            vol_floor_annual=cfg.vol_floor_annual,
            vol_ceiling_annual=cfg.vol_ceiling_annual,
        )
        self.strategy = build_strategy(cfg.strategy, cfg)
        self.risk = RiskManager(cfg)
        self.gateway = ClobGateway(cfg)

        if cfg.dry_run:
            self.executor = PaperExecutor(cfg.state_dir)
        else:
            self.executor = LiveExecutor(self.gateway)

        self.markets: Dict[str, CryptoMarket] = {}
        self._candle_opens: Dict[str, float] = {}
        self._last_refresh = 0.0
        self._running = True

    # ----- lifecycle -------------------------------------------------------
    def run(self) -> None:
        mode = "DRY-RUN (paper)" if self.cfg.dry_run else "LIVE"
        log.info("Starting polybot in %s mode | strategy=%s | symbols=%s durations=%s",
                 mode, self.strategy.name, self.cfg.symbols, self.cfg.durations_minutes)
        self.gateway.connect(require_auth=not self.cfg.dry_run)

        signal_module.signal(signal_module.SIGINT, self._stop)
        signal_module.signal(signal_module.SIGTERM, self._stop)

        while self._running:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 — never let one tick kill the loop
                log.exception("tick error: %s", exc)
            time.sleep(self.cfg.poll_interval_seconds)

        self._print_summary()

    def _stop(self, *_):
        log.info("Shutdown requested — finishing up...")
        self._running = False

    # ----- one iteration ---------------------------------------------------
    def _tick(self) -> None:
        now = time.time()
        if now - self._last_refresh >= self.cfg.market_refresh_seconds:
            self._refresh_markets(now)
            self._last_refresh = now

        self._settle_expired(now)

        halt = self.risk.halted()
        if halt:
            log.warning("Entries halted: %s", halt)
            return

        for market in list(self.markets.values()):
            self._evaluate_market(market, now)

    def _refresh_markets(self, now: float) -> None:
        found = self.gamma.discover(
            self.cfg.symbols,
            self.cfg.durations_minutes,
            self.cfg.duration_tolerance_seconds,
        )
        live = {}
        for m in found:
            if m.end_time > now:  # only keep unresolved markets
                live[m.condition_id] = m
        self.markets = live

    def _candle_open(self, market: CryptoMarket) -> Optional[float]:
        cid = market.condition_id
        if cid in self._candle_opens:
            return self._candle_opens[cid]
        price = self.feed.get_candle_open(market.symbol, market.start_time)
        if price:
            self._candle_opens[cid] = price
        return price

    def _evaluate_market(self, market: CryptoMarket, now: float) -> None:
        ttl = market.seconds_to_resolution(now)
        if ttl < self.cfg.min_seconds_to_resolution or ttl > self.cfg.max_seconds_to_resolution:
            return
        if self.risk.trades_per_market.get(market.condition_id, 0) >= self.cfg.max_trades_per_market:
            return

        spot = self.feed.get_price(market.symbol)
        if not spot:
            log.debug("no spot price for %s", market.symbol)
            return
        candle_open = self._candle_open(market)
        if not candle_open:
            log.debug("no candle open for %s", market.slug)
            return
        vol = self.feed.per_second_vol(market.symbol)

        try:
            up_q = self.gateway.get_quote(market.up_token_id)
            down_q = self.gateway.get_quote(market.down_token_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("book fetch failed for %s: %s", market.slug, exc)
            return

        sig = self.strategy.evaluate(market, candle_open, spot, vol, up_q, down_q, now)
        if sig is None:
            return

        reject = self.risk.can_enter(sig)
        if reject:
            log.debug("skip %s: %s", market.slug, reject)
            return

        avail = up_q.ask_size if sig.side.value == "UP" else down_q.ask_size
        sig = self.risk.size_signal(sig, available_size=avail)
        if sig.size <= 0:
            log.debug("skip %s: size rounds to 0", market.slug)
            return

        pos = self.executor.place(sig)
        if pos:
            self.risk.register_entry(pos)

    def _settle_expired(self, now: float) -> None:
        """Resolve paper positions whose market has ended, using the candle's
        realised direction from the price feed. Live settlement is on-chain."""
        if not isinstance(self.executor, PaperExecutor):
            return
        for pos in [p for p in self.risk.open_positions if p.open]:
            m = pos.market
            if now < m.end_time + 5:
                continue
            open_px = self._candle_opens.get(m.condition_id) or self.feed.get_candle_open(m.symbol, m.start_time)
            close_px = self.feed.get_candle_open(m.symbol, m.end_time) or self.feed.get_price(m.symbol)
            if not open_px or not close_px:
                continue
            resolved_up = close_px > open_px
            self.executor.settle(pos, resolved_up)
            self.risk.register_settlement(pos)

    # ----- reporting -------------------------------------------------------
    def _print_summary(self) -> None:
        closed = [p for p in self.risk.open_positions if not p.open]
        wins = sum(1 for p in closed if p.pnl > 0)
        total_pnl = sum(p.pnl for p in closed)
        log.info(
            "Session summary: %d entries, %d settled, %d wins, realized PnL=%.2f",
            len(self.risk.open_positions), len(closed), wins, total_pnl,
        )
