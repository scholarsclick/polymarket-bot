from polybot.config import Config
from polybot.models import CryptoMarket, Position, Side, Signal
from polybot.risk import RiskManager


def _cfg(**kw):
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _market(cid="c1"):
    return CryptoMarket(
        condition_id=cid, question="BTC Up or Down?", slug="btc",
        symbol="BTC", up_token_id="UP", down_token_id="DOWN",
        start_time=0, end_time=900, min_size=1.0,
    )


def _signal(price=0.5, fair=0.7, market=None):
    m = market or _market()
    return Signal(market=m, side=Side.UP, token_id="UP", fair_value=fair, price=price, edge=fair - price)


def test_kelly_sizing_positive_edge():
    cfg = _cfg(bankroll_usd=1000, kelly_fraction=0.5, max_position_usd=1000,
               max_total_exposure_usd=10000)
    rm = RiskManager(cfg)
    sig = rm.size_signal(_signal(price=0.5, fair=0.7))
    # kelly = (0.7-0.5)/(1-0.5)=0.4 ; half-kelly=0.2 ; notional=200 ; size=400
    # (size is floored to whole shares, so allow off-by-one from float rounding)
    assert sig.size in (399, 400)
    assert abs(sig.notional - 200) <= 0.5


def test_max_position_cap():
    cfg = _cfg(bankroll_usd=1000, kelly_fraction=1.0, max_position_usd=25,
               max_total_exposure_usd=10000)
    rm = RiskManager(cfg)
    sig = rm.size_signal(_signal(price=0.5, fair=0.9))
    assert sig.notional <= 25 + 1e-6


def test_min_order_size_zeroed():
    cfg = _cfg(bankroll_usd=1000, kelly_fraction=0.001, max_position_usd=1000,
               max_total_exposure_usd=10000)
    rm = RiskManager(cfg)
    m = _market()
    m.min_size = 50
    sig = rm.size_signal(_signal(price=0.5, fair=0.55, market=m))
    assert sig.size == 0


def test_exposure_limit_blocks_new_size():
    cfg = _cfg(bankroll_usd=1000, kelly_fraction=1.0, max_position_usd=1000,
               max_total_exposure_usd=50)
    rm = RiskManager(cfg)
    # pretend we already hold $50 of exposure
    pos = Position(market=_market("x"), side=Side.UP, token_id="UP", size=100, entry_price=0.5, entry_time=0)
    rm.open_positions.append(pos)
    assert abs(rm.current_exposure() - 50) < 1e-6
    sig = rm.size_signal(_signal(price=0.5, fair=0.9))
    assert sig.size == 0


def test_consecutive_losses_halt():
    cfg = _cfg(max_consecutive_losses=2)
    rm = RiskManager(cfg)
    for _ in range(2):
        p = Position(market=_market(), side=Side.UP, token_id="UP", size=10, entry_price=0.5, entry_time=0)
        p.pnl = -5
        rm.open_positions.append(p)
        rm.register_settlement(p)
    assert rm.halted() is not None
    assert rm.can_enter(_signal()) is not None


def test_daily_loss_limit_halt():
    cfg = _cfg(daily_loss_limit_usd=10, daily_loss_limit_pct=0.0)  # use the USD limit
    rm = RiskManager(cfg)
    p = Position(market=_market(), side=Side.UP, token_id="UP", size=20, entry_price=0.5, entry_time=0)
    p.pnl = -11
    rm.open_positions.append(p)
    rm.register_settlement(p)
    assert rm.halted() is not None


def test_max_trades_per_market():
    cfg = _cfg(max_trades_per_market=1)
    rm = RiskManager(cfg)
    m = _market("dup")
    pos = Position(market=m, side=Side.UP, token_id="UP", size=10, entry_price=0.5, entry_time=0)
    rm.register_entry(pos)
    assert rm.can_enter(_signal(market=m)) == "already traded this market"


def test_daily_loss_limit_pct_halts_at_50pct():
    cfg = _cfg(bankroll_usd=200.0, daily_loss_limit_pct=0.5)
    rm = RiskManager(cfg)
    assert rm.daily_loss_limit() == 100.0          # 50% of 200
    rm.realized_pnl_today = -99.0
    assert rm.halted() is None                      # not yet
    rm.realized_pnl_today = -100.0
    assert rm.halted() is not None                  # hit -50% -> stop
    assert "stopped for the day" in rm.halted()


def test_daily_loss_limit_pct_disabled_falls_back_to_usd():
    cfg = _cfg(bankroll_usd=200.0, daily_loss_limit_pct=0.0, daily_loss_limit_usd=30.0)
    rm = RiskManager(cfg)
    assert rm.daily_loss_limit() == 30.0
    rm.realized_pnl_today = -31.0
    assert rm.halted() is not None
