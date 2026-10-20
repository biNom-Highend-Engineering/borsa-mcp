"""Health checks for external services used by Borsa MCP."""

from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx


ResponseValidator = Callable[[httpx.Response], None]


MCP_SMOKE_TOOL = "scan_stocks"
MCP_SMOKE_ARGUMENTS = {"help": True}

# TEFAS delists funds, so probe several large, long-lived funds from different
# founders and only report an error when none of them answer.
TEFAS_CANARY_FUNDS = ("TI2", "AFA", "YAS")


@dataclass(frozen=True)
class Dependency:
    name: str
    url: str
    validator: ResponseValidator | None = None
    method: str = "GET"
    json: dict[str, Any] | None = None
    json_alternatives: tuple[dict[str, Any], ...] = ()
    headers: dict[str, str] | None = None
    required_env: str | None = None
    env_header: str | None = None
    env_json_field: str | None = None
    timeout_seconds: float | None = None
    use_sync_client: bool = False
    # Query parameters computed per probe, for APIs that need a recent window.
    params_factory: Callable[[], dict[str, str]] | None = None
    # Some sites serve their real page with an error status; trust the validator.
    allow_error_status: bool = False
    # A blocking check for upstreams that are not plain HTTP (e.g. websockets).
    # It runs in a worker thread and must raise when the upstream is unusable.
    probe: Callable[[], None] | None = None


class HealthRateLimited(Exception):
    """A provider is reachable but declined a probe because its quota is spent."""


def _validate_yahoo(response: httpx.Response) -> None:
    """Make sure Yahoo returned market data, not an HTML error page."""
    payload = response.json()
    chart = payload.get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise ValueError("Yahoo Finance returned no chart data")


def _validate_json(response: httpx.Response) -> None:
    response.json()


def _validate_xlsx(response: httpx.Response) -> None:
    if not response.content.startswith(b"PK"):
        raise ValueError("Expected an XLSX file")


def _validate_tefas_fund(response: httpx.Response) -> None:
    payload = response.json()
    if payload.get("errorMessage") or not payload.get("resultList"):
        raise ValueError("TEFAS returned no fund data")


def _validate_mynet_stock_list(response: httpx.Response) -> None:
    # Mynet is scraped, so HTTP 200 is not enough. Assert the exact markup
    # MynetProvider._fetch_ticker_urls depends on is still present.
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(response.content, "lxml")
    for link in soup.select("table a.ft-name[href][title]"):
        if "/borsa/hisseler/" in link["href"]:
            return
    raise ValueError("Mynet stock table markup changed")


def _validate_coinbase_products(response: httpx.Response) -> None:
    if not response.json().get("products"):
        raise ValueError("Coinbase returned no products")


def _validate_coinbase_currencies(response: httpx.Response) -> None:
    if not response.json().get("data"):
        raise ValueError("Coinbase returned no currencies")


def _validate_tradingview_earnings(response: httpx.Response) -> None:
    rows = response.json().get("data") or []
    if not rows or len(rows[0].get("d", [])) < 6:
        raise ValueError("TradingView returned no earnings row")


def _validate_btcturk(response: httpx.Response) -> None:
    if response.json().get("success") is not True:
        raise ValueError("BTCTurk API returned an unsuccessful response")


def _validate_bls(response: httpx.Response) -> None:
    payload = response.json()
    if payload.get("status") == "REQUEST_SUCCEEDED":
        return

    message = " ".join(str(item) for item in payload.get("message", []))
    lower_message = message.lower()
    if "daily" in lower_message and any(
        word in lower_message for word in ("limit", "threshold", "requests")
    ):
        raise HealthRateLimited(
            "BLS anonymous daily query quota has been reached"
        )
    else:
        raise ValueError("BLS API returned an unsuccessful response")


def _fred_validator(series_id: str, expected_start_year: int) -> ResponseValidator:
    def validate(response: httpx.Response) -> None:
        # Use the same strict parser as the production macro-data path. HTTP 200
        # is not enough: FRED can return an HTML error page or a truncated CSV.
        from providers.fred_cpi_provider import parse_fred_csv, validate_series

        values = parse_fred_csv(response.text, series_id)
        validate_series(values, expected_start_year=expected_start_year)

    return validate


