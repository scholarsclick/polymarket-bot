"""Live crypto spot price feed with multiple exchange sources, candle-open
lookup, and a rolling realized-volatility estimator.

All sources are REST-polled for robustness. Sources are tried in the configured
order and the first that answers wins; failures fall through to the next.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

import requests

log = logging.getLogger("polybot.pricefeed")

SECONDS_PER_YEAR = 365 * 24 * 3600

# Map our symbols to each exchange's product naming.
_SYMBOL_MAP = {
    "binance": {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"},
    "coinbase": {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"},
    "kraken": {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD"},
}


class PriceFeed:
    """Polls spot prices and maintains per-symbol history for vol estimation."""

    def __init__(
        self,
        sources: List[str],
        vol_window_seconds: int = 300,
        vol_floor_annual: float = 0.40,
        vol_ceiling_annual: float = 2.50,
        timeout: float = 6.0,
    ):
        self.sources = sources
        self.vol_window_seconds = vol_window_seconds
        self.vol_floor_annual = vol_floor_annual
        self.vol_ceiling_annual = vol_ceiling_annual
        self.timeout = timeout
        self._history: Dict[str, Deque[Tuple[float, float]]] = {}
        self._session = requests.Session()

    # ----- spot price ------------------------------------------------------
    def get_price(self, symbol: str) -> Optional[float]:
        """Return the latest spot price, trying each source in order."""
        price = None
        for src in self.sources:
            try:
                price = self._fetch_spot(src, symbol)
                if price and price > 0:
                    break
            except Exception as exc:  # noqa: BLE001 — feed must never crash the loop
                log.debug("price source %s failed for %s: %s", src, symbol, exc)
        if price and price > 0:
            self._record(symbol, price)
        return price

    def _fetch_spot(self, source: str, symbol: str) -> Optional[float]:
        product = _SYMBOL_MAP.get(source, {}).get(symbol.upper())
        if not product:
            return None
        if source == "binance":
            r = self._session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": product}, timeout=self.timeout,
            )
            r.raise_for_status()
            return float(r.json()["price"])
        if source == "coinbase":
            r = self._session.get(
                f"https://api.exchange.coinbase.com/products/{product}/ticker",
                timeout=self.timeout, headers={"User-Agent": "polybot/0.1"},
            )
            r.raise_for_status()
            return float(r.json()["price"])
        if source == "kraken":
            r = self._session.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": product}, timeout=self.timeout,
            )
            r.raise_for_status()
            result = r.json()["result"]
            first = next(iter(result.values()))
            return float(first["c"][0])  # last trade closed price
        return None

    # ----- candle open -----------------------------------------------------
    def get_candle_open(self, symbol: str, open_time: float) -> Optional[float]:
        """Fetch the spot price at (or just after) `open_time` using exchange
        kline/candle history. Used as the market's reference open price."""
        for src in self.sources:
            try:
                p = self._fetch_candle_open(src, symbol, open_time)
                if p and p > 0:
                    return p
            except Exception as exc:  # noqa: BLE001
                log.debug("candle-open source %s failed for %s: %s", src, symbol, exc)
        return None

    def _fetch_candle_open(self, source: str, symbol: str, open_time: float) -> Optional[float]:
        product = _SYMBOL_MAP.get(source, {}).get(symbol.upper())
        if not product:
            return None
        if source == "binance":
            # 1-minute klines; pick the candle whose open == the minute of open_time.
            start_ms = int(open_time // 60 * 60 * 1000)
            r = self._session.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": product, "interval": "1m",
                        "startTime": start_ms, "limit": 1},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                return float(data[0][1])  # open of the candle
        if source == "coinbase":
            start = int(open_time // 60 * 60)
            r = self._session.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={"granularity": 60, "start": start, "end": start + 60},
                timeout=self.timeout, headers={"User-Agent": "polybot/0.1"},
            )
            r.raise_for_status()
            data = r.json()
            if data:
                # candle: [time, low, high, open, close, volume]
                data.sort(key=lambda c: c[0])
                return float(data[0][3])
        return None

    # ----- volatility ------------------------------------------------------
    def _record(self, symbol: str, price: float) -> None:
        now = time.time()
        hist = self._history.setdefault(symbol, deque())
        hist.append((now, price))
        cutoff = now - self.vol_window_seconds
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def per_second_vol(self, symbol: str) -> float:
        """Estimate per-second return volatility from the rolling window,
        clamped between the configured annual floor and ceiling."""
        floor = self.vol_floor_annual / math.sqrt(SECONDS_PER_YEAR)
        ceil = self.vol_ceiling_annual / math.sqrt(SECONDS_PER_YEAR)

        hist = self._history.get(symbol)
        if not hist or len(hist) < 5:
            return floor

        pts = list(hist)
        log_rets: List[float] = []
        dts: List[float] = []
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0 or p0 <= 0 or p1 <= 0:
                continue
            log_rets.append(math.log(p1 / p0))
            dts.append(dt)
        if len(log_rets) < 4:
            return floor

        mean = sum(log_rets) / len(log_rets)
        var = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
        avg_dt = sum(dts) / len(dts)
        if avg_dt <= 0:
            return floor
        per_sec = math.sqrt(var / avg_dt)  # scale variance to per-second
        return max(floor, min(ceil, per_sec))
