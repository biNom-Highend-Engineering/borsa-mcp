"""Health checks for external services used by Borsa MCP."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx


ResponseValidator = Callable[[httpx.Response], None]


@dataclass(frozen=True)
class Dependency:
    name: str
    url: str
    validator: ResponseValidator | None = None
    method: str = "GET"
    json: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    required_env: str | None = None
    env_header: str | None = None
    timeout_seconds: float | None = None
    use_sync_client: bool = False


def _validate_yahoo(response: httpx.Response) -> None:
    """Make sure Yahoo returned market data, not an HTML error page."""
    payload = response.json()
    chart = payload.get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise ValueError("Yahoo Finance returned no chart data")


def _validate_json(response: httpx.Response) -> None:
    response.json()


def _validate_btcturk(response: httpx.Response) -> None:
    if response.json().get("success") is not True:
        raise ValueError("BTCTurk API returned an unsuccessful response")


def _validate_bls(response: httpx.Response) -> None:
    if response.json().get("status") != "REQUEST_SUCCEEDED":
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
        url="https://www.kap.org.tr/tr/",
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
    ),
    "is_yatirim": Dependency(
        name="Is Yatirim",
        url="https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/default.aspx",
    ),
    "tefas": Dependency(
        name="TEFAS",
        url="https://www.tefas.gov.tr/",
    ),
    "takasbank": Dependency(
        name="Takasbank",
        url="https://www.takasbank.com.tr/",
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
        name="Coinbase",
        url="https://api.coinbase.com/v2/time",
        validator=_validate_json,
    ),
    "world_bank": Dependency(
        name="World Bank",
        url="https://api.worldbank.org/v2/country/TR?format=json",
        validator=_validate_json,
    ),
    "tradingview": Dependency(
        name="TradingView Scanner",
        url="https://scanner.tradingview.com/turkey/scan",
        validator=_validate_json,
        method="POST",
        json={
            "symbols": {"tickers": ["BIST:THYAO"], "query": {"types": []}},
            "columns": ["close"],
        },
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
        url="https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0",
        validator=_validate_bls,
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

    try:
        effective_timeout = dependency.timeout_seconds or timeout_seconds
        headers = {"User-Agent": "BorsaMCP-HealthCheck/1.0"}
        headers.update(dependency.headers or {})
        if dependency.required_env:
            if dependency.env_header:
                headers[dependency.env_header] = secret
        request_kwargs: dict[str, Any] = {"headers": headers}
        if dependency.json is not None:
            request_kwargs["json"] = dependency.json
        request_kwargs["timeout"] = effective_timeout
        if dependency.use_sync_client:
            # FRED's CDN is reachable from Azure Container Apps with httpx's
            # synchronous transport, while the async transport stalls until its
            # timeout. Keep the ASGI event loop free by running that proven
            # transport in a worker thread.
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    _sync_request,
                    dependency,
                    request_kwargs,
                    effective_timeout,
                ),
                timeout=effective_timeout + 1,
            )
        else:
            response = await asyncio.wait_for(
                client.request(
                    dependency.method,
                    dependency.url,
                    **request_kwargs,
                ),
                timeout=effective_timeout,
            )
        response.raise_for_status()
        if dependency.validator:
            dependency.validator(response)
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "http_status": response.status_code,
        }
    except Exception as exc:
        return {
            "status": "error",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": _safe_error(exc),
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


async def check_dependencies(
    *,
    dependencies: list[Dependency] | None = None,
    timeout_seconds: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict[str, Any], int]:
    """Check dependencies concurrently and return the response body and HTTP status."""
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
        results = await asyncio.gather(
            *(_probe(client, dependency, timeout_seconds) for dependency in dependencies)
        )
    finally:
        if owns_client:
            await client.aclose()

    services = {
        dependency.name: result
        for dependency, result in zip(dependencies, results, strict=True)
    }
    checks_failed = any(result["status"] == "error" for result in results)
    body = {
        "status": "degraded" if checks_failed else "ok",
        "service": "Borsa MCP Server",
        "version": "1.0.0",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "application": {
            "python_process": "ok",
            "asgi": "ok",
            "http_routing": "ok",
        },
        "services": services,
    }
    return body, 503 if checks_failed else 200
