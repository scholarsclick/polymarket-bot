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
    signal_strength: float = 0.0         # z-score of the candle displacement
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
    strict_mode: bool = False,
    min_confidence_pct: float = 80.0,
    model_prob: Optional[float] = None,
    min_fair: float = 0.55,
    min_signal_strength: float = 0.5,
) -> Opportunity:
    """Pick the side the price action favours (the candle's *leader*) and trade
    it only when it is a meaningful FAVOURITE (model prob >= min_fair) that the
    market is UNDERPRICING (edge >= min_edge). Betting favourites is what makes
    wins outnumber losses; the edge keeps it +EV. Technicals/indicators confirm
    and shape confidence (and gate entry in strict mode)."""
    fair_up = fair_up_probability(spot, candle_open, max(0.0, seconds_left), vol_per_sec)

    # 1) Side = the candle's current leader (the most-likely outcome).
    if fair_up >= 0.5:
        side, fair, ask = Side.UP, fair_up, up_ask
    else:
        side, fair, ask = Side.DOWN, 1.0 - fair_up, down_ask

    edge = (fair - ask) if ask is not None else 0.0
    confidence = abs(analysis.bull_score - analysis.bear_score)

    # 2) Signal strength: how significant is the displacement vs noise (z-score).
    elapsed = max(1.0, duration_min * 60 - seconds_left)
    denom = vol_per_sec * math.sqrt(elapsed)
    signal_strength = abs(math.log(spot / candle_open) / denom) if (denom > 0 and spot > 0 and candle_open > 0) else 0.0

    # 3) Confidence: favourite strength + edge + trend/indicator/pattern confirm.
    trend_agrees = ((side is Side.UP and analysis.trend == "bullish")
                    or (side is Side.DOWN and analysis.trend == "bearish"))
    trend_conflicts = ((side is Side.UP and analysis.trend == "bearish")
                       or (side is Side.DOWN and analysis.trend == "bullish"))
    up = side is Side.UP
    fav, opp_s = (analysis.bull_score, analysis.bear_score) if up else (analysis.bear_score, analysis.bull_score)
    ind_p = (fav / (fav + opp_s)) if (fav + opp_s) else 0.5
    pdir = analysis.pattern_direction()
    pat_p = 1.0 if pdir == ("bullish" if up else "bearish") else (0.5 if pdir == "neutral" else 0.0)

    def _c(x):
        return max(0.0, min(1.0, x))

    pct01 = (0.32 * _c((fair - 0.5) / 0.4)        # how strong a favourite
             + 0.22 * _c(edge / (2 * min_edge if min_edge else 0.1))
             + 0.18 * (1.0 if trend_agrees else (0.5 if not trend_conflicts else 0.0))
             + 0.13 * ind_p
             + 0.08 * pat_p
             + 0.07 * _c(signal_strength / 2.0))
    if model_prob is not None:
        pct01 = 0.6 * pct01 + 0.4 * model_prob
    pct = round(pct01 * 100, 1)

    pat_names = ", ".join(f"{n}:{d}" for n, d in analysis.patterns) or "none"
    base = (f"{analysis.symbol} {side.value} fair={fair:.2f} edge={edge:+.3f} "
            f"strength={signal_strength:.1f} conf={pct:.0f}% trend={analysis.trend} "
            f"[{', '.join(analysis.signals) or 'n/a'}] patterns:{pat_names} "
            f"spot={spot:,.2f} open={candle_open:,.2f} | {market_name}")

    # HARD blockers — always (the win-rate filters):
    hard = []
    if not analysis.ok or ask is None:
        hard.append("insufficient data")
    if fair < min_fair:
        hard.append(f"not a favourite ({fair:.2f}<{min_fair:.2f})")
    if edge < min_edge:
        hard.append("weak edge")

    # SOFT blockers — only gate entry in strict mode:
    soft = []
    if trend_conflicts:
        soft.append("trend conflicts")
    if signal_strength < min_signal_strength:
        soft.append("weak signal")
    if avoid_rsi_extremes and analysis.rsi is not None:
        if side is Side.UP and analysis.rsi >= rsi_overbought:
            soft.append(f"RSI overbought {analysis.rsi:.0f}")
        if side is Side.DOWN and analysis.rsi <= rsi_oversold:
            soft.append(f"RSI oversold {analysis.rsi:.0f}")
    if pct < min_confidence_pct:
        soft.append(f"low confidence {pct:.0f}%<{min_confidence_pct:.0f}%")

    blockers = hard + (soft if strict_mode else [])

    def _opp(action, reason):
        return Opportunity(analysis.symbol, market_name, duration_min, seconds_left,
                           action, side, fair, ask, edge, confidence, reason,
                           confidence_pct=pct, signal_strength=round(signal_strength, 2),
                           blockers=blockers)

    if not blockers:
        tag = "✅ confirmed favourite" if strict_mode else "✅ favourite + edge"
        return _opp("enter", f"{tag} — " + base)
    if edge > 0 and "insufficient data" not in blockers:
        return _opp("possible", "possible (" + ", ".join(blockers) + ") — " + base)
    return _opp("no_trade", "no trade (" + ", ".join(blockers) + ") — " + base)

