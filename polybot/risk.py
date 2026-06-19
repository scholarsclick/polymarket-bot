"""Position sizing and risk limits."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .config import Config
from .models import Position, Signal

log = logging.getLogger("polybot.risk")


class RiskManager:
    """Sizes orders (fractional Kelly) and enforces exposure / loss limits."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.open_positions: List[Position] = []
        self.trades_per_market: Dict[str, int] = {}
        self.realized_pnl_today: float = 0.0
        self.consecutive_losses: int = 0

    # ----- gating ----------------------------------------------------------
    def daily_loss_limit(self) -> float:
        """Absolute daily loss limit in USD: a percent of capital if set,
        otherwise the fixed dollar limit."""
        pct = getattr(self.cfg, "daily_loss_limit_pct", 0.0)
        if pct and pct > 0:
            return pct * self.cfg.bankroll_usd
        return abs(self.cfg.daily_loss_limit_usd)

    def halted(self) -> Optional[str]:
        """Return a reason string if new entries are halted, else None."""
        limit = self.daily_loss_limit()
        if limit > 0 and self.realized_pnl_today <= -limit:
            return (f"daily loss limit reached: {self.realized_pnl_today:+.2f} "
                    f"≤ -{limit:.2f} — stopped for the day")
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return f"{self.consecutive_losses} consecutive losses"
        return None

    def current_exposure(self) -> float:
        return sum(p.cost for p in self.open_positions if p.open)

    def can_enter(self, signal: Signal) -> Optional[str]:
        """Return a rejection reason, or None if the entry is allowed."""
        halt = self.halted()
        if halt:
            return halt
        n_open = sum(1 for p in self.open_positions if p.open)
        if self.cfg.max_open_positions and n_open >= self.cfg.max_open_positions:
            return f"max open positions ({n_open})"
        cid = signal.market.condition_id
        if self.trades_per_market.get(cid, 0) >= self.cfg.max_trades_per_market:
            return "already traded this market"
        return None

    def can_enter_basic(self) -> Optional[str]:
        """Signal-independent gate: halted state and open-position cap only."""
        halt = self.halted()
        if halt:
            return halt
        n_open = sum(1 for p in self.open_positions if p.open)
        if self.cfg.max_open_positions and n_open >= self.cfg.max_open_positions:
            return f"max open positions ({n_open})"
        return None

    # ----- sizing ----------------------------------------------------------
    def size_signal(self, signal: Signal, available_size: Optional[float] = None,
                    confidence_scale: float = 1.0) -> Signal:
        """Fill in `size` and `notional` on the signal using fractional Kelly,
        clamped by per-trade, total-exposure, and book-liquidity caps.
        `confidence_scale` (default 1.0) scales the Kelly notional by how strong
        the signal is."""
        price = signal.price
        fair = signal.fair_value
        if price <= 0 or price >= 1:
            signal.size = 0.0
            return signal

        # Kelly fraction for a binary bet costing `price`, paying 1, win prob `fair`:
        #   f* = (fair - price) / (1 - price)
        kelly = max(0.0, (fair - price) / (1.0 - price))
        bet_fraction = min(1.0, kelly * self.cfg.kelly_fraction)
        kelly_notional = bet_fraction * self.cfg.bankroll_usd * max(0.0, confidence_scale)

        # caps (max_total_exposure_usd <= 0 means unlimited)
        notional = min(kelly_notional, self.cfg.max_position_usd)
        if self.cfg.max_total_exposure_usd and self.cfg.max_total_exposure_usd > 0:
            remaining_exposure = self.cfg.max_total_exposure_usd - self.current_exposure()
            notional = min(notional, max(0.0, remaining_exposure))

        size = notional / price if price > 0 else 0.0

        # respect book liquidity if known
        if available_size is not None:
            size = min(size, available_size)

        # respect exchange minimum order size
        min_size = max(signal.market.min_size, 0.0)
        if size < min_size:
            size = 0.0

        size = float(int(size))  # whole shares
        signal.size = size
        signal.notional = size * price
        return signal

    # ----- bookkeeping -----------------------------------------------------
    def register_entry(self, position: Position) -> None:
        self.open_positions.append(position)
        cid = position.market.condition_id
        self.trades_per_market[cid] = self.trades_per_market.get(cid, 0) + 1

    def register_settlement(self, position: Position) -> None:
        position.open = False
        self.realized_pnl_today += position.pnl
        if position.pnl < 0:
            self.consecutive_losses += 1
        elif position.pnl > 0:
            self.consecutive_losses = 0
