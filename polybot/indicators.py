"""Technical indicators — pure functions over price/candle series.

All functions return ``None`` when there is not enough data, and never raise on
short input, so callers can safely gate on availability.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .models import Candle


def ema_series(values: List[float], period: int) -> List[float]:
    """Exponential moving average series (seeded with the first value)."""
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return ema_series(values, period)[-1]


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> Optional[Tuple[float, float, float]]:
    """Return (macd_line, signal_line, histogram) for the latest bar."""
    if len(closes) < slow + signal:
        return None
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    macd_line = [a - b for a, b in zip(ef, es)]
    sig = ema_series(macd_line, signal)
    return macd_line[-1], sig[-1], macd_line[-1] - sig[-1]


def atr(candles: List[Candle], period: int = 14) -> Optional[float]:
    """Average True Range (Wilder)."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    val = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        val = (val * (period - 1) + trs[i]) / period
    return val


def volume_change(candles: List[Candle], lookback: int = 1) -> Optional[float]:
    """Percent change of the latest candle's volume vs `lookback` bars ago."""
    if len(candles) < lookback + 1:
        return None
    prev = candles[-1 - lookback].volume
    if prev <= 0:
        return None
    return (candles[-1].volume - prev) / prev * 100.0


def body_pct(candle: Candle) -> float:
    return candle.body_pct


def higher_high_lower_low(candles: List[Candle], lookback: int = 3) -> dict:
    """Market-structure flags comparing the latest candle to the prior window."""
    if len(candles) < lookback + 1:
        return {"higher_high": False, "lower_low": False,
                "higher_low": False, "lower_high": False}
    window = candles[-lookback - 1:-1]
    prior_high = max(c.high for c in window)
    prior_low = min(c.low for c in window)
    cur = candles[-1]
    return {
        "higher_high": cur.high > prior_high,
        "lower_low": cur.low < prior_low,
        "higher_low": cur.low > prior_low,
        "lower_high": cur.high < prior_high,
    }
