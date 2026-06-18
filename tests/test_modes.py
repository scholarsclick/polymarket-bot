"""Mode separation: Live Data Paper Trading must NOT use the simulator, and the
simulator must NEVER produce live prices (req 1, 2, 9, 11)."""

import time

from polybot.config import load_config
from polybot.runner import BotRunner


def _cfg():
    cfg = load_config("config.example.yaml")
    cfg.symbols = ["BTC", "ETH"]
    return cfg


def test_simulator_test_mode_marks_simulator_active_true():
    r = BotRunner(_cfg(), mode="paper", time_scale=120.0)
    r.start()
    try:
        time.sleep(2)
        s = r.snapshot()
        assert s.simulator_active is True
        assert s.debug.get("simulator_active") is True
    finally:
        r.stop()


def test_live_paper_trading_never_uses_simulator():
    # Offline here, so the feed fails -> trading paused, but crucially the
    # simulator must stay OFF and no synthetic prices may appear.
    r = BotRunner(_cfg(), mode="live", execute_orders=False, timeframe="5m")
    r.start()
    try:
        deadline = time.time() + 8
        s = r.snapshot()
        while time.time() < deadline:
            time.sleep(0.5)
            s = r.snapshot()
            if s.debug:
                break
        assert s.simulator_active is False            # the key guarantee
        assert s.debug.get("simulator_active") is False
        assert s.data_available is False               # offline -> paused
        assert s.prices == {}                          # NO fake BTC at 100k
        assert s.warning and "paused" in s.warning
    finally:
        r.stop()


def test_live_real_orders_requires_key():
    cfg = _cfg()
    cfg.private_key = None
    r = BotRunner(cfg, mode="live", execute_orders=True)
    r.start()
    time.sleep(0.5)
    s = r.snapshot()
    r.stop()
    assert s.error and "PRIVATE_KEY" in s.error
