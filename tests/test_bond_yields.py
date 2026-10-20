"""get_bond_yields must parse doviz.com's current markup, and fail loudly when it can't.

borsapy 0.11.0 looks for <table id="commodities"> on doviz.com/tahvil; the page
now serves <table id="bonds"> (with US/UK bonds mixed in), so every maturity
raised and the tool returned an empty "success" with no yields.
"""
import httpx
import pytest
from borsapy.cache import Cache
from borsapy.exceptions import DataNotAvailableError
from fastmcp import Client
from fastmcp.exceptions import ToolError

import providers.borsapy_bond_provider as bond_module
from providers.borsapy_bond_provider import BorsapyBondProvider, DovizcomBondsPageProvider
from unified_mcp_server import app


def _row(name, slug, flag, last, change, direction="up"):
    return f"""
    <tr data-name="{name}">
        <td>
          <a href="https://www.doviz.com/tahvil/{slug}">
            <img src="https://cdn.doviz.com/images/country_flags/{flag}.png" alt="{name}" class="currency-icon">
            <div class="currency-details"><div>{name}</div></div>
          </a>
        </td>
        <td class="text-bold">{last}</td>
        <td class="text-bold color-{direction}">
            %{change}
        </td>
    </tr>"""


def _page(rows, table_id="bonds"):
    return f"""<html><body>
    <table id="{table_id}">
      <thead><tr>
        <th class="no-sort"><input id="filter" placeholder="Tahvil Ara"></th>
        <th data-sorter>Son</th><th data-sorter-disabled>Değişim</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <table><tr><td>unrelated</td></tr></table>
    </body></html>"""


# Trimmed from the live page on 2026-10-20.
BONDS_PAGE = _page([
    _row("TR 2 Yıllık Tahvil Faizi", "tr-2-yillik-tahvil", "tr", "39,58", "0,15"),
    _row("TR 5 Yıllık Tahvil Faizi", "tr-5-yillik-tahvil", "tr", "38,89", "0,39"),
    _row("TR 10 Yıllık Tahvil Faizi", "tr-10-yillik-tahvil", "tr", "35,64", "-0,06", "down"),
    _row("ABD 2 Yıllık Tahvil Faizi", "abd-2-yillik-tahvil", "us", "4,76", "0,04"),
    _row("ABD 10 Yıllık Tahvil Faizi", "abd-10-yillik-tahvil", "us", "5,12", "-0,39", "down"),
    _row("İngiltere 2 Yıllık Tahvil Faizi", "ingiltere-2-yillik-tahvil", "gb", "4,91", "-0,38", "down"),
])


