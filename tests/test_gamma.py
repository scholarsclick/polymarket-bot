import json
import time

from polybot.gamma import GammaClient


def _client():
    return GammaClient()


def test_classify_reasons():
    c = _client()
    # accepted
    m, why = c.classify_market(_raw(), ["BTC", "ETH"], [5, 15], 90)
    assert m is not None and why == "ok"
    # wrong duration
    _, why = c.classify_market(_raw(endDate="2026-06-17T16:00:00Z"), ["BTC"], [5, 15], 90)
    assert "duration" in why and "60m" in why
    # no symbol
    _, why = c.classify_market(_raw(question="Will the Fed cut?", slug="fed"), ["BTC"], [5, 15], 90)
    assert why == "no BTC/ETH symbol"
    # not binary
    _, why = c.classify_market(_raw(outcomes=json.dumps(["A", "B", "C"])), ["BTC"], [5, 15], 90)
    assert "not binary" in why
    # yes/no accepted
    m, why = c.classify_market(_raw(outcomes=json.dumps(["Yes", "No"])), ["BTC"], [5, 15], 90)
    assert m is not None and why == "ok"


def test_discover_with_report(monkeypatch):
    c = _client()
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300))
    rows = [
        _raw(endDate=soon, startDate=time.strftime("%Y-%m-%dT%H:%M:%SZ",
             time.gmtime(time.time() + 300 - 900))),                       # ok 15m
        _raw(question="Ethereum Up or Down?", slug="eth-up-or-down",
             endDate=soon, startDate=time.strftime("%Y-%m-%dT%H:%M:%SZ",
             time.gmtime(time.time() + 300 - 300)), outcomes=json.dumps(["Up", "Down"])),  # ETH 5m
        _raw(question="Will the Fed cut rates?", slug="fed"),              # filtered: no symbol
    ]
    monkeypatch.setattr(c, "_request", lambda params: (rows, 200, "http://x", None))
    markets, report = c.discover_with_report(["BTC", "ETH"], [5, 15], 120)
    assert report["http_status"] == 200
    assert report["markets_returned"] == 3
    assert report["accepted"] >= 1
    assert "no BTC/ETH symbol" in report["reasons"]
    assert len(report["crypto_titles"]) >= 1     # BTC/ETH ones recorded
    assert report["available_fields"]            # field names captured
    assert report["raw_sample"]                  # sample present


def _raw(**over):
    base = {
        "question": "Bitcoin Up or Down — June 17, 3:15 PM ET?",
        "slug": "bitcoin-up-or-down-june-17-315pm",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["111", "222"]),
        "outcomes": json.dumps(["Up", "Down"]),
        "startDate": "2026-06-17T15:00:00Z",
        "endDate": "2026-06-17T15:15:00Z",
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
    }
    base.update(over)
    return base


def test_parse_basic_btc_15m():
    c = _client()
    m = c.parse_market(_raw(), ["BTC", "ETH"], [5, 15], 90)
    assert m is not None
    assert m.symbol == "BTC"
    assert m.up_token_id == "111"
    assert m.down_token_id == "222"
    assert m.duration_minutes == 15
    assert m.tick_size == 0.01


def test_parse_yes_no_outcomes_maps_yes_to_up():
    c = _client()
    raw = _raw(outcomes=json.dumps(["Yes", "No"]))
    m = c.parse_market(raw, ["BTC"], [15], 90)
    assert m is not None
    assert m.up_token_id == "111"  # "Yes" == up


def test_parse_down_first_outcome_order():
    c = _client()
    raw = _raw(outcomes=json.dumps(["Down", "Up"]), clobTokenIds=json.dumps(["AAA", "BBB"]))
    m = c.parse_market(raw, ["BTC"], [15], 90)
    assert m is not None
    assert m.up_token_id == "BBB"
    assert m.down_token_id == "AAA"


def test_parse_rejects_non_updown():
    c = _client()
    raw = _raw(question="Will the Fed cut rates in July?", slug="fed-july")
    assert c.parse_market(raw, ["BTC"], [15], 90) is None


def test_parse_rejects_wrong_symbol():
    c = _client()
    m = c.parse_market(_raw(), ["ETH"], [15], 90)  # BTC question, only ETH allowed
    assert m is None


def test_parse_rejects_duration_mismatch():
    c = _client()
    # 60-minute window but we only want 5/15m
    raw = _raw(startDate="2026-06-17T15:00:00Z", endDate="2026-06-17T16:00:00Z")
    assert c.parse_market(raw, ["BTC"], [5, 15], 90) is None


def test_parse_5m_window():
    c = _client()
    raw = _raw(startDate="2026-06-17T15:00:00Z", endDate="2026-06-17T15:05:00Z")
    m = c.parse_market(raw, ["BTC"], [5, 15], 90)
    assert m is not None
    assert m.duration_minutes == 5
