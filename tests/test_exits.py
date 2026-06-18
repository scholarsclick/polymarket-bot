from polybot.config import Config
from polybot.exits import ExitDecision, evaluate_exit, exit_levels, exit_performance
from polybot.runner import make_early_closed_trade


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


def test_take_profit_price():
    cfg = _cfg(take_profit_price=0.92, stop_loss_pct=0, trailing_stop_pct=0,
               time_exit_seconds=0, confidence_exit=False, volatility_exit=False)
    d = _eval(mark=0.95, cfg=cfg)
    assert d.should_exit and d.type == "take_profit"
    assert not _eval(mark=0.80, cfg=cfg).should_exit


def test_take_profit_pct():
    cfg = _cfg(take_profit_price=0, take_profit_pct=0.5, stop_loss_pct=0,
               trailing_stop_pct=0, time_exit_seconds=0,
               confidence_exit=False, volatility_exit=False)
    # entry 0.50, +50% -> 0.75
    assert _eval(mark=0.76, cfg=cfg).type == "take_profit"
    assert not _eval(mark=0.70, cfg=cfg).should_exit


def test_stop_loss_pct():
    cfg = _cfg(take_profit_price=0, stop_loss_pct=0.30, trailing_stop_pct=0,
               time_exit_seconds=0, confidence_exit=False, volatility_exit=False)
    # entry 0.50, -30% -> 0.35
    d = _eval(mark=0.34, cfg=cfg)
    assert d.should_exit and d.type == "stop_loss"
    assert not _eval(mark=0.40, cfg=cfg).should_exit


def test_trailing_stop():
    cfg = _cfg(take_profit_price=0, stop_loss_pct=0, trailing_stop_pct=0.10,
               time_exit_seconds=0, confidence_exit=False, volatility_exit=False)
    # peak 0.90 -> trail at 0.81
    assert _eval(mark=0.80, peak_mark=0.90, cfg=cfg).type == "trailing_stop"
    assert not _eval(mark=0.85, peak_mark=0.90, cfg=cfg).should_exit
    # never trails when never in profit (peak == entry)
    assert not _eval(mark=0.45, peak_mark=0.50, cfg=cfg).should_exit


def test_time_exit():
    cfg = _cfg(take_profit_price=0, stop_loss_pct=0, trailing_stop_pct=0,
               time_exit_seconds=30, confidence_exit=False, volatility_exit=False)
    assert _eval(seconds_left=25, cfg=cfg).type == "time_exit"
    assert not _eval(seconds_left=120, cfg=cfg).should_exit


def test_confidence_exit():
    cfg = _cfg(take_profit_price=0, stop_loss_pct=0, trailing_stop_pct=0,
               time_exit_seconds=0, confidence_exit=True, exit_confidence_min=2,
               volatility_exit=False)
    # long UP but trend flipped bearish with confidence 3
    assert _eval(side="UP", trend="bearish", confidence=3, cfg=cfg).type == "confidence_exit"
    # still bullish -> no exit
    assert not _eval(side="UP", trend="bullish", confidence=3, cfg=cfg).should_exit


def test_volatility_exit_only_when_losing():
    cfg = _cfg(take_profit_price=0, stop_loss_pct=0, trailing_stop_pct=0,
               time_exit_seconds=0, confidence_exit=False, volatility_exit=True,
               volatility_exit_mult=3.0)
    # vol 4x entry and position losing -> exit
    assert _eval(mark=0.45, vol_per_sec=4e-6, entry_vol=1e-6, cfg=cfg).type == "volatility_exit"
    # vol spike but position winning -> no exit
    assert not _eval(mark=0.60, vol_per_sec=4e-6, entry_vol=1e-6, cfg=cfg).should_exit


def test_priority_tp_before_time():
    cfg = _cfg(take_profit_price=0.92, time_exit_seconds=30)
    d = _eval(mark=0.95, seconds_left=10, cfg=cfg)
    assert d.type == "take_profit"   # TP checked before time exit


def test_disabled_when_flag_off():
    cfg = _cfg(enable_early_exits=False, take_profit_price=0.92)
    assert not _eval(mark=0.99, cfg=cfg).should_exit


def test_exit_levels():
    cfg = _cfg(take_profit_price=0.92, stop_loss_pct=0.30, trailing_stop_pct=0.10)
    lv = exit_levels(0.50, 0.90, cfg)
    assert lv["tp"] == 0.92
    assert abs(lv["sl"] - 0.35) < 1e-9
    assert abs(lv["trail"] - 0.81) < 1e-9


def test_early_close_record_and_performance():
    t = {"entry_time": 100, "market": "BTC 5m", "symbol": "BTC", "duration_min": 5,
         "side": "UP", "size": 20, "entry_price": 0.50, "candle_open": 100.0,
         "end_time": 400, "reason_entry": "trend bullish"}
    rec = make_early_closed_trade(t, exit_price=0.95, now=250, exit_type="take_profit",
                                  reason="TP hit")
    assert rec["exit_type"] == "take_profit"
    assert rec["close_price"] == 0.95
    assert abs(rec["pnl"] - 20 * (0.95 - 0.50)) < 1e-9    # +9.0
    assert rec["result"] == "win"

    loss = make_early_closed_trade({**t, "entry_price": 0.60}, 0.40, 250, "stop_loss", "SL")
    perf = exit_performance([rec, loss])
    by = {r["exit_type"]: r for r in perf}
    assert by["take_profit"]["wins"] == 1
    assert by["stop_loss"]["count"] == 1
    assert abs(by["take_profit"]["avg_pnl"] - 9.0) < 1e-9
