"""Shared data structures used across the bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    """Which outcome token we are buying."""

    UP = "UP"      # "Up" / "Yes" token — pays out if price closed higher
    DOWN = "DOWN"  # "Down" / "No" token — pays out if price closed lower/equal


@dataclass
class CryptoMarket:
    """A single short-term crypto up/down market on Polymarket."""

    condition_id: str
    question: str
    slug: str
    symbol: str                 # e.g. "BTC"
    up_token_id: str            # CLOB token id for the "Up"/"Yes" outcome
    down_token_id: str          # CLOB token id for the "Down"/"No" outcome
    start_time: float           # unix seconds — candle open / market start
    end_time: float             # unix seconds — resolution time
    tick_size: float = 0.01
    min_size: float = 5.0
    neg_risk: bool = False

    @property
    def duration_seconds(self) -> float:
        return self.end_time - self.start_time

    @property
    def duration_minutes(self) -> int:
        return round(self.duration_seconds / 60)

    def token_id(self, side: Side) -> str:
        return self.up_token_id if side is Side.UP else self.down_token_id

    def seconds_to_resolution(self, now: float) -> float:
        return self.end_time - now


@dataclass
class BookSide:
    """One side of the book reduced to the best level we can trade against."""

    best_price: Optional[float] = None   # best price available
    best_size: Optional[float] = None    # size available at/around the best price


@dataclass
class Quote:
    """A snapshot of a token's order book."""

    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    ask_size: Optional[float] = None
    bid_size: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0


@dataclass
class Signal:
    """A trade decision produced by a strategy."""

    market: CryptoMarket
    side: Side
    token_id: str
    fair_value: float       # model probability the chosen side wins
    price: float            # limit price we'd pay (per share)
    edge: float             # fair_value - price (after fees)
    reason: str = ""

    # filled in by the risk manager
    size: float = 0.0       # number of shares
    notional: float = 0.0   # size * price (USD)


@dataclass
class Position:
    """An open or settled paper/live position."""

    market: CryptoMarket
    side: Side
    token_id: str
    size: float
    entry_price: float
    entry_time: float
    open: bool = True
    resolved_up: Optional[bool] = None
    pnl: float = 0.0

    @property
    def cost(self) -> float:
        return self.size * self.entry_price
