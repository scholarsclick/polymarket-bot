import time

from polybot.config import Config
from polybot.runner import BotRunner, make_closed_trade


def _open_trade(side="UP", entry_price=0.55, size=10, candle_open=100.0):
    return {
        "id": 1, "entry_time": 1000.0, "market": "BTC up or down 5m",
        "symbol": "BTC", "duration_min": 5, "side": side, "size": size,
        "entry_price": entry_price, "candle_open": candle_open,
        "end_time": 1300.0, "reason_entry": "trend bullish + edge",
    }


def test_close_logging_win_records_all_fields():
    t = _open_trade(side="UP", entry_price=0.55, size=10, candle_open=100.0)
    rec = make_closed_trade(t, exit_px=100.8, now=1305.0)  # closed UP -> UP wins
    for field in ("entry_time", "close_time", "market", "side", "entry_price",
                  "exit_price", "pnl", "result", "reason_entry", "reason_close"):
        assert field in rec
    assert rec["result"] == "win"
    assert abs(rec["pnl"] - (10 * 1.0 - 10 * 0.55)) < 1e-9   # +4.5
    assert rec["exit_price"] == 100.8
    assert "WON" in rec["reason_close"]
    assert "trend bullish" in rec["reason_entry"]


def test_close_logging_loss():
    t = _open_trade(side="UP", entry_price=0.6, size=10, candle_open=100.0)
    rec = make_closed_trade(t, exit_px=99.5, now=1305.0)    # closed DOWN -> UP loses
    assert rec["result"] == "loss"
    assert abs(rec["pnl"] - (-10 * 0.6)) < 1e-9             # -6.0
    assert "LOST" in rec["reason_close"]


def _cfg():
    c = Config()
    c.symbols = ["BTC", "ETH"]
    c.durations_minutes = [5, 15]
    c.bankroll_usd = 200
    return c


def test_paper_runner_produces_state():
    runner = BotRunner(_cfg(), mode="paper", time_scale=120.0)
    runner.start()
    try:
        # let the background sim spin up and settle a few markets
        deadline = time.time() + 8
        settled = 0
        while time.time() < deadline:
            time.sleep(0.5)
            s = runner.snapshot()
            settled = s.settled
            if settled > 0:
                break
        s = runner.snapshot()
        assert s.running is True
        assert len(s.scanned) == 5            # five concurrent markets scanned
        assert s.entries >= 1                  # at least one trade taken
        assert s.error is None
        assert len(s.equity_curve) >= 1
    finally:
        runner.stop()
    time.sleep(0.7)
    assert runner.snapshot().running is False


def test_live_runner_requires_key_for_real_orders():
    cfg = _cfg()
    cfg.private_key = None
    runner = BotRunner(cfg, mode="live", execute_orders=True)
    runner.start()
    time.sleep(0.5)
    s = runner.snapshot()
    runner.stop()
    # without a key, real-order execution must refuse and report an error
    assert s.error is not None
    assert "PRIVATE_KEY" in s.error
