import time

from polybot.config import Config
from polybot.runner import BotRunner


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