def _validate_tcmb_inflation(response: httpx.Response) -> None:
    # Exercise the same parser used by get_macro_data(region="tr"). This catches
    # a TCMB page that is reachable but no longer contains usable inflation data.
    from providers.tcmb_provider import TcmbProvider

    provider = TcmbProvider(client=None)
    if not provider._parse_inflation_table(response.text):
        raise ValueError("TCMB returned no inflation rows")


def _validate_canlidoviz(response: httpx.Response) -> None:
    # borsapy reads {"<unix ts>": "open|high|low|close", ...} for FX quotes.
    payload = response.json()
    if not isinstance(payload, dict) or not payload:
        raise ValueError("canlidoviz returned no USD history")
    latest = payload[max(payload, key=int)]
    if len([float(value) for value in latest.split("|")]) < 4:
        raise ValueError("canlidoviz history row is not OHLC")


def _canlidoviz_usd_params() -> dict[str, str]:
    # get_quote reads the latest bar of the last 5 days, so probe a recent window.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    return {
        "period": "DAILY",
        "itemDataId": "1",  # USD in borsapy's CanlidovizProvider.CURRENCY_IDS
        "startDate": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _validate_hedeffiyat(response: httpx.Response) -> None:
    # borsapy finds each stock's page through these <option> links on /senetler.
    if not re.search(r'value="/senet/thyao-[^"]+"', response.text, re.IGNORECASE):
        raise ValueError("hedeffiyat stock list markup changed")


def _validate_bist_index_csv(response: httpx.Response) -> None:
    # borsapy reads these columns for index components and sector peers.
    rows = csv.DictReader(io.StringIO(response.text), delimiter=";")
    missing = {"BILESEN KODU", "ENDEKS KODU"} - set(rows.fieldnames or ())
    if missing:
        raise ValueError(f"Borsa Istanbul CSV lacks columns: {', '.join(sorted(missing))}")
    if not any(row["ENDEKS KODU"] == "XU100" for row in rows):
        raise ValueError("Borsa Istanbul CSV has no XU100 components")


def _probe_tradingview_websocket() -> None:
    # Use borsapy's own websocket client: the same code path that serves BIST
    # quotes, profiles and index values in production.
    from borsapy._providers.tradingview import get_tradingview_provider

    quote = get_tradingview_provider().get_quote("THYAO")
    if quote.get("last") is None:
        raise ValueError("TradingView websocket returned no last price")


TCMB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}


