import random

from polybot.analysis import analyze
from polybot.learning import (FEATURE_NAMES, OnlineLogReg, TradeLearner, featurize)
from polybot.models import Candle


def _trend_candles(direction="up", n=60, start=100.0, step=1.0):
    closes, px = [], start
    for _ in range(n):
        px += step if direction == "up" else -step
        closes.append(px)
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle(open_time=i * 60, open=o, high=max(o, c) + 0.2,
                          low=min(o, c) - 0.2, close=c, volume=100 + i,
                          close_time=i * 60 + 60))
    return out


def test_featurize_shape_and_orientation():
    a = analyze("BTC", "5m", _trend_candles("up"))
    fu = featurize(a, "UP", edge=0.06, seconds_left=120, duration_min=5)
    fd = featurize(a, "DOWN", edge=0.06, seconds_left=120, duration_min=5)
    assert len(fu) == len(FEATURE_NAMES)
    # bullish context supports UP (trend feature positive) and opposes DOWN
    assert fu[0] > 0 and fd[0] < 0
    assert all(-1.0001 <= v <= 1.0001 for v in fu)


def test_logreg_learns_separable_signal():
    m = OnlineLogReg(n_features=2, lr=0.2)
    rng = random.Random(0)
    for _ in range(2000):
        # label depends on feature 0: positive -> 1
        x0 = rng.uniform(-1, 1)
        x = [x0, rng.uniform(-1, 1)]
        y = 1 if x0 > 0 else 0
        m.update(x, y)
    assert m.predict_proba([0.9, 0.0]) > 0.7
    assert m.predict_proba([-0.9, 0.0]) < 0.3


def test_learner_cold_start_then_ready(tmp_path):
    lr = TradeLearner(str(tmp_path), min_samples=10)
    feats = [0.8] + [0.0] * (len(FEATURE_NAMES) - 1)
    assert lr.prob(feats) is None          # cold start -> no opinion
    for _ in range(12):
        lr.record_outcome(feats, correct=True)
    assert lr.ready
    assert lr.prob(feats) is not None
    st = lr.stats()
    assert st["samples"] == 12
    assert st["recent_accuracy"] is not None
    assert len(st["weights"]) == len(FEATURE_NAMES)


def test_learner_persists_across_restart(tmp_path):
    feats = [0.5] * len(FEATURE_NAMES)
    a = TradeLearner(str(tmp_path), min_samples=5)
    for _ in range(8):
        a.record_outcome(feats, correct=True)
    # new instance reads the log + model back
    b = TradeLearner(str(tmp_path), min_samples=5)
    assert b.n == 8 and b.ready
    assert b.prob(feats) is not None


def test_learner_separates_good_from_bad_setups(tmp_path):
    lr = TradeLearner(str(tmp_path), min_samples=20)
    good = [0.9] + [0.0] * (len(FEATURE_NAMES) - 1)
    bad = [-0.9] + [0.0] * (len(FEATURE_NAMES) - 1)
    for _ in range(150):
        lr.record_outcome(good, correct=True)
        lr.record_outcome(bad, correct=False)
    assert lr.prob(good) > lr.prob(bad)
    assert lr.prob(good) > 0.6
