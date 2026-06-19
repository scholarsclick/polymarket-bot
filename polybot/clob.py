"""Thin wrapper around py-clob-client: auth, order books, and order placement."""

from __future__ import annotations

import logging
from typing import Optional

from .config import Config
from .models import Quote

log = logging.getLogger("polybot.clob")


class ClobGateway:
    """Wraps the official ClobClient. Read-only methods work without keys;
    order placement requires a funded, authenticated account."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = None
        self._authed = False

    @property
    def authed(self) -> bool:
        return self._authed

    def connect(self, require_auth: bool) -> None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        kwargs = dict(host=self.cfg.clob_host, chain_id=self.cfg.chain_id)
        if self.cfg.private_key:
            kwargs["key"] = self.cfg.private_key
            kwargs["signature_type"] = self.cfg.signature_type
            if self.cfg.funder:
                kwargs["funder"] = self.cfg.funder

        self._client = ClobClient(**kwargs)

        if not self.cfg.private_key:
            if require_auth:
                raise RuntimeError(
                    "Live trading needs POLYMARKET_PRIVATE_KEY set in .env"
                )
            log.info("CLOB connected read-only (no private key).")
            return

        # Level-2 API credentials: use provided, else derive from the key.
        if self.cfg.has_api_creds:
            creds = ApiCreds(
                api_key=self.cfg.api_key,
                api_secret=self.cfg.api_secret,
                api_passphrase=self.cfg.api_passphrase,
            )
        else:
            creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)
        self._authed = True
        log.info("CLOB authenticated as %s", self._safe_address())

    def _safe_address(self) -> str:
        try:
            return self._client.get_address()
        except Exception:  # noqa: BLE001
            return "<unknown>"

    # ----- market data -----------------------------------------------------
    def get_quote(self, token_id: str) -> Quote:
        """Best bid/ask snapshot for a token."""
        book = self._client.get_order_book(token_id)
        best_bid = best_ask = None
        bid_size = ask_size = None
        # bids are sorted ascending, asks ascending — take extremes safely.
        if getattr(book, "bids", None):
            top_bid = max(book.bids, key=lambda o: float(o.price))
            best_bid, bid_size = float(top_bid.price), float(top_bid.size)
        if getattr(book, "asks", None):
            top_ask = min(book.asks, key=lambda o: float(o.price))
            best_ask, ask_size = float(top_ask.price), float(top_ask.size)
        return Quote(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            ask_size=ask_size,
            bid_size=bid_size,
        )

    def get_tick_size(self, token_id: str) -> float:
        try:
            return float(self._client.get_tick_size(token_id))
        except Exception:  # noqa: BLE001
            return 0.01

    def get_neg_risk(self, token_id: str) -> bool:
        try:
            return bool(self._client.get_neg_risk(token_id))
        except Exception:  # noqa: BLE001
            return False

    # ----- order placement -------------------------------------------------
    def buy_marketable(self, token_id: str, price: float, size: float) -> dict:
        """Place a marketable BUY limit (Fill-And-Kill) at `price` for `size`
        shares. Returns the raw post_order response."""
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        tick = self.get_tick_size(token_id)
        neg_risk = self.get_neg_risk(token_id)
        price = round_to_tick(price, tick)

        order = self._client.create_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=BUY),
            PartialCreateOrderOptions(tick_size=_tick_str(tick), neg_risk=neg_risk),
        )
        return self._client.post_order(order, OrderType.FAK)

    def sell_marketable(self, token_id: str, price: float, size: float) -> dict:
        """Place a marketable SELL limit (Fill-And-Kill) at `price` for `size`
        shares — used to close a position early. Returns the post_order response."""
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import SELL

        tick = self.get_tick_size(token_id)
        neg_risk = self.get_neg_risk(token_id)
        price = round_to_tick(price, tick)

        order = self._client.create_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=SELL),
            PartialCreateOrderOptions(tick_size=_tick_str(tick), neg_risk=neg_risk),
        )
        return self._client.post_order(order, OrderType.FAK)

    def balance_allowance(self):
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        return self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 6)


def _tick_str(tick: float) -> str:
    # py-clob-client expects one of "0.1","0.01","0.001","0.0001"
    for t in ("0.1", "0.01", "0.001", "0.0001"):
        if abs(tick - float(t)) < 1e-9:
            return t
    return "0.01"
