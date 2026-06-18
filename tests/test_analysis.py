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


def test_rsi_overbought_blocks_long_entry():
    # strong uptrend pushes RSI high; with the guard on, an UP entry is refused
    a = analyze("BTC", "5m", _trend_candles("up"))
    assert a.rsi is not None and a.rsi >= 70
    opp = decide_opportunity(
        a, "BTC up or down 5m", 5, 120.0,
        candle_open=100.0, spot=100.6, vol_per_sec=0.0003,
        up_ask=0.55, down_ask=0.45, min_edge=0.04,
        avoid_rsi_extremes=True, rsi_overbought=70.0, rsi_oversold=20.0)
    assert opp.action == "no_trade"
    assert "overbought" in opp.reason.lower()
    # guard off -> the same setup trades
    opp2 = decide_opportunity(
        a, "BTC up or down 5m", 5, 120.0,
        candle_open=100.0, spot=100.6, vol_per_sec=0.0003,
        up_ask=0.55, down_ask=0.45, min_edge=0.04, avoid_rsi_extremes=False)
    assert opp2.action == "enter"
