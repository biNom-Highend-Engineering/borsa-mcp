"""WTI and pump prices must come back from api.doviz.com.

doviz.com stopped writing its API token into the page as a plain hex string; every
page now computes it with an inline obfuscated function. The scraper found nothing,
fell back to hardcoded tokens the API had long since revoked, and every WTI / diesel
/ gasoline / lpg request died with `HTTP 401 {"error":true,"message":""}` — which the
tools then reported as "No current FX quotes for WTI", with the 401 nowhere in sight.
Separately, doviz.com renamed WTI to XTI-USD, so /assets/WTI/* is a 404 even with a
good token.
"""
import re
import time
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from borsapy.exceptions import DataNotAvailableError

from models import DovizcomArsivSonucu, DovizcomGuncelSonucu, DovizcomOHLCVarligi
from providers.borsapy_fx_provider import BorsapyFXProvider
from providers.dovizcom_auth import (
    DovizcomAuthError,
    DovizcomAuthManager,
    decode_obfuscated_token,
    extract_token_from_html,
)
from providers.dovizcom_legacy_provider import DovizcomProvider
from models.unified_base import MarketType
from providers.market_router import MarketRouter


# Captured verbatim from https://www.doviz.com/ on 2026-10-20.
_CAPTURED_SNIPPET = (
    '"Authorization": "Bearer " + (function(){var _0xc59e=["","split","0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/","slice","indexOf","","",".","pow","reduce","reverse","0"];function _0xe54c(d,e,f){var g=_0xc59e[2][_0xc59e[1]](_0xc59e[0]);var h=g[_0xc59e[3]](0,e);var i=g[_0xc59e[3]](0,f);var j=d[_0xc59e[1]](_0xc59e[0])[_0xc59e[10]]()[_0xc59e[9]](function(a,b,c){if(h[_0xc59e[4]](b)!==-1)return a+=h[_0xc59e[4]](b)*(Math[_0xc59e[8]](e,c))},0);var k=_0xc59e[0];while(j>0){k=i[j%f]+k;j=(j-(j%f))/f}return k||_0xc59e[11]}return (function(h,u,n,t,e,r){r="";for(var i=0,len=h.length;i<len;i++){var s="";while(h[i]!==n[e]){s+=h[i];i++}for(var j=0;j<n.length;j++)s=s.replace(new RegExp(n[j],"g"),j);r+=String.fromCharCode(_0xe54c(s,e,10)-t)}return decodeURIComponent(escape(r))})("AAwkCAhkAJhkAwwkAwCkCwCkAwwkAwJkAwwkCwwkCwwkAwCkCwCkAJhkCwAkCwwkAAAkCAhkCwJkCwwkAwCkAAAkCAhkAAwkCwhkCwwkAAhkAAJkCwwkCwwkAJhkAAhkAAwkAJhkAACkAAhkCwCkAAJkCwJkCwwkCwhkAwwkAACkCwJkAAAkAwwkCwJkAACkAACkAAJkAwAkCwAkAAwkAwAkCAhkAwwkAwwkCwJkAwCkCwAkAwJkCwwkAwwkCwCk",73,"hwAJCkpxK",8,5,47)})()'
)
_CAPTURED_TOKEN = "5f903e020bb3e9cb6fdb36f5ab47bb945984e7dba08d60d8871c51f00d3c2b0e"

# A second capture (/emtia/abd-ham-petrol, same day): different alphabet, offset and base.
_CAPTURED_WTI_PAGE_ARGS = (
    "vvutyrutyrvtyrytvyxtyrvtyuvtyurtvyPtvyPtyrPtyrPtyrytvyPtvyytvvutvyvtyuvtyuvtyrvtyrutyuPtvvutyurtyuPtyuPtyrytyuytyrPtyrvtyuPtyuvtvyPtvyPtyuvtyuPtyuvtyrxtyuvtyrxtvvutyrxtvvutvvrtyrvtvyytvyytvvrtvyvtyuytyrutyrxtvyvtyuPtyuutvvutvyvtyuutvyvtvyvtyuPtyrytvvutvyPt",
    "ruyvPxtgc", 25, 6,
)
_CAPTURED_WTI_PAGE_TOKEN = "f021d285cc331cafb88209f599173298cc898484f4fe2aaeb704b96fb6bb91fc"


def _encode(token: str, alphabet: str = "hwAJCkpxK", offset: int = 8, base: int = 5) -> str:
    """Inverse of doviz.com's decoder, to mint pages carrying arbitrary tokens."""
    out = []
    for ch in token:
        n, digits = ord(ch) + offset, ""
        while n:
            digits = alphabet[n % base] + digits
            n //= base
        out.append(digits + alphabet[base])
    return "".join(out)


