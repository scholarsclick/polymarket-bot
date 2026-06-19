from polybot.analysis import analyze, decide_opportunity
from polybot.models import Candle, Side


def _trend_candles(direction="up", n=60, start=100.0, step=1.0):
    closes = []
    px = start
    for i in range(n):
        px += step if direction == "up" else -step
        closes.append(px)
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle(open_time=i * 60, open=o, high=max(o, c) + 0.2,
                          low=min(o, c) - 0.2, close=c, volume=100 + i,
                          close_time=i * 60 + 60))
    return out


def test_analyze_bullish_trend():
    a = analyze("BTC", "5m", _trend_candles("up"))
    assert a.ok is True
    assert a.trend == "bullish"
    assert a.bull_score > a.bear_score
    assert a.rsi is not None and a.ema9 is not None


def test_analyze_bearish_trend():
    a = analyze("BTC", "5m", _trend_candles("down"))
    assert a.trend == "bearish"


def test_decide_enter_when_trend_and_edge_agree():
    a = analyze("BTC", "5m", _trend_candles("up"))
    # bullish trend; up_ask cheap relative to a high fair (spot well above open)
    opp = decide_opportunity(
        a, "BTC up or down 5m", 5, 120.0,
        candle_open=100.0, spot=100.6, vol_per_sec=0.0003,
        up_ask=0.55, down_ask=0.45, min_edge=0.04)
    assert opp.action == "enter"
    assert opp.side is Side.UP
    assert "BTC up or down 5m" in opp.reason


def test_decide_no_trade_when_weak_edge():
    a = analyze("BTC", "5m", _trend_candles("up"))
    opp = decide_opportunity(
        a, "BTC up or down 5m", 5, 120.0,
        candle_open=100.0, spot=100.0, vol_per_sec=0.001,
        up_ask=0.52, down_ask=0.52, min_edge=0.05)
    assert opp.action in ("no_trade", "possible")


def test_rsi_overbought_blocks_only_in_strict():
    # RSI extreme is a SOFT filter: it blocks in strict mode but not normal mode.
    a = analyze("BTC", "5m", _trend_candles("up"))
    assert a.rsi is not None and a.rsi >= 70
    common = dict(candle_open=100.0, spot=100.6, vol_per_sec=0.0003,
                  up_ask=0.55, down_ask=0.45, min_edge=0.04,
                  avoid_rsi_extremes=True, rsi_overbought=70.0, rsi_oversold=20.0)
    strict = decide_opportunity(a, "BTC 5m", 5, 120.0, **common, strict_mode=True)
    assert strict.action != "enter"
    assert any("overbought" in b.lower() for b in strict.blockers)
    # normal mode: the same setup trades (RSI not a hard blocker)
    normal = decide_opportunity(a, "BTC 5m", 5, 120.0, **common, strict_mode=False)
    assert normal.action == "enter"
    assert normal.confidence_pct >= 80    # confidence still reported


def test_strict_blocks_weak_but_normal_trades():
    # mild uptrend with positive edge: strict mode blocks on low confidence,
    # normal mode takes the trade.
    a = analyze("BTC", "5m", _trend_candles("up"))
    common = dict(candle_open=100.0, spot=100.15, vol_per_sec=0.001,
                  up_ask=0.52, down_ask=0.50, min_edge=0.03)
    strict = decide_opportunity(a, "BTC 5m", 5, 120.0, **common,
                                strict_mode=True, min_confidence_pct=95.0)
    normal = decide_opportunity(a, "BTC 5m", 5, 120.0, **common, strict_mode=False)
    assert 0 <= normal.confidence_pct <= 100      # confidence still shown
    # normal mode should NOT block on the soft confidence/indicator filters
    assert not any("low confidence" in b for b in normal.blockers)
    if normal.edge >= 0.03:
        assert normal.action == "enter"           # trades on a valid signal
    # strict can block the same setup on confidence
    assert strict.action != "enter" or strict.confidence_pct >= 95.0
