from polybot.config import Config
from polybot.simulate import run_simulation


def _cfg():
    c = Config()
    c.dry_run = True
    c.bankroll_usd = 200
    c.durations_minutes = [5, 15]
    return c


def test_simulation_runs_and_is_deterministic():
    cfg = _cfg()
    r1 = run_simulation(cfg, n_markets=40, seed=123)
    r2 = run_simulation(cfg, n_markets=40, seed=123)
    assert r1.n_markets == 40
    assert 0 <= r1.entries <= 40
    assert r1.wins + r1.losses <= r1.entries
    # deterministic given the same seed
    assert abs(r1.pnl - r2.pnl) < 1e-9
    assert r1.entries == r2.entries


def test_simulation_positive_edge_large_sample():
    # With the model matching the generator and a lagging book, edge is positive
    # at large N. This guards against a regression that flips the sign.
    cfg = _cfg()
    res = run_simulation(cfg, n_markets=1500, seed=7)
    assert res.entries > 500
    assert res.pnl > 0