DEPENDENCIES: dict[str, Dependency] = {
    "yahoo_finance": Dependency(
        name="Yahoo Finance",
        url="https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1d",
        validator=_validate_yahoo,
    ),
    "kap": Dependency(
        name="KAP",
        url="https://www.kap.org.tr/tr/api/company/generic/excel/IGS/A",
        validator=_validate_xlsx,
        headers={"Referer": "https://www.kap.org.tr/tr/bist-sirketler"},
    ),
    "tcmb": Dependency(
        name="TCMB Inflation (TUFE)",
        url=(
            "https://www.tcmb.gov.tr/wps/wcm/connect/tr/tcmb+tr/main+menu/"
            "istatistikler/enflasyon+verileri"
        ),
        validator=_validate_tcmb_inflation,
        headers=TCMB_HEADERS,
    ),
    "tcmb_ufe": Dependency(
        name="TCMB Inflation (UFE)",
        url=(
            "https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/"
            "Istatistikler/Enflasyon+Verileri/Uretici+Fiyatlari"
        ),
        validator=_validate_tcmb_inflation,
        headers=TCMB_HEADERS,
    ),
    "tcmb_evds": Dependency(
        name="TCMB EVDS",
        url=(
            "https://evds3.tcmb.gov.tr/igmevdsms-dis/"
            "series=TP.DK.USD.A&startDate=01-07-2026&endDate=02-07-2026&type=json"
        ),
        validator=_validate_json,
        required_env="EVDS_API_KEY",
        env_header="key",
    ),
    "tcmb_calculator": Dependency(
        name="TCMB Inflation Calculator",
        url="https://appg.tcmb.gov.tr/KIMENFH/enflasyon/hesapla",
        validator=_validate_json,
        method="POST",
        json={
            "baslangicYil": "2020",
            "baslangicAy": "1",
            "bitisYil": "2021",
            "bitisAy": "1",
            "malSepeti": "100",
        },
        headers={
            "Origin": "https://herkesicin.tcmb.gov.tr",
            "Referer": "https://herkesicin.tcmb.gov.tr/",
        },
    ),
    "mynet": Dependency(
        name="Mynet Finans",
        url="https://finans.mynet.com/borsa/hisseler/",
        validator=_validate_mynet_stock_list,
    ),
    "is_yatirim": Dependency(
        name="Is Yatirim",
        url="https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/default.aspx",
    ),
    "tefas": Dependency(
        name="TEFAS",
        url="https://www.tefas.gov.tr/api/funds/fonBilgiGetir",
        validator=_validate_tefas_fund,
        method="POST",
        json={"fonKodu": TEFAS_CANARY_FUNDS[0]},
        json_alternatives=tuple(
            {"fonKodu": code} for code in TEFAS_CANARY_FUNDS[1:]
        ),
        headers={"Accept": "application/json"},
    ),
    "takasbank": Dependency(
        name="Takasbank",
        url=(
            "https://www.takasbank.com.tr/plugins/"
            "ExcelExportTefasFundsTradingInvestmentPlatform?language=tr"
        ),
        validator=_validate_xlsx,
    ),
    "doviz": Dependency(
        name="Doviz.com",
        url="https://www.doviz.com/ekonomik-takvim",
    ),
    "btcturk": Dependency(
        name="BTCTurk",
        url="https://api.btcturk.com/api/v2/server/exchangeInfo",
        validator=_validate_btcturk,
    ),
    "btcturk_graph": Dependency(
        name="BTCTurk Graph API",
        url=(
            "https://graph-api.btcturk.com/v1/klines/history"
            "?symbol=BTCTRY&resolution=1440&from=1785456000&to=1786060800"
        ),
        validator=_validate_json,
    ),
    "coinbase": Dependency(
        name="Coinbase Products",
        url="https://api.coinbase.com/api/v3/brokerage/market/products",
        validator=_validate_coinbase_products,
        headers={"Accept": "application/json"},
    ),
    "coinbase_currencies": Dependency(
        name="Coinbase Currencies",
        url="https://api.coinbase.com/v2/currencies",
        validator=_validate_coinbase_currencies,
        headers={"Accept": "application/json"},
    ),
    "world_bank": Dependency(
        name="World Bank",
        url="https://api.worldbank.org/v2/country/TR?format=json",
        validator=_validate_json,
    ),
    "tradingview": Dependency(
        name="TradingView Earnings",
        url="https://scanner.tradingview.com/turkey/scan",
        validator=_validate_tradingview_earnings,
        method="POST",
        json={
            "filter": [
                {"left": "exchange", "operation": "equal", "right": "BIST"}
            ],
            "symbols": {"tickers": ["BIST:THYAO"], "query": {"types": []}},
            "columns": [
                "name",
                "earnings_release_date",
                "earnings_release_next_date",
                "earnings_per_share_basic_ttm",
                "earnings_per_share_diluted_ttm",
                "earnings_per_share_forecast_next_fq",
            ],
            "range": [0, 1],
        },
        headers={"Content-Type": "application/json"},
    ),
    "fred_us": Dependency(
        name="FRED US CPI",
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCNS",
        validator=_fred_validator("CPIAUCNS", 1913),
        headers={"User-Agent": f"python-httpx/{httpx.__version__}"},
        timeout_seconds=15,
        use_sync_client=True,
    ),
    "fred_eu": Dependency(
        name="FRED Euro Area HICP",
        url=(
            "https://fred.stlouisfed.org/graph/"
            "fredgraph.csv?id=CP0000EZCCM086NEST"
        ),
        validator=_fred_validator("CP0000EZCCM086NEST", 1996),
        headers={"User-Agent": f"python-httpx/{httpx.__version__}"},
        timeout_seconds=15,
        use_sync_client=True,
    ),
    "bls": Dependency(
        name="BLS",
        url="https://api.bls.gov/publicAPI/v2/timeseries/data/",
        validator=_validate_bls,
        method="POST",
        json={"seriesid": ["CUUR0000SA0"]},
        required_env="BLS_API_KEY",
        env_json_field="registrationkey",
    ),
    "tradingview_websocket": Dependency(
        name="TradingView Websocket",
        url="wss://data.tradingview.com/socket.io/websocket",
        probe=_probe_tradingview_websocket,
        # get_quote waits up to 10s for the first price packet.
        timeout_seconds=12,
    ),
    "canlidoviz": Dependency(
        name="Canlidoviz",
        url="https://a.canlidoviz.com/items/history",
        validator=_validate_canlidoviz,
        params_factory=_canlidoviz_usd_params,
        headers={
            "Accept": "*/*",
            "Origin": "https://canlidoviz.com",
            "Referer": "https://canlidoviz.com/",
        },
    ),
    "hedeffiyat": Dependency(
        name="Hedeffiyat",
        url="https://www.hedeffiyat.com.tr/senetler",
        validator=_validate_hedeffiyat,
        # The stock list is served with HTTP 404; borsapy reads it regardless.
        allow_error_status=True,
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout_seconds=15,
    ),
    "borsa_istanbul_csv": Dependency(
        name="Borsa Istanbul Index Components",
        url="https://www.borsaistanbul.com/datum/hisse_endeks_ds.csv",
        validator=_validate_bist_index_csv,
    ),
    "eurostat": Dependency(
        name="Eurostat",
        url=(
            "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
            "prc_hicp_midx?format=JSON&geo=EA&coicop=CP00&unit=I15&sinceTimePeriod=2025-01"
        ),
        validator=_validate_json,
    ),
}


