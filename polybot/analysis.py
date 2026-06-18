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
    confidence: int        # net confluence score (integer)
    reason: str
    confidence_pct: float = 0.0          # 0..100 combined confidence
    blockers: list = None                # active blockers (why it's not an enter)

    def __post_init__(self):
        if self.blockers is None:
            self.blockers = []


def confidence_components(analysis: Analysis, side: Side, edge: float, min_edge: float):
    """Return (pillars dict, pct 0..1) for the four agreement pillars."""
    up = side is Side.UP
    fav = analysis.bull_score if up else analysis.bear_score
    opp = analysis.bear_score if up else analysis.bull_score
    total = fav + opp

    trend_p = 1.0 if analysis.trend == ("bullish" if up else "bearish") else 0.0
    ind_p = (fav / total) if total else 0.5
    pdir = analysis.pattern_direction()
    if pdir == ("bullish" if up else "bearish"):
        pat_p = 1.0
    elif pdir == "neutral":
        pat_p = 0.5
    else:
        pat_p = 0.0
    edge_p = max(0.0, min(1.0, edge / (2 * min_edge))) if min_edge > 0 else (1.0 if edge > 0 else 0.0)

    pct = 0.30 * trend_p + 0.30 * ind_p + 0.15 * pat_p + 0.25 * edge_p
    return {"trend": trend_p, "indicators": ind_p, "pattern": pat_p, "edge": edge_p}, pct


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
    avoid_rsi_extremes: bool = False,
    rsi_overbought: float = 80.0,
    rsi_oversold: float = 20.0,
    high_confidence_only: bool = True,
    min_confidence_pct: float = 80.0,
    model_prob: Optional[float] = None,
) -> Opportunity:
    """Quality-gated decision. Enters ONLY when trend + indicators + pattern +
    market-price edge agree and the combined confidence clears the bar; lists
    every active blocker otherwise. There is no trade-count target — weak setups
    are simply skipped."""
    fair_up = fair_up_probability(spot, candle_open, max(0.0, seconds_left), vol_per_sec)

    if analysis.trend == "bullish":
        side, fair, ask = Side.UP, fair_up, up_ask
    elif analysis.trend == "bearish":
        side, fair, ask = Side.DOWN, 1.0 - fair_up, down_ask
    else:
        return Opportunity(analysis.symbol, market_name, duration_min, seconds_left,
                           "no_trade", None, fair_up, up_ask, 0.0,
                           analysis.bull_score - analysis.bear_score,
                           "neutral / choppy — no directional agreement",
                           confidence_pct=0.0, blockers=["neutral/choppy"])

    confidence = abs(analysis.bull_score - analysis.bear_score)
    edge = (fair - ask) if ask is not None else 0.0
    pillars, pct01 = confidence_components(analysis, side, edge, min_edge)
    if model_prob is not None:
        pct01 = 0.5 * pct01 + 0.5 * model_prob     # blend in the learned model
    pct = round(pct01 * 100, 1)

    pat_names = ", ".join(f"{n}:{d}" for n, d in analysis.patterns) or "none"
    base = (f"{analysis.symbol} {analysis.trend} conf={pct:.0f}% "
            f"[{', '.join(analysis.signals) or 'n/a'}] patterns:{pat_names} "
            f"spot={spot:,.2f} open={candle_open:,.2f} edge={edge:+.3f} | {market_name}")

    blockers = []
    if not analysis.ok or ask is None:
        blockers.append("insufficient data")
    if pillars["indicators"] < 0.5 or pillars["pattern"] == 0.0:
        blockers.append("conflicting indicators")
    if avoid_rsi_extremes and analysis.rsi is not None:
        if side is Side.UP and analysis.rsi >= rsi_overbought:
            blockers.append(f"RSI overbought {analysis.rsi:.0f}")
        if side is Side.DOWN and analysis.rsi <= rsi_oversold:
            blockers.append(f"RSI oversold {analysis.rsi:.0f}")
    if edge < min_edge:
        blockers.append("weak edge")
    if high_confidence_only and pct < min_confidence_pct:
        blockers.append(f"low confidence {pct:.0f}%<{min_confidence_pct:.0f}%")

    def _opp(action, reason):
        return Opportunity(analysis.symbol, market_name, duration_min, seconds_left,
                           action, side, fair, ask, edge, confidence, reason,
                           confidence_pct=pct, blockers=blockers)

    if not blockers:
        return _opp("enter", "✅ trend+indicators+pattern+edge agree — " + base)
    if edge > 0 and "conflicting indicators" not in blockers and "insufficient data" not in blockers:
        return _opp("possible", "possible (" + ", ".join(blockers) + ") — " + base)
    return _opp("no_trade", "no trade (" + ", ".join(blockers) + ") — " + base)

