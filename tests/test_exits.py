from polybot.config import Config
from polybot.exits import ExitDecision, evaluate_exit, exit_levels, exit_performance
from polybot.runner import (_confidence_scale, _entry_quality_block,
                            make_early_closed_trade)


def _cfg(**kw):
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _eval(**kw):
    base = dict(side="UP", entry_price=0.50, mark=0.50, peak_mark=0.50,
                seconds_left=200, vol_per_sec=1e-6, entry_vol=1e-6,
                trend="bullish", confidence=3)
    base.update(kw)
    cfg = base.pop("cfg")
    return evaluate_exit(cfg=cfg, **base)


def test_tp_and_sl_removed():
    # Take-profit and stop-loss were removed; only big moves no longer auto-exit.
    cfg = _cfg(trailing_stop_pct=0, time_exit_seconds=0,
               confidence_exit=False, volatility_exit=False)
    assert not _eval(mark=0.99, cfg=cfg).should_exit   # would have been TP
    assert not _eval(mark=0.10, cfg=cfg).should_exit   # would have been SL
    assert not hasattr(cfg, "take_profit_price")
    assert not hasattr(cfg, "stop_loss_pct")


def test_trailing_stop():
    cfg = _cfg(trailing_stop_pct=0.10,
               time_exit_seconds=0, confidence_exit=False, volatility_exit=False)
    # peak 0.90 -> trail at 0.81
    assert _eval(mark=0.80, peak_mark=0.90, cfg=cfg).type == "trailing_stop"
    assert not _eval(mark=0.85, peak_mark=0.90, cfg=cfg).should_exit
    # never trails when never in profit (peak == entry)
    assert not _eval(mark=0.45, peak_mark=0.50, cfg=cfg).should_exit


def test_time_exit():
    cfg = _cfg(trailing_stop_pct=0,
               time_exit_seconds=30, confidence_exit=False, volatility_exit=False)
    assert _eval(seconds_left=25, cfg=cfg).type == "time_exit"
    assert not _eval(seconds_left=120, cfg=cfg).should_exit


def test_confidence_exit():
    cfg = _cfg(trailing_stop_pct=0,
               time_exit_seconds=0, confidence_exit=True, exit_confidence_min=2,
               volatility_exit=False)
    # long UP but trend flipped bearish with confidence 3
    assert _eval(side="UP", trend="bearish", confidence=3, cfg=cfg).type == "confidence_exit"
    # still bullish -> no exit
    assert not _eval(side="UP", trend="bullish", confidence=3, cfg=cfg).should_exit


def test_volatility_exit_only_when_losing():
    cfg = _cfg(trailing_stop_pct=0,
               time_exit_seconds=0, confidence_exit=False, volatility_exit=True,
               volatility_exit_mult=3.0)
    # vol 4x entry and position losing -> exit
    assert _eval(mark=0.45, vol_per_sec=4e-6, entry_vol=1e-6, cfg=cfg).type == "volatility_exit"
    # vol spike but position winning -> no exit
    assert not _eval(mark=0.60, vol_per_sec=4e-6, entry_vol=1e-6, cfg=cfg).should_exit


def test_priority_trailing_before_time():
    cfg = _cfg(trailing_stop_pct=0.10, time_exit_seconds=30)
    d = _eval(mark=0.80, peak_mark=0.90, seconds_left=10, cfg=cfg)
    assert d.type == "trailing_stop"   # trailing checked before time exit


def test_disabled_when_flag_off():
    cfg = _cfg(enable_early_exits=False, trailing_stop_pct=0.10, time_exit_seconds=30)
    assert not _eval(mark=0.40, peak_mark=0.90, seconds_left=5, cfg=cfg).should_exit


def test_exit_levels_only_trail():
    cfg = _cfg(trailing_stop_pct=0.10)
    lv = exit_levels(0.50, 0.90, cfg)
    assert abs(lv["trail"] - 0.81) < 1e-9
    assert "tp" not in lv and "sl" not in lv


def test_early_close_result_is_prediction_correctness():
    # Bought UP at 0.50, sold at 0.62 (PROFIT) but spot is BELOW the candle open
    # -> the prediction was WRONG, so it must count as a LOSS, not a win.
    t = {"entry_time": 100, "market": "BTC 5m", "symbol": "BTC", "duration_min": 5,
         "side": "UP", "size": 20, "entry_price": 0.50, "candle_open": 100.0,
         "end_time": 400, "reason_entry": "trend bullish"}
    rec = make_early_closed_trade(t, exit_price=0.62, spot_at_close=99.5, now=250,
                                  exit_type="trailing_stop", reason="trail")
    assert rec["pnl"] > 0                 # we made money
    assert rec["correct"] is False
    assert rec["result"] == "loss"        # but the prediction was wrong

    # profitable AND correct (spot above open) -> win
    win = make_early_closed_trade(t, 0.62, 100.4, 250, "time_exit", "time")
    assert win["correct"] is True and win["result"] == "win"


def test_exit_performance_counts_correctness_not_pnl():
    rows = [
        {"exit_type": "trailing_stop", "result": "loss", "pnl": 3.0},   # profit but wrong
        {"exit_type": "trailing_stop", "result": "win", "pnl": 5.0},
    ]
    perf = {r["exit_type"]: r for r in exit_performance(rows)}
    assert perf["trailing_stop"]["count"] == 2
    assert perf["trailing_stop"]["wins"] == 1          # only the correct one
    assert abs(perf["trailing_stop"]["pnl"] - 8.0) < 1e-9


def test_confidence_scale_scales_size():
    cfg = _cfg(confidence_sizing=True, exit_confidence_min=2,
               confidence_sizing_min_mult=0.5, confidence_sizing_max_mult=1.5)
    assert _confidence_scale(2, cfg) == 1.0     # baseline
    assert _confidence_scale(3, cfg) == 1.5     # stronger -> capped at max
    assert _confidence_scale(1, cfg) == 0.5     # weaker -> floored at min
    # disabled -> always 1.0
    assert _confidence_scale(5, _cfg(confidence_sizing=False)) == 1.0


def test_entry_quality_block_spread_and_liquidity():
    cfg = _cfg(max_spread=0.10, min_liquidity=100.0)
    assert _entry_quality_block(0.20, 500, cfg)            # wide spread -> blocked
    assert _entry_quality_block(0.02, 50, cfg)             # thin liquidity -> blocked
    assert _entry_quality_block(0.02, 500, cfg) is None    # good book -> allowed
