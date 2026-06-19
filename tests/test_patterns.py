from polybot import patterns as pat
from polybot.models import Candle


def c(o, h, l, cl, v=100.0, t=0):
    return Candle(open_time=t, open=o, high=h, low=l, close=cl, volume=v, close_time=t + 60)


def test_bullish_engulfing():
    prev = c(10, 10.2, 9.5, 9.6)      # small bearish
    cur = c(9.5, 11.0, 9.4, 10.9)     # large bullish engulfing
    assert pat.engulfing(prev, cur) == "bullish"


def test_bearish_engulfing():
    prev = c(9.6, 10.2, 9.5, 10.0)    # bullish
    cur = c(10.1, 10.2, 9.0, 9.1)     # large bearish engulfing
    assert pat.engulfing(prev, cur) == "bearish"


def test_pin_bar_bullish_long_lower_wick():
    # small body up top, long lower wick
    cur = c(10.0, 10.1, 9.0, 10.05)
    assert pat.pin_bar(cur) == "bullish"


def test_pin_bar_bearish_long_upper_wick():
    cur = c(10.0, 11.0, 9.95, 9.97)
    assert pat.pin_bar(cur) == "bearish"


def test_inside_bar():
    prev = c(10, 11, 9, 10.5)
    cur = c(10.2, 10.8, 9.5, 10.4)
    assert pat.inside_bar(prev, cur) == "neutral"


def test_doji():
    cur = c(10.0, 10.5, 9.5, 10.02)   # tiny body
    assert pat.doji(cur) == "neutral"


def test_momentum_candle():
    cur = c(10.0, 11.05, 9.98, 11.0)  # big body, small wicks
    assert pat.momentum_candle(cur) == "bullish"


def test_breakout_bullish():
    candles = [c(10, 10.5, 9.5, 10) for _ in range(11)]
    candles.append(c(10, 12, 9.9, 11.8))  # closes above prior highs
    assert pat.breakout(candles) == "bullish"


def test_detect_patterns_returns_list():
    prev = c(10, 10.2, 9.5, 9.6)
    cur = c(9.5, 11.0, 9.4, 10.9)
    found = pat.detect_patterns([prev, cur])
    names = [n for n, _ in found]
    assert "engulfing" in names
