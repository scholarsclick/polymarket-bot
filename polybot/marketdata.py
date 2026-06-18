"""Live market data: real spot prices and OHLC candles from public exchange
APIs (Binance, Coinbase), with health tracking and **no fake fallback**.

If every source fails, `get_spot` / `get_candles` return ``None`` and the
health record carries the error — callers must treat that as "data unavailable"
and stop trading, never substitute a synthetic price.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from .models import Candle

log = logging.getLogger("polybot.marketdata")

# our symbol -> exchange product id
_SPOT = {
    "binance": {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"},
    "coinbase": {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"},
}
# our timeframe -> exchange granularity
_BINANCE_TF = {"1m": "1m", "5m": "5m", "15m": "15m"}
_COINBASE_GRAN = {"1m": 60, "5m": 300, "15m": 900}


@dataclass
class FeedHealth:
    ok: bool = False
    source: Optional[str] = None
    last_ok_ts: float = 0.0
    last_error: Optional[str] = None
    last_attempt_ts: float = 0.0

    def mark_ok(self, source: str) -> None:
        self.ok = True
        self.source = source
        self.last_ok_ts = time.time()
        self.last_error = None
        self.last_attempt_ts = self.last_ok_ts

    def mark_fail(self, error: str) -> None:
        self.ok = False
        self.last_error = error
        self.last_attempt_ts = time.time()

    @property
    def age_seconds(self) -> Optional[float]:
        return (time.time() - self.last_ok_ts) if self.last_ok_ts else None


class MarketDataFeed:
    def __init__(self, sources: Optional[List[str]] = None, timeout: float = 6.0):
        self.sources = sources or ["binance", "coinbase"]
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polybot/0.2"})
        self.spot_health = FeedHealth()
        self.candle_health = FeedHealth()

    # ----- spot ------------------------------------------------------------
    def get_spot(self, symbol: str) -> Optional[float]:
        errors = []
        for src in self.sources:
            try:
                price = self._fetch_spot(src, symbol)
                if price and price > 0:
                    self.spot_health.mark_ok(src)
                    return price
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{src}: {exc}")
        self.spot_health.mark_fail("; ".join(errors) or "no source returned a price")
        return None

    def _fetch_spot(self, source: str, symbol: str) -> Optional[float]:
        product = _SPOT.get(source, {}).get(symbol.upper())
        if not product:
            return None
        if source == "binance":
            r = self._session.get("https://api.binance.com/api/v3/ticker/price",
                                  params={"symbol": product}, timeout=self.timeout)
            r.raise_for_status()
            return float(r.json()["price"])
        if source == "coinbase":
            r = self._session.get(
                f"https://api.exchange.coinbase.com/products/{product}/ticker",
                timeout=self.timeout)
            r.raise_for_status()
            return float(r.json()["price"])
        return None

    # ----- candles ---------------------------------------------------------
    def get_candles(self, symbol: str, timeframe: str, limit: int = 120
                    ) -> Optional[List[Candle]]:
        errors = []
        for src in self.sources:
            try:
                candles = self._fetch_candles(src, symbol, timeframe, limit)
                if candles:
                    self.candle_health.mark_ok(src)
                    return candles
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{src}: {exc}")
        self.candle_health.mark_fail("; ".join(errors) or "no source returned candles")
        return None

    def _fetch_candles(self, source: str, symbol: str, timeframe: str, limit: int
                       ) -> Optional[List[Candle]]:
        product = _SPOT.get(source, {}).get(symbol.upper())
        if not product:
            return None
        if source == "binance":
            interval = _BINANCE_TF.get(timeframe)
            if not interval:
                return None
            r = self._session.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": product, "interval": interval, "limit": limit},
                timeout=self.timeout)
            r.raise_for_status()
            out = []
            for k in r.json():
                out.append(Candle(
                    open_time=k[0] / 1000.0, open=float(k[1]), high=float(k[2]),
                    low=float(k[3]), close=float(k[4]), volume=float(k[5]),
                    close_time=k[6] / 1000.0))
            return out
        if source == "coinbase":
            gran = _COINBASE_GRAN.get(timeframe)
            if not gran:
                return None
            r = self._session.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={"granularity": gran}, timeout=self.timeout)
            r.raise_for_status()
            rows = r.json()  # [time, low, high, open, close, volume], newest first
            rows.sort(key=lambda c: c[0])
            out = []
            for c in rows[-limit:]:
                out.append(Candle(
                    open_time=float(c[0]), open=float(c[3]), high=float(c[2]),
                    low=float(c[1]), close=float(c[4]), volume=float(c[5]),
                    close_time=float(c[0]) + gran))
            return out
        return None

    # ----- helpers ---------------------------------------------------------
    def candle_open_at(self, candles: List[Candle], t: float) -> Optional[float]:
        """Open price of the candle covering time `t` (used as a market's
        reference open)."""
        best = None
        for c in candles:
            if c.open_time <= t and (c.close_time == 0 or t <= c.close_time + 1):
                best = c.open
        return best

    @property
    def healthy(self) -> bool:
        return self.spot_health.ok and self.candle_health.ok
