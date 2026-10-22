"""BtcTurk candles fall back to Binance via TradingView when graph-api is empty.

On 2026-10-22 graph-api.btcturk.com answered every window with an empty list,
which left crypto_tr history, technical analysis and OHLC with nothing to show.
"""

import time

import httpx
import pandas as pd
import pytest

from providers import btcturk_provider
from providers.btcturk_provider import (
    FALLBACK_SOURCE,
    FALLBACK_WARNING,
    BtcTurkProvider,
    fetch_fallback_bars,
)

DAY = 86400


def _bars(start: int, count: int, step: int = DAY) -> list[dict]:
    return [
        {"time": start + i * step, "open": 100.0 + i, "high": 101.0 + i,
         "low": 99.0 + i, "close": 100.5 + i, "volume": 2.5}
        for i in range(count)
    ]


def _provider(handler) -> BtcTurkProvider:
    return BtcTurkProvider(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _empty_graph(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/ohlcs":
        return httpx.Response(200, json=[])
    # What graph-api returned on 2026-10-22.
    return httpx.Response(200, json={"s": None, "nb": 1609459140, "t": [], "o": [],
                                     "h": [], "l": [], "c": [], "v": []})


async def test_empty_ohlc_is_served_from_the_fallback(monkeypatch):
    now = int(time.time())
    calls = []

    def fallback(*args):
        calls.append(args)
        return _bars(now - 5 * DAY, 5)

    monkeypatch.setattr(btcturk_provider, "fetch_fallback_bars", fallback)

    result = await _provider(_empty_graph).get_ohlc("BTCTRY", now - 5 * DAY, now)

    assert calls == [("BTCTRY", 1440, now - 5 * DAY, now)]
    assert len(result.ohlc_data) == 5
    assert result.ohlc_data[-1].close == 104.5
    assert result.source == FALLBACK_SOURCE
    assert result.warning == FALLBACK_WARNING
    assert result.error_message is None


async def test_btcturk_candles_are_used_when_present(monkeypatch):
    now = int(time.time())

    def graph(request):
        return httpx.Response(200, json=_bars(now - 2 * DAY, 2))

    def fallback(*args):
        raise AssertionError("fallback must not run when BtcTurk has candles")

    monkeypatch.setattr(btcturk_provider, "fetch_fallback_bars", fallback)

    result = await _provider(graph).get_ohlc("BTCTRY", now - 2 * DAY, now)

    assert len(result.ohlc_data) == 2
    assert result.source == "btcturk"
    assert result.warning is None


async def test_ohlc_reports_both_failures(monkeypatch):
    def fallback(*args):
        raise ValueError("invalid symbol")

    monkeypatch.setattr(btcturk_provider, "fetch_fallback_bars", fallback)

    result = await _provider(_empty_graph).get_ohlc("NOSUCHTRY")

    assert result.ohlc_data == []
    assert "BtcTurk returned no candles" in result.error_message
    assert "invalid symbol" in result.error_message


async def test_graph_api_http_error_also_falls_back(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(
        btcturk_provider, "fetch_fallback_bars", lambda *args: _bars(now - DAY, 1)
    )

    result = await _provider(lambda request: httpx.Response(503)).get_ohlc(
        "BTCTRY", now - DAY, now
    )

    assert len(result.ohlc_data) == 1
    assert result.source == FALLBACK_SOURCE


async def test_empty_klines_are_served_from_the_fallback(monkeypatch):
    now = int(time.time())
    calls = []

    def fallback(*args):
        calls.append(args)
        return _bars(now - 3 * 3600, 3, step=3600)

    monkeypatch.setattr(btcturk_provider, "fetch_fallback_bars", fallback)

    result = await _provider(_empty_graph).get_kline("ETHTRY", "1H", now - 3 * 3600, now)

    assert calls == [("ETHTRY", 60, now - 3 * 3600, now)]
    assert result.status == "ok"
    assert [k.close for k in result.klines] == [100.5, 101.5, 102.5]
    assert result.source == FALLBACK_SOURCE
    assert result.warning == FALLBACK_WARNING


async def test_klines_report_both_failures(monkeypatch):
    def fallback(*args):
        raise ValueError("invalid symbol")

    monkeypatch.setattr(btcturk_provider, "fetch_fallback_bars", fallback)

    result = await _provider(_empty_graph).get_kline("NOSUCHTRY", "1D")

    assert result.klines == []
    assert "invalid symbol" in result.error_message


async def test_technical_analysis_says_where_its_candles_came_from(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(
        btcturk_provider, "fetch_fallback_bars", lambda *args: _bars(now - 180 * DAY, 180)
    )

    result = await _provider(_empty_graph).get_kripto_teknik_analiz("BTCTRY")

    assert result.error_message is None
    assert result.teknik_indiktorler is not None
    assert result.source == FALLBACK_SOURCE
    assert result.warning == FALLBACK_WARNING


class _FakeTradingView:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls = []

    def get_history(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return self.frame


def _frame(timestamps: list[int]) -> pd.DataFrame:
    index = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("Europe/Istanbul")
    return pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 3.0},
        index=index,
    )


def test_fallback_asks_binance_and_trims_to_the_window(monkeypatch):
    now = int(time.time())
    from_ts, to_ts = now - 3 * DAY, now - DAY
    tv = _FakeTradingView(_frame([now - 4 * DAY, from_ts, now - 2 * DAY, to_ts, now]))
    monkeypatch.setattr(btcturk_provider, "get_tradingview_provider", lambda: tv)

    bars = fetch_fallback_bars("BTC_TRY", 1440, from_ts, to_ts)

    symbol, kwargs = tv.calls[0]
    assert symbol == "BTCTRY"
    assert kwargs["exchange"] == "BINANCE"
    assert kwargs["interval"] == "1d"
    assert [bar["time"] for bar in bars] == [from_ts, now - 2 * DAY, to_ts]
    assert bars[0] == {"time": from_ts, "open": 1.0, "high": 2.0, "low": 0.5,
                       "close": 1.5, "volume": 3.0}


@pytest.mark.parametrize(("minutes", "interval"), [(60, "1h"), (240, "4h"), (10080, "1wk")])
def test_fallback_maps_graph_api_resolutions(monkeypatch, minutes, interval):
    now = int(time.time())
    tv = _FakeTradingView(_frame([now - 60]))
    monkeypatch.setattr(btcturk_provider, "get_tradingview_provider", lambda: tv)

    fetch_fallback_bars("ETHTRY", minutes, now - DAY, now)

    assert tv.calls[0][1]["interval"] == interval


def test_intraday_fallback_requests_enough_bars_for_a_24h_market(monkeypatch):
    # borsapy sizes intraday requests for BIST's 510-minute session; without
    # stretching the start, a week of hourly crypto bars came back ~2.5 days short.
    now = int(time.time())
    tv = _FakeTradingView(_frame([now - 60]))
    monkeypatch.setattr(btcturk_provider, "get_tradingview_provider", lambda: tv)

    fetch_fallback_bars("ETHTRY", 60, now - 7 * DAY, now)

    start = tv.calls[0][1]["start"]
    days_back = (pd.Timestamp.now() - pd.Timestamp(start)).days
    assert days_back >= 7 * 1440 / 510


def test_fallback_raises_when_the_window_is_empty(monkeypatch):
    now = int(time.time())
    tv = _FakeTradingView(_frame([now - 30 * DAY]))
    monkeypatch.setattr(btcturk_provider, "get_tradingview_provider", lambda: tv)

    with pytest.raises(ValueError, match="no candles"):
        fetch_fallback_bars("BTCTRY", 1440, now - DAY, now)