_KEY_BY_NAME = {dependency.name: key for key, dependency in DEPENDENCIES.items()}

# Upstreams that tools call but that have no probe yet. Listed per tool branch
# so the health body is honest about what a green status does not cover.
DOVIZ_API = "doviz_api"


@dataclass(frozen=True)
class ToolCheck:
    """What one tool branch needs from upstream to answer.

    ``requires`` holds groups of dependency keys: every group must have at least
    one working member, and the members of a group are fallbacks for each other.
    """

    tool: str
    branch: str
    requires: tuple[tuple[str, ...], ...]
    not_probed: tuple[str, ...] = ()


def _tool(
    tool: str,
    branch: str,
    *requires: str | tuple[str, ...],
    not_probed: tuple[str, ...] = (),
) -> ToolCheck:
    groups = tuple((item,) if isinstance(item, str) else item for item in requires)
    return ToolCheck(tool, branch, groups, not_probed)


TOOL_CHECKS: tuple[ToolCheck, ...] = (
    _tool("search_symbol", "bist", "kap"),
    _tool("search_symbol", "us", "yahoo_finance"),
    _tool("search_symbol", "fund", "takasbank"),
    _tool("search_symbol", "crypto_tr", "btcturk"),
    _tool("search_symbol", "crypto_global", "coinbase", "coinbase_currencies"),
    _tool("get_profile", "bist", "tradingview_websocket"),
    _tool("get_profile", "us", "yahoo_finance"),
    _tool("get_profile", "fund", "tefas"),
    _tool("get_quote", "bist", "tradingview_websocket"),
    _tool("get_quote", "us", "yahoo_finance"),
    # Most currencies and metals come from canlidoviz; the rest are routed to
    # the doviz.com API, which has no probe yet.
    _tool("get_quote", "fx", "canlidoviz", not_probed=(DOVIZ_API,)),
    _tool("get_quote", "crypto_tr", "btcturk"),
    _tool("get_quote", "crypto_global", "coinbase"),
    _tool("get_historical_data", "bist", "tradingview_websocket"),
    _tool("get_historical_data", "us", "yahoo_finance"),
    # Daily bars come from canlidoviz, intraday bars from the websocket.
    _tool(
        "get_historical_data",
        "fx",
        "canlidoviz",
        "tradingview_websocket",
        not_probed=(DOVIZ_API,),
    ),
    _tool("get_historical_data", "crypto_tr", "btcturk_graph"),
    _tool("get_historical_data", "crypto_global", "coinbase"),
    _tool("get_historical_data", "fund", "tefas"),
    _tool("get_technical_analysis", "bist", "tradingview_websocket"),
    _tool("get_technical_analysis", "us", "yahoo_finance"),
    _tool("get_technical_analysis", "crypto_tr", "btcturk_graph"),
    _tool("get_technical_analysis", "crypto_global", "coinbase"),
    _tool("get_analyst_data", "bist", "hedeffiyat"),
    _tool("get_analyst_data", "us", "yahoo_finance"),
    # borsapy is tried first, but the router reads fields it never returns, so
    # the TradingView scanner is what actually answers BIST earnings.
    _tool("get_earnings", "bist", "tradingview"),
    _tool("get_earnings", "us", "yahoo_finance"),
    # borsa_client falls back to Yahoo when Is Yatirim returns no statement.
    _tool("get_financial_statements", "bist", ("is_yatirim", "yahoo_finance")),
    _tool("get_financial_statements", "us", "yahoo_finance"),
    _tool(
        "get_financial_ratios",
        "bist",
        ("is_yatirim", "yahoo_finance"),
        "tradingview_websocket",
    ),
    _tool("get_financial_ratios", "us", "yahoo_finance"),
    _tool("get_corporate_actions", "bist", "is_yatirim"),
    _tool("get_corporate_actions", "us", "yahoo_finance"),
    _tool("get_news", "bist", "mynet"),
    _tool("screen_securities", "bist", "is_yatirim", "borsa_istanbul_csv"),
    _tool("screen_securities", "us", "yahoo_finance"),
    _tool(
        "scan_stocks",
        "bist",
        "tradingview",
        "borsa_istanbul_csv",
        "tradingview_websocket",
    ),
    _tool("compare_assets", "all", "kap", "takasbank", "canlidoviz"),
    _tool("get_sector_comparison", "bist", "is_yatirim", "borsa_istanbul_csv"),
    _tool("get_sector_comparison", "us", "yahoo_finance"),
    _tool("get_crypto_market", "btcturk", "btcturk"),
    _tool("get_crypto_market", "btcturk_ohlc", "btcturk_graph"),
    _tool("get_crypto_market", "coinbase", "coinbase"),
    # The doviz probe checks the HTML calendar, which is the fallback for the API.
    _tool("get_economic_calendar", "all", "doviz", not_probed=(DOVIZ_API,)),
    _tool("get_bond_yields", "tr", "doviz"),
    _tool("get_fund_data", "tefas", "tefas"),
    _tool("screen_funds", "tefas", "tefas"),
    _tool("get_index_data", "bist", "tradingview_websocket", "borsa_istanbul_csv"),
    _tool("get_index_data", "us", "yahoo_finance"),
    _tool("get_macro_data", "tr_cpi", "tcmb"),
    _tool("get_macro_data", "tr_ppi", "tcmb_ufe"),
    _tool("get_macro_data", "tr_calculator", "tcmb_calculator"),
    # FRED is primary; BLS and Eurostat take over when FRED fails. Without
    # BLS_API_KEY the provider falls back to anonymous BLS v1 (25 requests/day),
    # which is too thin to count on, so an unconfigured BLS is no backup here.
    _tool("get_macro_data", "us", ("fred_us", "bls")),
    _tool("get_macro_data", "eu", ("fred_eu", "eurostat")),
    _tool("get_evds_data", "series", "tcmb_evds"),
)

