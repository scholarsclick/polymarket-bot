import math

from polybot.models import CryptoMarket, Quote, Side
from polybot.strategy import MomentumStrategy, fair_up_probability


def _market(end_time=1000.0):
    return CryptoMarket(
        condition_id="c1", question="Bitcoin Up or Down?", slug="bitcoin-up-or-down",
        symbol="BTC", up_token_id="UP", down_token_id="DOWN",
        start_time=end_time - 900, end_time=end_time, min_size=1.0,
    )


def test_fair_prob_symmetric_at_open():
    # spot == open, lots of time -> 50/50
    p = fair_up_probability(spot=100.0, candle_open=100.0, seconds_remaining=300, vol_per_sec=0.001)
    assert abs(p - 0.5) < 1e-9


def test_fair_prob_goes_to_one_when_up_and_no_time():
    p = fair_up_probability(spot=101.0, candle_open=100.0, seconds_remaining=0, vol_per_sec=0.001)
    assert p == 1.0
    p2 = fair_up_probability(spot=99.0, candle_open=100.0, seconds_remaining=0, vol_per_sec=0.001)
    assert p2 == 0.0


def test_fair_prob_monotonic_in_price():
    base = fair_up_probability(100.0, 100.0, 120, 0.0005)
    higher = fair_up_probability(100.5, 100.0, 120, 0.0005)
    lower = fair_up_probability(99.5, 100.0, 120, 0.0005)
    assert lower < base < higher


def test_momentum_buys_up_when_underpriced():
    strat = MomentumStrategy(min_edge=0.05, max_price=0.95, min_price=0.05)
    m = _market(end_time=1000.0)
    # Strong up move with little time -> fair_up near 1; ask cheap at 0.6 -> big edge.
    up_q = Quote(token_id="UP", best_bid=0.55, best_ask=0.60, ask_size=100)
    down_q = Quote(token_id="DOWN", best_bid=0.40, best_ask=0.45, ask_size=100)
    sig = strat.evaluate(
        m, candle_open=100.0, spot=100.4, vol_per_sec=0.0003,
        up_quote=up_q, down_quote=down_q, now=970.0,  # 30s left
    )
    assert sig is not None
    assert sig.side is Side.UP
    assert sig.edge >= 0.05


def test_momentum_no_trade_when_fairly_priced():
    strat = MomentumStrategy(min_edge=0.05)
    m = _market(end_time=1000.0)
    up_q = Quote(token_id="UP", best_bid=0.49, best_ask=0.51, ask_size=100)
    down_q = Quote(token_id="DOWN", best_bid=0.49, best_ask=0.51, ask_size=100)
    sig = strat.evaluate(
        m, candle_open=100.0, spot=100.0, vol_per_sec=0.0005,
        up_quote=up_q, down_quote=down_q, now=700.0,  # 300s left, 50/50
    )
    assert sig is None


def test_momentum_respects_max_price():
    strat = MomentumStrategy(min_edge=0.01, max_price=0.90)
    m = _market(end_time=1000.0)
    # near-certain up, but ask is 0.97 > max_price -> no trade
    up_q = Quote(token_id="UP", best_bid=0.96, best_ask=0.97, ask_size=100)
    down_q = Quote(token_id="DOWN", best_bid=0.02, best_ask=0.03, ask_size=100)
    sig = strat.evaluate(
        m, candle_open=100.0, spot=100.8, vol_per_sec=0.0002,
        up_quote=up_q, down_quote=down_q, now=985.0,  # 15s left
    )
    # Up is blocked by max_price; Down fair ~0 so no edge -> None
    assert sig is None
