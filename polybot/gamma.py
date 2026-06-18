"""Discover short-term crypto up/down markets via Polymarket's Gamma API.

`discover_with_report` returns the eligible markets *and* a detailed diagnostic
report (counts, per-market filter reasons, sample raw response, etc.) so the
dashboard can show exactly why markets are or aren't being picked up.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional, Tuple

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
    "BNB": ["bnb"],
}

_UP_OUTCOMES = {"up", "yes", "higher", "above"}
_DOWN_OUTCOMES = {"down", "no", "lower", "below"}

# Title hints for these markets (used as a soft signal, not a hard requirement).
_UPDOWN_RE = re.compile(r"up or down|higher or lower|\bup\b.*\bdown\b|up/down", re.IGNORECASE)

# The real candle window lives in the slug, e.g. "btc-updown-5m-1781765400"
# -> duration 5 minutes, start epoch 1781765400. (endDate/startDate in the API
# payload are resolution time and series-creation time, NOT the candle window.)
_SLUG_WINDOW_RE = re.compile(r"(?:updown|up-or-down)-(\d+)\s*m-(\d+)", re.IGNORECASE)
# Fallback: a time range in the title, e.g. "2:50AM-2:55AM ET" -> 5 minutes.
_TITLE_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*([AP]M)?\s*[-–]\s*(\d{1,2}):(\d{2})\s*([AP]M)", re.IGNORECASE)


def _title_duration_minutes(question: str) -> Optional[int]:
    m = _TITLE_RANGE_RE.search(question or "")
    if not m:
        return None
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    ap1 = (ap1 or ap2 or "").upper()
    ap2 = ap2.upper()

    def to_min(h, mm, ap):
        h = int(h) % 12
        if ap == "PM":
            h += 12
        return h * 60 + int(mm)

    t1, t2 = to_min(h1, m1, ap1), to_min(h2, m2, ap2)
    diff = (t2 - t1) % (24 * 60)
    return diff or None


def _parse_window(slug: str, question: str, raw: dict
                  ) -> Tuple[Optional[float], Optional[float], Optional[int], str]:
    """Return (start_time, end_time, duration_min, how). Prefers the slug, then
    a title time range, then the (often misleading) startDate/endDate pair."""
    end_time = _parse_iso(_first(raw, "endDate", "endDateIso"))

    m = _SLUG_WINDOW_RE.search(slug or "")
    if m:
        dur = int(m.group(1))
        start = float(m.group(2))
        end = end_time if end_time else start + dur * 60
        return start, end, dur, "slug"

    dur = _title_duration_minutes(question)
    if dur is not None and end_time is not None:
        return end_time - dur * 60, end_time, dur, "title"

    start = _parse_iso(_first(raw, "startDate", "startDateIso", "gameStartTime"))
    if start is not None and end_time is not None:
        return start, end_time, round((end_time - start) / 60), "dates"

    return None, end_time, None, "none"



def _parse_iso(ts) -> Optional[float]:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        # epoch seconds or ms
        return ts / 1000.0 if ts > 1e12 else float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _detect_symbol(text: str, allowed: List[str]) -> Optional[str]:
    low = text.lower()
    for sym in allowed:
        for kw in _SYMBOL_KEYWORDS.get(sym.upper(), [sym.lower()]):
            if re.search(rf"\b{re.escape(kw)}\b", low):
                return sym.upper()
    return None


def _as_list(raw) -> List:
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


def _first(raw: dict, *keys):
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return None


def _matches_duration(seconds: float, durations_minutes: List[int], tol: int) -> bool:
    return any(abs(seconds - d * 60) <= tol for d in durations_minutes)


class GammaClient:
    def __init__(self, host: str = "https://gamma-api.polymarket.com", timeout: float = 8.0):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polybot/0.3"})

    # ----- raw fetch -------------------------------------------------------
    def _request(self, params: dict) -> Tuple[list, Optional[int], str, Optional[str]]:
        url = f"{self.host}/markets"
        try:
            r = self._session.get(url, params=params, timeout=self.timeout)
            status = r.status_code
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            return items, status, str(r.url), None
        except Exception as exc:  # noqa: BLE001
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
            return [], status, url, str(exc)

    # ----- classification --------------------------------------------------
    def classify_market(self, raw: dict, symbols: List[str], durations_minutes: List[int],
                        duration_tolerance_seconds: int) -> Tuple[Optional[CryptoMarket], str]:
        """Return (market, reason). `market` is None when filtered; `reason`
        explains why (or 'ok')."""
        question = str(_first(raw, "question", "title") or "").strip()
        slug = str(raw.get("slug", "") or "")
        if not question and not slug:
            return None, "no title/slug"

        symbol = _detect_symbol(f"{question} {slug}", symbols)
        if not symbol:
            return None, "no BTC/ETH symbol"

        token_ids = [str(t) for t in _as_list(raw.get("clobTokenIds"))]
        outcomes = [str(o) for o in _as_list(raw.get("outcomes"))]
        if len(token_ids) != 2 or len(outcomes) != 2:
            return None, f"not binary (outcomes={outcomes or 'n/a'})"

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
            return None, f"outcomes not up/down|yes/no ({outcomes})"
        down_idx = 1 - up_idx

        # The candle window comes from the slug (or title), NOT startDate/endDate
        # — the API's startDate is the series creation time, not the candle open.
        start_time, end_time, duration_min, how = _parse_window(slug, question, raw)
        if end_time is None:
            return None, "no end date"
        if duration_min is None or start_time is None:
            return None, "cannot determine candle window"
        if not _matches_duration(duration_min * 60, durations_minutes, duration_tolerance_seconds):
            return None, f"duration {duration_min}m not in {durations_minutes} (via {how})"

        try:
            tick = float(_first(raw, "orderPriceMinTickSize", "minTickSize") or 0.01)
        except (TypeError, ValueError):
            tick = 0.01
        try:
            min_size = float(_first(raw, "orderMinSize", "minimumOrderSize") or 5.0)
        except (TypeError, ValueError):
            min_size = 5.0

        market = CryptoMarket(
            condition_id=str(_first(raw, "conditionId", "condition_id") or slug),
            question=question or slug, slug=slug, symbol=symbol,
            up_token_id=token_ids[up_idx], down_token_id=token_ids[down_idx],
            start_time=start_time, end_time=end_time, tick_size=tick,
            min_size=min_size, neg_risk=bool(raw.get("negRisk", False)),
        )
        return market, "ok"

    # back-compat shim used by older callers/tests
    def parse_market(self, raw, symbols, durations_minutes, duration_tolerance_seconds):
        market, _ = self.classify_market(raw, symbols, durations_minutes, duration_tolerance_seconds)
        return market

    # ----- discovery with diagnostics -------------------------------------
    def discover_with_report(self, symbols: List[str], durations_minutes: List[int],
                             duration_tolerance_seconds: int, max_pages: int = 4
                             ) -> Tuple[List[CryptoMarket], dict]:
        now = time.time()
        iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Try strategies in order; use the first that returns rows. Ordering by
        # endDate ascending WITH end_date_min surfaces the soonest *upcoming*
        # markets (the 5m/15m ones) instead of old unresolved markets.
        strategies = [
            ("upcoming (end_date_min)", {
                "active": "true", "closed": "false", "archived": "false",
                "limit": 500, "end_date_min": iso_now,
                "order": "endDate", "ascending": "true"}),
            ("active soonest", {
                "active": "true", "closed": "false",
                "limit": 500, "order": "endDate", "ascending": "true"}),
            ("broad open", {"closed": "false", "limit": 1000}),
        ]

        attempts = []
        raws: list = []
        used = None
        chosen_url = None
        chosen_status = None
        for name, params in strategies:
            items, status, url, err = self._request(params)
            attempts.append({"strategy": name, "status": status,
                             "returned": len(items), "error": err})
            if items and not raws:
                raws, used, chosen_url, chosen_status = items, name, url, status
            # keep going only to record attempts if nothing found yet
            if raws and items:
                break

        markets: List[CryptoMarket] = []
        reasons: Counter = Counter()
        crypto_titles: list = []
        seen = set()
        for raw in raws:
            market, reason = self.classify_market(raw, symbols, durations_minutes,
                                                  duration_tolerance_seconds)
            reasons[reason] += 1
            # record every BTC/ETH-looking market regardless of acceptance
            sym = _detect_symbol(f"{raw.get('question','')} {raw.get('slug','')}", symbols)
            if sym:
                end_t = _parse_iso(_first(raw, "endDate", "endDateIso"))
                crypto_titles.append({
                    "title": str(_first(raw, "question", "title") or raw.get("slug", ""))[:70],
                    "symbol": sym,
                    "outcomes": _as_list(raw.get("outcomes")),
                    "expiry": end_t,
                    "reason": reason,
                })
            if market and market.condition_id not in seen and market.end_time > now:
                seen.add(market.condition_id)
                markets.append(market)

        report = {
            "ts": now,
            "endpoint": f"{self.host}/markets",
            "attempts": attempts,
            "strategy_used": used,
            "http_status": chosen_status,
            "params_used": chosen_url,
            "markets_returned": len(raws),
            "accepted": len(markets),
            "filtered_out": len(raws) - len(markets),
            "reasons": dict(reasons),
            "first_titles": [str(_first(r, "question", "title") or r.get("slug", ""))[:80]
                             for r in raws[:20]],
            "crypto_titles": crypto_titles[:60],
            "available_fields": sorted(raws[0].keys()) if raws else [],
            "raw_sample": _trim_sample(raws[0]) if raws else {},
            "accepted_markets": [{
                "title": m.question[:70], "symbol": m.symbol,
                "duration_min": m.duration_minutes, "expiry": m.end_time,
                "seconds_left": m.seconds_to_resolution(now),
            } for m in markets],
        }
        log.info("Discovery: %d returned, %d accepted, %d crypto-related (strategy=%s)",
                 len(raws), len(markets), len(crypto_titles), used)
        return markets, report

    def discover(self, symbols, durations_minutes, duration_tolerance_seconds, max_pages: int = 4):
        markets, _ = self.discover_with_report(symbols, durations_minutes,
                                               duration_tolerance_seconds, max_pages)
        return markets


def _trim_sample(raw: dict) -> dict:
    """A compact sample of a raw market for field-name verification."""
    keep = ["question", "title", "slug", "conditionId", "clobTokenIds", "outcomes",
            "outcomePrices", "startDate", "endDate", "endDateIso", "active", "closed",
            "liquidity", "volume", "orderPriceMinTickSize", "orderMinSize", "negRisk"]
    return {k: raw.get(k) for k in keep if k in raw}
