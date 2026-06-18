"""Self-learning layer: learn which signals actually predict correct trades.

Every closed trade contributes one training example — its entry features plus
the label "was the prediction correct" — to an online logistic-regression model.
The model then estimates P(correct) for new setups, which the engine uses to
gate and size entries. Until enough trades have been seen it stays silent and
the rule-based logic runs unchanged.

Pure Python (no sklearn/numpy needed); weights persist to JSON and the raw
training rows to JSONL so learning survives restarts.
"""

from __future__ import annotations

import json
import math
import os
from collections import deque
from typing import List, Optional

# Features are oriented toward the bet direction: positive = supports the bet,
# so the model learns "do supporting signals predict a correct trade?".
FEATURE_NAMES = ["trend", "rsi", "macd", "ema", "volume", "body",
                 "pattern", "edge", "time_left"]


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _sign(x: float) -> float:
    return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)


def _orient(val: float, side: str) -> float:
    return val if side == "UP" else -val


def featurize(analysis, side: str, edge: float, seconds_left: float,
              duration_min: int) -> List[float]:
    """Build the oriented feature vector for an entry. `analysis` is an
    Analysis object (has ema9/ema21/rsi/macd_hist/volume_change/body_pct/
    bull_score/bear_score/structure/pattern_direction())."""
    price = getattr(analysis, "price", 0.0) or 1.0
    trend = (analysis.bull_score - analysis.bear_score)
    rsi = analysis.rsi if analysis.rsi is not None else 50.0
    macd = analysis.macd_hist if analysis.macd_hist is not None else 0.0
    ema_gap = (analysis.ema9 - analysis.ema21) if (analysis.ema9 and analysis.ema21) else 0.0
    volch = analysis.volume_change if analysis.volume_change is not None else 0.0
    body = analysis.body_pct if analysis.body_pct is not None else 0.0

    pdir = analysis.pattern_direction()
    pat = 1.0 if pdir == "bullish" else (-1.0 if pdir == "bearish" else 0.0)
    struct = 0.0
    if analysis.structure.get("higher_high") and analysis.structure.get("higher_low"):
        struct = 1.0
    elif analysis.structure.get("lower_low") and analysis.structure.get("lower_high"):
        struct = -1.0
    pattern_feat = _orient(pat, side) * 0.5 + _orient(struct, side) * 0.5

    tf = (seconds_left / (duration_min * 60)) if duration_min else 0.0

    return [
        _clamp(_orient(trend, side) / 5.0),
        _clamp(_orient(rsi - 50.0, side) / 50.0),
        _orient(_sign(macd), side),
        _orient(_sign(ema_gap), side),
        _clamp(volch / 100.0),
        _clamp(body / 100.0, 0.0, 1.0),
        _clamp(pattern_feat),
        _clamp(edge * 5.0),
        _clamp(tf, 0.0, 1.0),
    ]


class OnlineLogReg:
    """Logistic regression trained by SGD, one sample at a time."""

    def __init__(self, n_features: int, lr: float = 0.05, l2: float = 1e-4):
        self.w = [0.0] * n_features
        self.b = 0.0
        self.lr = lr
        self.l2 = l2

    def _z(self, x: List[float]) -> float:
        return sum(wi * xi for wi, xi in zip(self.w, x)) + self.b

    def predict_proba(self, x: List[float]) -> float:
        z = max(-30.0, min(30.0, self._z(x)))
        return 1.0 / (1.0 + math.exp(-z))

    def update(self, x: List[float], y: int) -> None:
        p = self.predict_proba(x)
        err = p - y
        for i, xi in enumerate(x):
            self.w[i] -= self.lr * (err * xi + self.l2 * self.w[i])
        self.b -= self.lr * err

    def to_dict(self) -> dict:
        return {"w": self.w, "b": self.b}

    def load_dict(self, d: dict) -> None:
        if d.get("w") and len(d["w"]) == len(self.w):
            self.w = list(d["w"])
            self.b = float(d.get("b", 0.0))


class TradeLearner:
    """Persistence + online model + prequential accuracy tracking."""

    def __init__(self, state_dir: str = "state", lr: float = 0.05,
                 min_samples: int = 25):
        self.min_samples = min_samples
        self.model = OnlineLogReg(len(FEATURE_NAMES), lr=lr)
        self.n = 0
        self._acc = deque(maxlen=200)   # recent prequential correctness of the model
        os.makedirs(state_dir, exist_ok=True)
        self.log_path = os.path.join(state_dir, "trade_log.jsonl")
        self.model_path = os.path.join(state_dir, "model.json")
        self._load()

    # ----- inference -------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self.n >= self.min_samples

    def prob(self, features: List[float]) -> Optional[float]:
        """P(trade correct), or None until the model has enough data."""
        if not self.ready:
            return None
        return self.model.predict_proba(features)

    # ----- learning --------------------------------------------------------
    def record_outcome(self, features: List[float], correct: bool) -> None:
        y = 1 if correct else 0
        # prequential: score the model's pre-update prediction
        pred = 1 if self.model.predict_proba(features) >= 0.5 else 0
        self._acc.append(1 if pred == y else 0)
        self.model.update(features, y)
        self.n += 1
        self._append_log(features, y)
        self._save_model()

    def _append_log(self, features: List[float], y: int) -> None:
        try:
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps({"x": features, "y": y}) + "\n")
        except OSError:
            pass

    def _save_model(self) -> None:
        try:
            with open(self.model_path, "w") as fh:
                json.dump({"model": self.model.to_dict(), "n": self.n}, fh)
        except OSError:
            pass

    def _load(self) -> None:
        # warm-start weights, then replay the log to refresh n + accuracy
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path) as fh:
                    d = json.load(fh)
                self.model.load_dict(d.get("model", {}))
            except (OSError, json.JSONDecodeError):
                pass
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path) as fh:
                    rows = [json.loads(ln) for ln in fh if ln.strip()]
                self.n = len(rows)
            except (OSError, json.JSONDecodeError):
                pass

    # ----- reporting -------------------------------------------------------
    def stats(self) -> dict:
        acc = (sum(self._acc) / len(self._acc)) if self._acc else None
        weights = sorted(zip(FEATURE_NAMES, self.model.w),
                         key=lambda kv: abs(kv[1]), reverse=True)
        return {
            "samples": self.n,
            "ready": self.ready,
            "min_samples": self.min_samples,
            "recent_accuracy": acc,
            "weights": [{"feature": f, "weight": round(w, 3)} for f, w in weights],
        }
