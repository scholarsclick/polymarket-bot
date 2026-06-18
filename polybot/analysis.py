"""Trend analysis and opportunity decision.

`analyze` turns a candle series into an `Analysis` (indicators + patterns +
trend). `decide_opportunity` compares that read against a Polymarket market's
YES/NO prices and the lognormal fair value to produce a trade decision with a
human-readable reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import indicators as ind
from . import patterns as pat
from .models import Candle, Side
from .strategy import fair_up_probability


@dataclass
class Analysis:
    symbol: str
    timeframe: str
    price: float                      # latest close / spot
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    atr: Optional[float] = None
    volume_change: Optional[float] = None
    body_pct: Optional[float] = None
    structure: Dict[str, bool] = field(default_factory=dict)
    patterns: List[Tuple[str, str]] = field(default_factory=list)
    trend: str = "neutral"            # bullish | bearish | neutral
    bull_score: int = 0
    bear_score: int = 0
    signals: List[str] = field(default_factory=list)
    ok: bool = False                  # enough data to be usable

    @property
    def bias(self) -> str:
        return self.trend

    def pattern_direction(self) -> str:
        bull = sum(1 for _, d in self.patterns if d == "bullish")
        bear = sum(1 for _, d in self.patterns if d == "bearish")
        if bull > bear:
            return "bullish"
        if bear > bull:
            return "bearish"
        return "neutral"


def analyze(symbol: str, timeframe: str, candles: List[Candle]) -> Analysis:
    """Compute indicators, patterns and an overall trend from candles."""
    price = candles[-1].close if candles else 0.0
    a = Analysis(symbol=symbol, timeframe=timeframe, price=price)
    closes = [c.close for c in candles]
    if len(candles) < 30:
        a.ok = False
        return a

    a.ema9 = ind.ema(closes, 9)
    a.ema21 = ind.ema(closes, 21)
    a.rsi = ind.rsi(closes, 14)
    m = ind.macd(closes)
    if m:
        a.macd, a.macd_signal, a.macd_hist = m
    a.atr = ind.atr(candles, 14)
    a.volume_change = ind.volume_change(candles)
    a.body_pct = ind.body_pct(candles[-1])
    a.structure = ind.higher_high_lower_low(candles)
    a.patterns = pat.detect_patterns(candles)
    a.ok = a.ema9 is not None and a.ema21 is not None and a.rsi is not None

    # ----- score the trend from confluence of signals --------------------
    bull, bear, signals = 0, 0, []
    if a.ema9 is not None and a.ema21 is not None:
        if a.ema9 > a.ema21:
            bull += 1; signals.append("EMA9>EMA21")
        elif a.ema9 < a.ema21:
            bear += 1; signals.append("EMA9<EMA21")
    if a.ema9 is not None:
        if price > a.ema9:
            bull += 1; signals.append("price>EMA9")
        else:
            bear += 1; signals.append("price<EMA9")
    if a.macd_hist is not None:
        if a.macd_hist > 0:
            bull += 1; signals.append("MACD+")
        elif a.macd_hist < 0:
            bear += 1; signals.append("MACD-")
    if a.rsi is not None:
        if a.rsi >= 55:
            bull += 1; signals.append(f"RSI {a.rsi:.0f}")
        elif a.rsi <= 45:
            bear += 1; signals.append(f"RSI {a.rsi:.0f}")
    if a.structure.get("higher_high") and a.structure.get("higher_low"):
        bull += 1; signals.append("HH/HL")
    if a.structure.get("lower_low") and a.structure.get("lower_high"):
        bear += 1; signals.append("LL/LH")
    pdir = a.pattern_direction()
    if pdir == "bullish":
        bull += 1; signals.append("bull pattern")
    elif pdir == "bearish":
        bear += 1; signals.append("bear pattern")

    a.bull_score, a.bear_score, a.signals = bull, bear, signals
    if bull - bear >= 2:
        a.trend = "bullish"
    elif bear - bull >= 2:
        a.trend = "bearish"
    else:
        a.trend = "neutral"
    return a


@dataclass
class Opportunity:
    symbol: str
    market_name: str
    duration_min: int
    seconds_left: float
    action: str            # "enter" | "possible" | "no_trade"
    side: Optional[Side]
    fair: float
    market_price: Optional[float]
    edge: float
    confidence: int        # net confluence score
    reason: str


def decide_opportunity(
    analysis: Analysis,
    market_name: str,
    duration_min: int,
    seconds_left: float,
    candle_open: float,
    spot: float,
    vol_per_sec: float,
    up_ask: Optional[float],
    down_ask: Optional[float],
    min_edge: float,
    min_confidence: int = 2,
) -> Opportunity:
    """Combine trend + indicators + pattern with the market's YES/NO price.

    Returns an Opportunity whose `action` is:
      - "enter"     trend/indicators/pattern agree AND edge >= min_edge
      - "possible"  direction agrees but edge is thin / data marginal
      - "no_trade"  weak edge or no directional agreement
    """
    fair_up = fair_up_probability(spot, candle_open, max(0.0, seconds_left), vol_per_sec)

    # directional bias from the analysis
    if analysis.trend == "bullish":
        side, fair, ask = Side.UP, fair_up, up_ask
    elif analysis.trend == "bearish":
        side, fair, ask = Side.DOWN, 1.0 - fair_up, down_ask
    else:
        return Opportunity(analysis.symbol, market_name, duration_min, seconds_left,
                           "no_trade", None, fair_up, up_ask, 0.0,
                           analysis.bull_score - analysis.bear_score,
                           "trend neutral — no directional agreement")

    confidence = abs(analysis.bull_score - analysis.bear_score)
    edge = (fair - ask) if ask is not None else 0.0
    pat_names = ", ".join(f"{n}:{d}" for n, d in analysis.patterns) or "none"
    base = (f"{analysis.symbol} {analysis.trend} "
            f"[{', '.join(analysis.signals) or 'n/a'}] | patterns: {pat_names} | "
            f"spot={spot:,.2f} open={candle_open:,.2f} "
            f"fair={fair:.3f} ask={ask if ask is not None else float('nan'):.3f} "
            f"edge={edge:+.3f} | {market_name}")

    if not analysis.ok or ask is None:
        return Opportunity(analysis.symbol, market_name, duration_min, seconds_left,
                           "no_trade", side, fair, ask, edge, confidence,
                           "insufficient data / no book — " + base)

    if confidence >= min_confidence and edge >= min_edge:
        return Opportunity(analysis.symbol, market_name, duration_min, seconds_left,
                           "enter", side, fair, ask, edge, confidence,
                           "AGREE trend+indicators+market — " + base)
    if confidence >= min_confidence and edge > 0:
        return Opportunity(analysis.symbol, market_name, duration_min, seconds_left,
                           "possible", side, fair, ask, edge, confidence,
                           "possible (edge thin) — " + base)
    return Opportunity(analysis.symbol, market_name, duration_min, seconds_left,
                       "no_trade", side, fair, ask, edge, confidence,
                       "no trade (weak edge) — " + base)