_CAPTURED_ENCODED = re.search(r'\)\("([^"]+)",73,', _CAPTURED_SNIPPET).group(1)


def _page(token: str) -> str:
    snippet = _CAPTURED_SNIPPET.replace(_CAPTURED_ENCODED, _encode(token))
    return f"<html><script>$.ajaxSetup({{headers: {{{snippet}}}}});</script></html>"


# --- Token extraction ---------------------------------------------------------

def test_the_captured_obfuscated_token_decodes():
    html = f"<script>$.ajaxSetup({{headers: {{{_CAPTURED_SNIPPET}}}}});</script>"
    assert extract_token_from_html(html) == _CAPTURED_TOKEN


def test_decoder_parameters_are_read_from_the_page_not_hardcoded():
    assert decode_obfuscated_token(*_CAPTURED_WTI_PAGE_ARGS) == _CAPTURED_WTI_PAGE_TOKEN


def test_a_literal_token_is_still_found():
    token = "ab" * 32
    assert extract_token_from_html(f'headers: {{"Authorization": "Bearer {token}"}}') == token


def test_a_page_without_a_token_yields_none_not_a_random_hash():
    # The old scraper's last-resort pattern took ANY 64-hex string on the page.
    html = '<script>var sri = "' + "0" * 64 + '";</script>'
    assert extract_token_from_html(html) is None


# --- Auth manager -------------------------------------------------------------

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_auth_manager_scrapes_and_caches_the_token():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(200, text=_page("cafe" * 16))

    async with _client(handler) as client:
        auth = DovizcomAuthManager(client)
        assert await auth.get_valid_token() == "cafe" * 16
        assert await auth.get_valid_token() == "cafe" * 16
    assert hits == ["https://www.doviz.com/"]


async def test_no_token_raises_instead_of_sending_a_dead_fallback():
    async with _client(lambda r: httpx.Response(200, text="<html>redesigned</html>")) as client:
        auth = DovizcomAuthManager(client)
        with pytest.raises(DovizcomAuthError, match="no bearer token found"):
            await auth.get_valid_token()


async def test_a_failed_scrape_is_retried_after_the_cooldown_not_never():
    pages = iter(["<html>blip</html>", _page("beef" * 16)])
    async with _client(lambda r: httpx.Response(200, text=next(pages))) as client:
        auth = DovizcomAuthManager(client)
        with pytest.raises(DovizcomAuthError):
            await auth.get_valid_token()
        # Within the cooldown: fail fast, carrying the original reason.
        with pytest.raises(DovizcomAuthError, match="no bearer token found"):
            await auth.get_valid_token()
        auth._last_failure = time.time() - auth.RETRY_COOLDOWN_SECONDS - 1
        assert await auth.get_valid_token() == "beef" * 16


# --- Legacy provider against a mocked api.doviz.com ---------------------------

_GOOD = "1111" * 16
_STALE = "2222" * 16


def _doviz(api_handler, pages=None):
    """MockTransport for www.doviz.com (token pages) + api.doviz.com."""
    pages = iter(pages or [_page(_GOOD)])
    calls = []

    def handler(request):
        if request.url.host == "www.doviz.com":
            return httpx.Response(200, text=next(pages))
        calls.append(request)
        return api_handler(request)

    return _client(handler), calls


def _authorized(request):
    return (
        request.headers.get("Authorization") == f"Bearer {_GOOD}"
        and request.headers.get("X-Requested-With") == "XMLHttpRequest"
        and request.headers.get("Origin") == "https://www.doviz.com"
    )


_UNAUTHORIZED = httpx.Response(401, json={"error": True, "message": ""})


async def test_wti_is_fetched_as_xti_usd_with_the_scraped_token():
    def api(request):
        if not _authorized(request):
            return _UNAUTHORIZED
        assert request.url.path == "/api/v12/assets/XTI-USD/daily"
        return httpx.Response(200, json={"error": False, "data": {"archive": [
            {"close": 98.94, "update_date": 1792480239},
        ]}})

    client, calls = _doviz(api)
    async with client:
        result = await DovizcomProvider(client).get_asset_current("WTI")
    assert result.error_message is None
    assert result.guncel_deger == 98.94
    assert len(calls) == 1


async def test_a_401_rescrapes_the_token_and_retries():
    def api(request):
        return httpx.Response(200, json={"error": False, "data": {"archive": [
            {"update_date": 1792443600, "open": 0, "highest": 0, "lowest": 0, "close": 96.32},
        ]}}) if _authorized(request) else _UNAUTHORIZED

    client, calls = _doviz(api, pages=[_page(_STALE), _page(_GOOD)])
    async with client:
        result = await DovizcomProvider(client).get_asset_current("diesel")
    assert result.guncel_deger == 96.32
    assert [c.headers["Authorization"] for c in calls] == [f"Bearer {_STALE}", f"Bearer {_GOOD}"]
    assert calls[0].url.path == "/api/v12/assets/diesel/archive"