# Worst first: a tool reports the worst status among its branches.
_STATUS_RANK = ("down", "unknown", "not_configured", "degraded", "ok", "not_monitored")


def _group_status(statuses: list[str]) -> str:
    if "ok" in statuses:
        failed = any(status in ("error", "rate_limited") for status in statuses)
        return "degraded" if failed else "ok"
    if "not_checked" in statuses:
        return "unknown"
    if all(status == "not_configured" for status in statuses):
        return "not_configured"
    return "down"


def _worst(statuses: list[str]) -> str:
    return min(statuses, key=_STATUS_RANK.index)


def evaluate_tools(results_by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Roll upstream probe results up to tool branches.

    A branch is ``down`` only when some required source has no working
    alternative; running on a fallback is ``degraded``. Sources that were not
    probed in this run make a branch ``unknown`` rather than ``down``.
    """
    tools: dict[str, Any] = {}
    for check in TOOL_CHECKS:
        sources: dict[str, str] = {}
        group_statuses = []
        for group in check.requires:
            statuses = [
                results_by_key.get(key, {}).get("status", "not_checked")
                for key in group
            ]
            sources.update(zip(group, statuses))
            group_statuses.append(_group_status(statuses))

        branch: dict[str, Any] = {
            "status": _worst(group_statuses) if group_statuses else "not_monitored",
            "sources": sources,
        }
        if check.not_probed:
            branch["not_probed"] = list(check.not_probed)
        tools.setdefault(check.tool, {"branches": {}})["branches"][check.branch] = branch

    for tool in tools.values():
        tool["status"] = _worst(
            [branch["status"] for branch in tool["branches"].values()]
        )
    return tools


def configured_dependencies() -> list[Dependency]:
    """Return configured checks; defaults to every registered external service."""
    configured = os.getenv("HEALTHCHECK_SERVICES") or ",".join(DEPENDENCIES)
    return dependencies_for(configured.split(","))


def dependencies_for(service_keys: list[str]) -> list[Dependency]:
    """Resolve public health-check keys to dependency definitions."""
    aliases = {"fred": "fred_us"}
    keys = [
        aliases.get(key.strip().lower(), key.strip().lower())
        for key in service_keys
        if key.strip()
    ]
    unknown = sorted(set(keys) - DEPENDENCIES.keys())
    if unknown:
        raise ValueError(f"Unknown health-check services: {', '.join(unknown)}")
    if not keys:
        raise ValueError("At least one health-check service is required")
    return [DEPENDENCIES[key] for key in keys]


async def _probe(
    client: httpx.AsyncClient,
    dependency: Dependency,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    if dependency.required_env:
        secret = os.getenv(dependency.required_env, "").strip()
        if not secret:
            return {
                "status": "not_configured",
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "reason": f"{dependency.required_env} is not configured",
            }

    effective_timeout = dependency.timeout_seconds or timeout_seconds
    if dependency.probe:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(dependency.probe), timeout=effective_timeout
            )
        except Exception as exc:
            return {
                "status": "error",
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": _safe_error(exc),
            }
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    headers = {"User-Agent": "BorsaMCP-HealthCheck/1.0"}
    headers.update(dependency.headers or {})
    if dependency.required_env and dependency.env_header:
        headers[dependency.env_header] = secret

    if dependency.json is None:
        payloads: list[dict[str, Any] | None] = [None]
    else:
        payloads = [dependency.json, *dependency.json_alternatives]

    async def attempt(payload: dict[str, Any] | None) -> httpx.Response:
        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": effective_timeout,
        }
        if dependency.params_factory:
            request_kwargs["params"] = dependency.params_factory()
        if payload is not None:
            body = dict(payload)
            if dependency.required_env and dependency.env_json_field:
                body[dependency.env_json_field] = secret
            request_kwargs["json"] = body
        if dependency.use_sync_client:
            # FRED's CDN is reachable from Azure Container Apps with httpx's
            # synchronous transport, while the async transport stalls until its
            # timeout. Keep the ASGI event loop free by running that proven
            # transport in a worker thread.
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _sync_request,
                    dependency,
                    request_kwargs,
                    effective_timeout,
                ),
                timeout=effective_timeout + 1,
            )
        return await asyncio.wait_for(
            client.request(dependency.method, dependency.url, **request_kwargs),
            timeout=effective_timeout,
        )

    last_error: Exception | None = None
    for payload in payloads:
        try:
            response = await attempt(payload)
            if not dependency.allow_error_status:
                response.raise_for_status()
            if dependency.validator:
                dependency.validator(response)
            return {
                "status": "ok",
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "http_status": response.status_code,
            }
        except HealthRateLimited as exc:
            return {
                "status": "rate_limited",
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "reason": str(exc),
            }
        except Exception as exc:
            last_error = exc

    assert last_error is not None
    return {
        "status": "error",
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "error": _safe_error(last_error),
    }


def _sync_request(
    dependency: Dependency,
    request_kwargs: dict[str, Any],
    timeout_seconds: float,
) -> httpx.Response:
    """Perform a blocking probe without blocking the caller's event loop."""
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout_seconds,
        verify=False,
    ) as client:
        return client.request(
            dependency.method,
            dependency.url,
            **request_kwargs,
        )