def _provider(html: str, status: int = 200) -> DovizcomBondsPageProvider:
    """A page provider whose HTTP client serves `html` and whose cache is empty."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=html.encode("utf-8"))

    provider = DovizcomBondsPageProvider(cache=Cache())
    provider._client.close()
    provider._client = httpx.Client(transport=httpx.MockTransport(handler))
    provider.calls = calls
    return provider


@pytest.fixture
def serve_page(monkeypatch):
    """Point the shared page provider (what the tool uses) at canned HTML."""
    def _serve(html: str, status: int = 200) -> DovizcomBondsPageProvider:
        provider = _provider(html, status)
        monkeypatch.setattr(bond_module, "_bonds_page_provider", provider)
        return provider
    return _serve


# --- parser -------------------------------------------------------------------

def test_parses_only_turkish_bonds_from_the_bonds_table():
    bonds = DovizcomBondsPageProvider(cache=Cache()).parse_bonds_html(BONDS_PAGE)

    assert [b["maturity"] for b in bonds] == ["2Y", "5Y", "10Y"]
    assert all(b["name"].startswith("TR ") for b in bonds)
    ten = bonds[2]
    assert ten["yield"] == 35.64
    assert ten["yield_decimal"] == pytest.approx(0.3564)
    assert ten["change_pct"] == -0.06
    assert ten["url"] == "https://www.doviz.com/tahvil/tr-10-yillik-tahvil"


def test_legacy_commodities_table_is_still_accepted():
    page = _page([_row("TR 10 Yıllık Tahvil Faizi", "tr-10-yillik-tahvil", "tr", "35,64", "0,06")],
                 table_id="commodities")
    bonds = DovizcomBondsPageProvider(cache=Cache()).parse_bonds_html(page)
    assert bonds[0]["maturity"] == "10Y"


def test_missing_table_raises():
    with pytest.raises(DataNotAvailableError, match="markup changed"):
        DovizcomBondsPageProvider(cache=Cache()).parse_bonds_html(
            "<html><table id='something-else'></table></html>"
        )


def test_table_without_turkish_rows_raises():
    page = _page([_row("ABD 10 Yıllık Tahvil Faizi", "abd-10-yillik-tahvil", "us", "5,12", "0,1")])
    with pytest.raises(DataNotAvailableError):
        DovizcomBondsPageProvider(cache=Cache()).parse_bonds_html(page)


def test_get_bond_yields_fetches_once_then_serves_from_cache():
    provider = _provider(BONDS_PAGE)

    assert len(provider.get_bond_yields()) == 3
    assert len(provider.get_bond_yields()) == 3
    assert len(provider.calls) == 1
    assert str(provider.calls[0].url) == "https://www.doviz.com/tahvil"


# --- BorsapyBondProvider / risk-free rate users -------------------------------

async def test_tahvil_faizleri_returns_all_maturities():
    result = await BorsapyBondProvider(_provider(BONDS_PAGE)).get_tahvil_faizleri()

    assert result["toplam_tahvil"] == 3
    assert result["tahvil_lookup"] == pytest.approx({"2Y": 0.3958, "5Y": 0.3889, "10Y": 0.3564})
    assert "uyarilar" not in result
    assert "error" not in result


async def test_missing_maturity_is_reported_as_a_warning():
    page = _page([_row("TR 10 Yıllık Tahvil Faizi", "tr-10-yillik-tahvil", "tr", "35,64", "0,06")])
    result = await BorsapyBondProvider(_provider(page)).get_tahvil_faizleri()

    assert result["tahvil_lookup"] == pytest.approx({"10Y": 0.3564})
    assert result["uyarilar"] == ["2Y: not listed on doviz.com/tahvil",
                                  "5Y: not listed on doviz.com/tahvil"]


async def test_10y_yield_feeds_the_dcf_risk_free_rate():
    provider = BorsapyBondProvider(_provider(BONDS_PAGE))

    assert await provider.get_10y_tahvil_faizi() == pytest.approx(0.3564)
    assert provider.get_risk_free_rate() == pytest.approx(0.3564)


async def test_risk_free_rate_is_none_when_the_page_breaks():
    provider = BorsapyBondProvider(_provider("<html></html>"))

    assert await provider.get_10y_tahvil_faizi() is None
    assert provider.get_risk_free_rate() is None


async def test_buffett_dcf_uses_the_live_10y_yield(serve_page):
    from providers.buffett_analyzer_provider import BuffettAnalyzerProvider

    serve_page(BONDS_PAGE)
    analyzer = BuffettAnalyzerProvider(tahvil_provider=BorsapyBondProvider())

    assert await analyzer.tahvil_provider.get_10y_tahvil_faizi() == pytest.approx(0.3564)


# --- tool boundary ------------------------------------------------------------

async def test_tool_returns_yields_and_risk_free_rate(serve_page):
    serve_page(BONDS_PAGE)

    async with Client(app) as client:
        result = await client.call_tool("get_bond_yields", {})

    text = result.content[0].text
    assert "35.64" in text and "39.58" in text and "38.89" in text
    assert "0.3564" in text  # risk_free_rate
    assert "4.76" not in text  # US 2Y must not leak in


@pytest.mark.parametrize("html,status", [
    ("<html><table id='commodities-v2'></table></html>", 200),  # markup changed again
    (BONDS_PAGE, 503),                                           # upstream down
])
async def test_tool_raises_instead_of_returning_an_empty_success(serve_page, html, status):
    serve_page(html, status)

    async with Client(app) as client:
        with pytest.raises(ToolError, match="no Turkish bond yields"):
            await client.call_tool("get_bond_yields", {})
