"""Discover short-term crypto up/down markets via Polymarket's Gamma API."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

import requests

from .models import CryptoMarket

log = logging.getLogger("polybot.gamma")

# Map free-text market questions to a canonical symbol.
_SYMBOL_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
}

_UP_OUTCOMES = {"up", "yes", "higher"}
_DOWN_OUTCOMES = {"down", "no", "lower"}

# Questions for these markets look like "Bitcoin Up or Down — ..." / "... higher?"
_UPDOWN_RE = re.compile(r"up or down|higher or lower|\bup\b.*\bdown\b", re.IGNORECASE)


def _parse_iso(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        # Gamma returns e.g. "2026-06-17T15:15:00Z"
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _detect_symbol(text: str, allowed: List[str]) -> Optional[str]:
    low = text.lower()
    for sym in allowed:
        for kw in _SYMBOL_KEYWORDS.get(sym.upper(), [sym.lower()]):
            if re.search(rf"\b{re.escape(kw)}\b", low):
                return sym.upper()
    return None


def _as_list(raw) -> List:
    """Gamma sometimes returns list fields as JSON-encoded strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except json.JSONDecodeError:
            return []
    return []


class GammaClient:
    def __init__(self, host: str = "https://gamma-api.polymarket.com", timeout: float = 8.0):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def _get_markets(self, limit: int = 500, offset: int = 0) -> List[dict]:
        r = self._session.get(
            f"{self.host}/markets",
            params={
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": limit,
                "offset": offset,
                "order": "endDate",
                "ascending": "true",
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])

    def parse_market(
        self,
        raw: dict,
        symbols: List[str],
        durations_minutes: List[int],
        duration_tolerance_seconds: int,
    ) -> Optional[CryptoMarket]:
        """Turn a raw Gamma market dict into a CryptoMarket, or None if it
        is not a short-term crypto up/down market we trade."""
        question = (raw.get("question") or raw.get("title") or "").strip()
        slug = raw.get("slug", "")
        if not question:
            return None
        if not _UPDOWN_RE.search(question) and "up-or-down" not in slug:
            return None

        symbol = _detect_symbol(f"{question} {slug}", symbols)
        if not symbol:
            return None

        token_ids = [str(t) for t in _as_list(raw.get("clobTokenIds"))]
        outcomes = [str(o) for o in _as_list(raw.get("outcomes"))]
        if len(token_ids) != 2 or len(outcomes) != 2:
            return None

        # Identify which token is the "Up" outcome.
        up_idx = None
        for i, oc in enumerate(outcomes):
            if oc.strip().lower() in _UP_OUTCOMES:
                up_idx = i
                break
        if up_idx is None:
            for i, oc in enumerate(outcomes):
                if oc.strip().lower() in _DOWN_OUTCOMES:
                    up_idx = 1 - i
                    break
        if up_idx is None:
            return None
        down_idx = 1 - up_idx

        end_time = _parse_iso(raw.get("endDate") or raw.get("endDateIso"))
        start_time = _parse_iso(raw.get("startDate") or raw.get("startDateIso"))
        if end_time is None:
            return None
        if start_time is None and end_time is not None:
            # Infer from the closest configured duration if start missing.
            start_time = end_time - min(durations_minutes) * 60

        duration_min = round((end_time - start_time) / 60)
        if not _matches_duration(end_time - start_time, durations_minutes, duration_tolerance_seconds):
            return None

        try:
            tick = float(raw.get("orderPriceMinTickSize") or raw.get("minTickSize") or 0.01)
        except (TypeError, ValueError):
            tick = 0.01
        try:
            min_size = float(raw.get("orderMinSize") or raw.get("minimumOrderSize") or 5.0)
        except (TypeError, ValueError):
            min_size = 5.0

        return CryptoMarket(
            condition_id=raw.get("conditionId") or raw.get("condition_id") or slug,
            question=question,
            slug=slug,
            symbol=symbol,
            up_token_id=token_ids[up_idx],
            down_token_id=token_ids[down_idx],
            start_time=start_time,
            end_time=end_time,
            tick_size=tick,
            min_size=min_size,
            neg_risk=bool(raw.get("negRisk", False)),
        )

    def discover(
        self,
        symbols: List[str],
        durations_minutes: List[int],
        duration_tolerance_seconds: int,
        max_pages: int = 4,
    ) -> List[CryptoMarket]:
        """Fetch and filter active short-term crypto up/down markets."""
        out: List[CryptoMarket] = []
        seen = set()
        for page in range(max_pages):
            try:
                raws = self._get_markets(limit=500, offset=page * 500)
            except Exception as exc:  # noqa: BLE001
                log.warning("Gamma fetch failed (page %d): %s", page, exc)
                break
            if not raws:
                break
            for raw in raws:
                m = self.parse_market(raw, symbols, durations_minutes, duration_tolerance_seconds)
                if m and m.condition_id not in seen:
                    seen.add(m.condition_id)
                    out.append(m)
            if len(raws) < 500:
                break
        log.info("Discovered %d short-term crypto markets", len(out))
        return out


def _matches_duration(seconds: float, durations_minutes: List[int], tol: int) -> bool:
    for d in durations_minutes:
        if abs(seconds - d * 60) <= tol:
            return True
    return False