def _safe_error(exc: Exception) -> str:
    """Return useful diagnostics without including URLs, query strings, or secrets."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return "request timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {str(exc)[:160]}"


async def check_mcp_tools(
    mcp_app: Any,
    *,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    """Exercise MCP initialize, tools/list and tools/call in process.

    Raw provider pings do not prove that FastMCP can publish schemas, dispatch a
    call, or serialize its result. The smoke tool uses its local help mode so the
    platform health check does not consume provider quota every 30 seconds.
    External provider probes remain separate below and identify upstream
    outages alongside this MCP-level check.
    """
    started = time.perf_counter()

    async def run_probe() -> tuple[list[Any], Any]:
        # Import lazily: health.py remains usable by lightweight unit tests and
        # scripts which do not otherwise need to import the MCP client stack.
        from fastmcp import Client

        async with Client(mcp_app) as client:
            tools = await client.list_tools()
            result = await client.call_tool(MCP_SMOKE_TOOL, MCP_SMOKE_ARGUMENTS)
            return tools, result

    try:
        tools, result = await asyncio.wait_for(run_probe(), timeout=timeout_seconds)
        if not tools:
            raise ValueError("MCP server published no tools")
        if result.is_error:
            raise ValueError(f"MCP smoke tool failed: {MCP_SMOKE_TOOL}")

        text_parts = [
            item.text
            for item in result.content
            if getattr(item, "text", None)
        ]
        if not "".join(text_parts).strip():
            raise ValueError(f"MCP smoke tool returned no text: {MCP_SMOKE_TOOL}")

        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "protocol_checks": ["initialize", "tools/list", "tools/call"],
            "tool_count": len(tools),
            "smoke_tool": MCP_SMOKE_TOOL,
        }
    except Exception as exc:
        return {
            "status": "error",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": _safe_error(exc),
        }


async def check_dependencies(
    *,
    dependencies: list[Dependency] | None = None,
    timeout_seconds: float | None = None,
    client: httpx.AsyncClient | None = None,
    mcp_app: Any | None = None,
    tool_aware: bool = False,
) -> tuple[dict[str, Any], int]:
    """Check MCP readiness and dependencies, returning body and HTTP status.

    By default any failed dependency makes the check fail, which suits a probe
    aimed at specific services. With ``tool_aware`` the check fails only when a
    tool branch has lost every source it can use, so an upstream outage that a
    fallback covers is reported as ``degraded`` without failing the check.
    """
    dependencies = dependencies if dependencies is not None else configured_dependencies()
    timeout_seconds = timeout_seconds or float(os.getenv("HEALTHCHECK_TIMEOUT_SECONDS", "5"))

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout_seconds,
            verify=False,
        )

    started = time.perf_counter()
    try:
        tasks = [
            _probe(client, dependency, timeout_seconds) for dependency in dependencies
        ]
        if mcp_app is not None:
            tasks.append(check_mcp_tools(mcp_app, timeout_seconds=timeout_seconds + 5))
        gathered = await asyncio.gather(*tasks)
    finally:
        if owns_client:
            await client.aclose()

    results = gathered[: len(dependencies)]
    mcp_result = gathered[-1] if mcp_app is not None else {
        "status": "not_checked",
        "reason": "No MCP application was supplied",
    }

    services = {
        dependency.name: result
        for dependency, result in zip(dependencies, results, strict=True)
    }
    upstream_failed = any(result["status"] == "error" for result in results)
    app_failed = mcp_result["status"] == "error"
    body: dict[str, Any] = {
        "service": "Borsa MCP Server",
        "version": "1.0.0",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "application": mcp_result,
    }

    if not tool_aware:
        checks_failed = upstream_failed or app_failed
        body["status"] = "degraded" if checks_failed else "ok"
        body["services"] = services
        return body, 503 if checks_failed else 200

    tools = evaluate_tools(
        {_KEY_BY_NAME.get(name, name): result for name, result in services.items()}
    )
    down_tools = [
        f"{tool_name} ({branch_name})"
        for tool_name, tool in tools.items()
        for branch_name, branch in tool["branches"].items()
        if branch["status"] == "down"
    ]
    checks_failed = app_failed or bool(down_tools)
    if checks_failed:
        body["status"] = "unhealthy"
    else:
        body["status"] = "degraded" if upstream_failed else "ok"
    body["down_tools"] = down_tools
    body["tools"] = tools
    body["services"] = services
    return body, 503 if checks_failed else 200
