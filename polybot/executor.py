"""Order execution: a paper (simulated) executor and a live CLOB executor."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from .clob import ClobGateway
from .models import Position, Side, Signal

log = logging.getLogger("polybot.executor")


class Executor:
    def place(self, signal: Signal) -> Optional[Position]:
        raise NotImplementedError


class PaperExecutor(Executor):
    """Simulates immediate fills at the signal's limit price and journals them."""

    def __init__(self, state_dir: str = "state"):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.journal_path = os.path.join(state_dir, "paper_journal.jsonl")

    def place(self, signal: Signal) -> Optional[Position]:
        if signal.size <= 0:
            return None
        pos = Position(
            market=signal.market,
            side=signal.side,
            token_id=signal.token_id,
            size=signal.size,
            entry_price=signal.price,
            entry_time=time.time(),
        )
        self._journal("ENTER", signal, pos)
        log.info(
            "[PAPER] BUY %s %.0f @ %.3f (%s) — %s",
            signal.side.value, signal.size, signal.price,
            f"${signal.notional:.2f}", signal.reason,
        )
        return pos

    def settle(self, pos: Position, resolved_up: bool) -> None:
        won = (resolved_up and pos.side is Side.UP) or (not resolved_up and pos.side is Side.DOWN)
        payoff = pos.size * (1.0 if won else 0.0)
        pos.pnl = payoff - pos.cost
        pos.resolved_up = resolved_up
        pos.open = False
        self._journal("SETTLE", None, pos, extra={"resolved_up": resolved_up, "won": won})
        log.info(
            "[PAPER] SETTLE %s %s — pnl=%.2f (%s)",
            pos.market.symbol, pos.side.value, pos.pnl,
            "WIN" if won else "LOSS",
        )

    def _journal(self, event: str, signal: Optional[Signal], pos: Position, extra: dict = None) -> None:
        rec = {
            "ts": time.time(),
            "event": event,
            "symbol": pos.market.symbol,
            "slug": pos.market.slug,
            "side": pos.side.value,
            "size": pos.size,
            "entry_price": pos.entry_price,
            "pnl": pos.pnl,
        }
        if signal is not None:
            rec.update({"fair": signal.fair_value, "edge": signal.edge})
        if extra:
            rec.update(extra)
        try:
            with open(self.journal_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            log.debug("journal write failed: %s", exc)


class LiveExecutor(Executor):
    """Places real Fill-And-Kill orders via the CLOB. Settlement is handled by
    Polymarket on-chain; we only track the entry locally for stats."""

    def __init__(self, gateway: ClobGateway):
        self.gw = gateway

    def place(self, signal: Signal) -> Optional[Position]:
        if signal.size <= 0:
            return None
        try:
            resp = self.gw.buy_marketable(signal.token_id, signal.price, signal.size)
        except Exception as exc:  # noqa: BLE001
            log.error("Live order failed (%s): %s", signal.reason, exc)
            return None

        success = bool(resp.get("success", True)) if isinstance(resp, dict) else True
        if not success:
            log.error("Order rejected: %s", resp)
            return None

        log.info(
            "[LIVE] BUY %s %.0f @ %.3f — %s | resp=%s",
            signal.side.value, signal.size, signal.price, signal.reason,
            resp.get("orderID", resp) if isinstance(resp, dict) else resp,
        )
        return Position(
            market=signal.market,
            side=signal.side,
            token_id=signal.token_id,
            size=signal.size,
            entry_price=signal.price,
            entry_time=time.time(),
        )