async def test_a_persistent_401_surfaces_as_the_error_not_as_no_data():
    # _get_asset_from_archive used to swallow this into
    # "No fuel price data available for this asset".
    client, _ = _doviz(lambda r: _UNAUTHORIZED, pages=[_page(_STALE), _page(_STALE)])
    async with client:
        result = await DovizcomProvider(client).get_asset_current("gasoline")
    assert result.guncel_deger is None
    assert "HTTP 401" in result.error_message


async def test_an_unreachable_token_surfaces_through_the_fx_provider():
    client, calls = _doviz(lambda r: pytest.fail("API called without a token"),
                           pages=["<html>no token here</html>"])
    async with client:
        result = await BorsapyFXProvider(client).get_asset_current("WTI")
    assert result.guncel_deger is None
    assert "doviz.com API token" in result.error_message
    assert calls == []


async def test_fuel_archive_parses_through_the_fx_provider():
    def api(request):
        if not _authorized(request):
            return _UNAUTHORIZED
        assert request.url.path == "/api/v12/assets/lpg/archive"
        return httpx.Response(200, json={"error": False, "data": {"archive": [
            {"update_date": 1792011600, "open": 0, "highest": 0, "lowest": 0, "close": 39.45},
            {"update_date": 1792098000, "open": 0, "highest": 0, "lowest": 0, "close": 39.49},
        ]}})

    client, _ = _doviz(api)
    async with client:
        result = await BorsapyFXProvider(client).get_asset_archive("lpg", "2026-10-14", "2026-10-15")
    assert result.error_message is None
    assert [v.kapanis for v in result.ohlc_verileri] == [39.45, 39.49]


# --- The tools ----------------------------------------------------------------

def _router(**methods):
    router = MarketRouter()
    client = MagicMock()
    for name, side in methods.items():
        setattr(client, name, AsyncMock(side_effect=side))
    router._client = client
    return router


async def test_quote_failure_carries_the_upstream_reason():
    async def current(asset):
        return DovizcomGuncelSonucu(
            varlik_adi=asset, guncel_deger=None, son_guncelleme=None,
            error_message='doviz.com API returned HTTP 401 for /assets/XTI-USD/daily: ...',
        )

    router = _router(get_dovizcom_guncel_kur=current)
    with pytest.raises(DataNotAvailableError, match="HTTP 401"):
        await router.get_fx_data(symbols=["WTI"])


@pytest.mark.parametrize("symbol", ["lpg", "WTI", "wti"])
async def test_historical_fuel_and_wti_go_to_the_doviz_archive(symbol):
    seen = {}

    async def archive(asset, start, end):
        seen.update(asset=asset, start=start, end=end)
        return DovizcomArsivSonucu(
            varlik_adi=asset, baslangic_tarihi=date(2026, 10, 10), bitis_tarihi=date(2026, 10, 12),
            ohlc_verileri=[
                DovizcomOHLCVarligi(tarih=date(2026, 10, 10), acilis=0.0, en_yuksek=0.0, en_dusuk=0.0, kapanis=39.45),
                DovizcomOHLCVarligi(tarih=date(2026, 10, 12), acilis=0.0, en_yuksek=40.1, en_dusuk=39.2, kapanis=39.49),
            ],
            veri_sayisi=2,
        )

    router = _router(get_dovizcom_arsiv_veri=archive)
    result = await router.get_historical_data(
        symbol, MarketType.FX, start_date="2026-10-10", end_date="2026-10-12"
    )
    assert seen == {"asset": symbol.upper() if symbol == "wti" else symbol,
                    "start": "2026-10-10", "end": "2026-10-12"}
    assert result["metadata"]["source"] == "doviz.com"
    first, second = result["data"]
    # doviz.com's 0 means "no value", not a price of zero.
    assert (first["open"], first["high"], first["low"], first["close"]) == (None, None, None, 39.45)
    assert (second["high"], second["low"]) == (40.1, 39.2)


async def test_historical_fx_failure_carries_the_upstream_reason():
    async def archive(asset, start, end):
        return DovizcomArsivSonucu(
            varlik_adi=asset, ohlc_verileri=[], veri_sayisi=0,
            error_message="Could not obtain a doviz.com API token: no bearer token found",
        )

    router = _router(get_dovizcom_arsiv_veri=archive)
    with pytest.raises(DataNotAvailableError, match="doviz.com API token"):
        await router.get_historical_data("diesel", MarketType.FX, period="1mo")
