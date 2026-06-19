"""Candlestick pattern detection — pure functions over Candle series.

Each detector returns a direction string: "bullish", "bearish", or "" (none).
`detect_patterns` runs them all on the latest candle and returns a list of
``(name, direction)`` for everything that fired.
"""

from __future__ import annotations

from typing import List, Tuple

from .models import Candle


def doji(c: Candle) -> str:
    """Very small body relative to range — indecision (neutral → 'bullish' tag
    is not implied; we return 'neutral')."""
    if c.range <= 0:
        return ""
    return "neutral" if c.body_pct < 10.0 else ""


def momentum_candle(c: Candle) -> str:
    """Large body with small wicks — strong directional push."""
    if c.body_pct >= 70.0:
        return "bullish" if c.bullish else "bearish"
    return ""


def engulfing(prev: Candle, cur: Candle) -> str:
    """Current real body fully engulfs the previous body, opposite colour."""
    cur_top, cur_bot = max(cur.open, cur.close), min(cur.open, cur.close)
    prev_top, prev_bot = max(prev.open, prev.close), min(prev.open, prev.close)
    engulfs = cur_top >= prev_top and cur_bot <= prev_bot and cur.body > prev.body
    if not engulfs:
        return ""
    if cur.bullish and not prev.bullish:
        return "bullish"
    if not cur.bullish and prev.bullish:
        return "bearish"
    return ""


def pin_bar(c: Candle) -> str:
    """Long wick rejecting one side, small body (>= 2x body on the wick)."""
    if c.range <= 0 or c.body <= 0:
        return ""
    if c.lower_wick >= 2.0 * c.body and c.upper_wick <= c.body:
        return "bullish"   # long lower wick → rejection of lower prices
    if c.upper_wick >= 2.0 * c.body and c.lower_wick <= c.body:
        return "bearish"
    return ""


def inside_bar(prev: Candle, cur: Candle) -> str:
    """Current candle's range is inside the previous candle's range."""
    if cur.high < prev.high and cur.low > prev.low:
        return "neutral"
    return ""


def breakout(candles: List[Candle], lookback: int = 10) -> str:
    """Latest close breaks beyond the prior `lookback` candles' high/low."""
    if len(candles) < lookback + 1:
        return ""
    window = candles[-lookback - 1:-1]
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    cur = candles[-1]
    if cur.close > hi:
        return "bullish"
    if cur.close < lo:
        return "bearish"
    return ""


def detect_patterns(candles: List[Candle]) -> List[Tuple[str, str]]:
    """Detect patterns on the latest candle. Returns [(name, direction), ...]."""
    out: List[Tuple[str, str]] = []
    if not candles:
        return out
    cur = candles[-1]

    for name, direction in (
        ("doji", doji(cur)),
        ("momentum", momentum_candle(cur)),
        ("pin_bar", pin_bar(cur)),
        ("breakout", breakout(candles)),
    ):
        if direction:
            out.append((name, direction))

    if len(candles) >= 2:
        prev = candles[-2]
        for name, direction in (
            ("engulfing", engulfing(prev, cur)),
            ("inside_bar", inside_bar(prev, cur)),
        ):
            if direction:
                out.append((name, direction))
    return out
