"""Early-exit logic for open positions.

An open position can be closed before market resolution by selling the held
outcome token back into the book. `evaluate_exit` checks, in priority order:

  take_profit -> stop_loss -> trailing_stop -> time_exit ->
  confidence_exit -> volatility_exit

and returns the first that fires. `exit_levels` computes the price levels shown
on the dashboard (TP / SL / trailing).

All prices here are the **mark** = the price you could SELL the held token at
right now (its best bid). Unrealized PnL for a position of `size` shares bought
at `entry` is `size * (mark - entry)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ExitDecision:
    should_exit: bool
    type: str = ""
    reason: str = ""


def exit_levels(entry_price: float, peak_mark: Optional[float], cfg) -> dict:
    """Compute the trailing-stop price level for display. (Take-profit and
    stop-loss were removed; trailing stop locks gains dynamically.)"""
    trail = None
    if (getattr(cfg, "trailing_stop_pct", 0) and cfg.trailing_stop_pct > 0
            and peak_mark and peak_mark > entry_price):
        trail = peak_mark * (1 - cfg.trailing_stop_pct)
    return {"trail": trail}


def evaluate_exit(*, side: str, entry_price: float, mark: Optional[float],
                  peak_mark: Optional[float], seconds_left: float,
                  vol_per_sec: Optional[float], entry_vol: Optional[float],
                  trend: Optional[str], confidence: Optional[int], cfg) -> ExitDecision:
    if not getattr(cfg, "enable_early_exits", True) or mark is None or entry_price <= 0:
        return ExitDecision(False)

    gain = (mark - entry_price) / entry_price
    lv = exit_levels(entry_price, peak_mark, cfg)

    if lv["trail"] is not None and mark <= lv["trail"]:
        return ExitDecision(True, "trailing_stop",
                            f"trailing-stop: mark {mark:.3f} ≤ {lv['trail']:.3f} "
                            f"(peak {peak_mark:.3f})")

    if getattr(cfg, "time_exit_seconds", 0) and seconds_left <= cfg.time_exit_seconds:
        return ExitDecision(True, "time_exit",
                            f"time-exit: {seconds_left:.0f}s ≤ {cfg.time_exit_seconds:.0f}s "
                            f"before expiry (mark {mark:.3f})")

    if getattr(cfg, "confidence_exit", False) and trend and confidence is not None:
        opposed = ((side == "UP" and trend == "bearish")
                   or (side == "DOWN" and trend == "bullish"))
        if opposed and confidence >= getattr(cfg, "exit_confidence_min", 2):
            return ExitDecision(True, "confidence_exit",
                                f"confidence-exit: trend {trend} opposes {side} "
                                f"(conf {confidence})")

    if (getattr(cfg, "volatility_exit", False) and entry_vol and vol_per_sec
            and vol_per_sec >= entry_vol * getattr(cfg, "volatility_exit_mult", 3.0)
            and gain < 0):
        return ExitDecision(True, "volatility_exit",
                            f"volatility-exit: vol {vol_per_sec / entry_vol:.1f}× entry "
                            f"against position (loss {gain:+.0%})")

    return ExitDecision(False)


def confidence_buckets(closed_trades) -> list:
    """Performance grouped by entry confidence: <70, 70-79, 80-89, 90-100."""
    bands = [("<70", 0, 70), ("70-79", 70, 80), ("80-89", 80, 90), ("90-100", 90, 1e9)]
    out = []
    for label, lo, hi in bands:
        rows = [t for t in closed_trades if lo <= t.get("confidence_pct", 0) < hi]
        n = len(rows)
        wins = sum(1 for t in rows if t.get("result") == "win")
        pnl = sum(t.get("pnl", 0.0) for t in rows)
        out.append({"bucket": label, "count": n,
                    "win_rate": (wins / n) if n else 0.0, "pnl": pnl})
    return out


def exit_performance(closed_trades) -> list:
    """Aggregate closed trades by exit type: count, win rate, avg & total PnL."""
    buckets: dict = {}
    for t in closed_trades:
        et = t.get("exit_type", "resolution")
        b = buckets.setdefault(et, {"exit_type": et, "count": 0, "wins": 0,
                                    "pnl": 0.0})
        b["count"] += 1
        # a "win" means the prediction was correct (not merely that PnL > 0)
        b["wins"] += 1 if t.get("result") == "win" else 0
        b["pnl"] += t.get("pnl", 0.0)
    rows = []
    for b in buckets.values():
        b["win_rate"] = b["wins"] / b["count"] if b["count"] else 0.0
        b["avg_pnl"] = b["pnl"] / b["count"] if b["count"] else 0.0
        rows.append(b)
    rows.sort(key=lambda r: r["avg_pnl"], reverse=True)
    return rows
