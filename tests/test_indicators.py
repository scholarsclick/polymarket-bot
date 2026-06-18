from polybot import indicators as ind
from polybot.models import Candle


def _candles(closes, vol=100.0):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        hi = max(o, c) + 0.5
        lo = min(o, c) - 0.5
        out.append(Candle(open_time=i * 60, open=o, high=hi, low=lo, close=c,
                          volume=vol + i, close_time=i * 60 + 60))
    return out


def test_ema_none_when_short():
    assert ind.ema([1, 2], 9) is None


def test_ema_tracks_level():
    e = ind.ema([10] * 30, 9)
    assert abs(e - 10) < 1e-6


def test_rsi_high_on_uptrend_low_on_downtrend():
    up = list(range(1, 40))
    down = list(range(40, 1, -1))
    assert ind.rsi([float(x) for x in up]) > 70
    assert ind.rsi([float(x) for x in down]) < 30


def test_rsi_none_when_short():
    assert ind.rsi([1, 2, 3]) is None


def test_macd_returns_triplet():
    closes = [float(x) for x in range(1, 60)]
    m = ind.macd(closes)
    assert m is not None and len(m) == 3
    # rising series -> macd line above signal -> positive histogram
    assert m[2] > 0


def test_atr_positive():
    cs = _candles([float(x) for x in range(1, 40)])
    a = ind.atr(cs)
    assert a is not None and a > 0


def test_volume_change_sign():
    cs = _candles([1.0, 2.0, 3.0])  # volumes 100,101,102 -> increasing
    assert ind.volume_change(cs) > 0


def test_structure_flags_higher_high():
    cs = _candles([1.0, 2.0, 3.0, 4.0, 5.0])
    s = ind.higher_high_lower_low(cs)
    assert s["higher_high"] and s["higher_low"]
    assert not s["lower_low"]
