"""Live price-fetch success and fallback/error behaviour (req 1 & 9):
on API failure the feed must return None and record the error — never a fake
price."""

import pytest

from polybot.marketdata import MarketDataFeed


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FailSession:
    def get(self, *a, **k):
        raise RuntimeError("network down")


class _OkSpotSession:
    def get(self, url, *a, **k):
        return _Resp({"price": "65000.5"})


class _OkKlineSession:
    def get(self, url, params=None, **k):
        # binance kline rows: [openTime, open, high, low, close, volume, closeTime, ...]
        rows = [[i * 60000, "100", "110", "90", "105", "12.5", i * 60000 + 59999]
                for i in range(120)]
        return _Resp(rows)


def test_spot_success_marks_health_ok():
    f = MarketDataFeed(sources=["binance"])
    f._session = _OkSpotSession()
    price = f.get_spot("BTC")
    assert price == 65000.5
    assert f.spot_health.ok is True
    assert f.spot_health.source == "binance"


def test_spot_failure_returns_none_and_records_error():
    f = MarketDataFeed(sources=["binance", "coinbase"])
    f._session = _FailSession()
    price = f.get_spot("BTC")
    assert price is None                      # NEVER a fake fallback
    assert f.spot_health.ok is False
    assert "network down" in f.spot_health.last_error


def test_candles_success_builds_candles():
    f = MarketDataFeed(sources=["binance"])
    f._session = _OkKlineSession()
    candles = f.get_candles("BTC", "5m", limit=120)
    assert candles and len(candles) == 120
    assert candles[0].open == 100 and candles[0].high == 110
    assert candles[0].close == 105 and candles[0].volume == 12.5
    assert f.candle_health.ok is True


def test_candles_failure_returns_none():
    f = MarketDataFeed(sources=["binance"])
    f._session = _FailSession()
    assert f.get_candles("BTC", "5m") is None
    assert f.candle_health.ok is False
    assert f.healthy is False


# --- validated spot: sanity range, fallback, divergence (req 3 & 4) --------
class _MultiSession:
    def __init__(self, binance=None, coinbase=None, fail_binance=False):
        self.binance, self.coinbase, self.fail_binance = binance, coinbase, fail_binance

    def get(self, url, params=None, **k):
        if "binance" in url:
            if self.fail_binance:
                raise RuntimeError("binance down")
            return _Resp({"price": str(self.binance)})
        if "coinbase" in url:
            if self.coinbase is None:
                raise RuntimeError("coinbase down")
            return _Resp({"price": str(self.coinbase)})
        raise RuntimeError("unknown url")


def _feed(**kw):
    f = MarketDataFeed(sources=["binance", "coinbase"])
    f._session = _MultiSession(**kw)
    return f


def test_price_in_range_helper():
    from polybot.marketdata import price_in_range
    assert price_in_range("BTC", 65000) is True
    assert price_in_range("BTC", 5) is False        # absurdly low
    assert price_in_range("BTC", None) is False


def test_validated_spot_prefers_binance():
    f = _feed(binance=65000.0, coinbase=65010.0)
    r = f.get_validated_spot("BTC")
    assert r.price == 65000.0 and r.source == "binance"


def test_validated_spot_falls_back_to_coinbase():
    f = _feed(coinbase=65000.0, fail_binance=True)
    r = f.get_validated_spot("BTC")
    assert r.price == 65000.0 and r.source == "coinbase"   # Binance->Coinbase fallback
    assert "error" in str(r.raw["binance"])


def test_validated_spot_rejects_out_of_range():
    f = _feed(binance=5.0, coinbase=5.0)                    # BTC at $5 is garbage
    r = f.get_validated_spot("BTC")
    assert r.price is None
    assert "out-of-range" in str(r.raw["binance"])


def test_validated_spot_rejects_source_divergence():
    f = _feed(binance=65000.0, coinbase=50000.0)           # ~23% apart
    r = f.get_validated_spot("BTC")
    assert r.price is None                                  # never silently picks one
    assert r.rejected and "diverge" in r.rejected
