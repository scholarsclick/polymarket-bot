"""Trading strategies for short-term crypto up/down markets.

The default `MomentumStrategy` derives a fair probability that the candle
closes *up* from the live spot price vs. the candle open, using a zero-drift
lognormal model of the remaining price path, then trades against the order
book when the book price differs from fair by more than the configured edge.

Fair model
----------
Let r = ln(spot / open) be the log-return realised so far in the candle, and
let sigma_rem = vol_per_sec * sqrt(seconds_remaining) be the remaining
volatility. Future log-return X ~ N(0, sigma_rem^2). The candle closes up iff
r + X > 0, so

    P(up) = P(X > -r) = Phi(r / sigma_rem)

As time runs out sigma_rem -> 0 and P(up) collapses to 1 (if r>0) or 0.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from .models import CryptoMarket, Quote, Side, Signal

log = logging.getLogger("polybot.strategy")


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fair_up_probability(
    spot: float,
    candle_open: float,
    seconds_remaining: float,
    vol_per_sec: float,
) -> float:
    """Probability the candle closes up given current state. See module docstring."""
    if spot <= 0 or candle_open <= 0:
        return 0.5
    r = math.log(spot / candle_open)
    if seconds_remaining <= 0:
        return 1.0 if r > 0 else 0.0
    sigma_rem = vol_per_sec * math.sqrt(seconds_remaining)
    if sigma_rem <= 1e-12:
        return 1.0 if r > 0 else 0.0
    return _norm_cdf(r / sigma_rem)


class Strategy:
    """Strategy interface. Implement `evaluate` to produce a Signal or None."""

    name = "base"

    def evaluate(
        self,
        market: CryptoMarket,
        candle_open: float,
        spot: float,
        vol_per_sec: float,
        up_quote: Quote,
        down_quote: Quote,
        now: float,
    ) -> Optional[Signal]:
        raise NotImplementedError


class MomentumStrategy(Strategy):
    name = "momentum"

    def __init__(
        self,
        min_edge: float = 0.05,
        max_price: float = 0.95,
        min_price: float = 0.05,
        taker_fee_bps: float = 0.0,
    ):
        self.min_edge = min_edge
        self.max_price = max_price
        self.min_price = min_price
        self.fee = taker_fee_bps / 10_000.0

    def evaluate(
        self,
        market: CryptoMarket,
        candle_open: float,
        spot: float,
        vol_per_sec: float,
        up_quote: Quote,
        down_quote: Quote,
        now: float,
    ) -> Optional[Signal]:
        seconds_remaining = max(0.0, market.seconds_to_resolution(now))
        fair_up = fair_up_probability(spot, candle_open, seconds_remaining, vol_per_sec)
        fair_down = 1.0 - fair_up

        candidates = []
        for side, fair, quote in (
            (Side.UP, fair_up, up_quote),
            (Side.DOWN, fair_down, down_quote),
        ):
            ask = quote.best_ask
            if ask is None:
                continue
            # effective cost including taker fee
            cost = ask * (1.0 + self.fee)
            if cost < self.min_price or cost > self.max_price:
                continue
            edge = fair - cost
            if edge >= self.min_edge:
                candidates.append((edge, side, fair, ask))

        if not candidates:
            return None

        edge, side, fair, ask = max(candidates, key=lambda c: c[0])
        return Signal(
            market=market,
            side=side,
            token_id=market.token_id(side),
            fair_value=fair,
            price=ask,
            edge=edge,
            reason=(
                f"{market.symbol} {side.value}: fair={fair:.3f} ask={ask:.3f} "
                f"edge={edge:.3f} t_left={seconds_remaining:.0f}s "
                f"spot={spot:.2f} open={candle_open:.2f}"
            ),
        )


def build_strategy(name: str, cfg) -> Strategy:
    name = (name or "momentum").lower()
    if name == "momentum":
        return MomentumStrategy(
            min_edge=cfg.min_edge,
            max_price=cfg.max_price,
            min_price=cfg.min_price,
            taker_fee_bps=cfg.taker_fee_bps,
        )
    raise ValueError(f"Unknown strategy: {name!r}")
