import json

import httpx
import pytest

from health import (
    DEPENDENCIES,
    TOOL_CHECKS,
    Dependency,
    TEFAS_CANARY_FUNDS,
    check_mcp_tools,
    check_dependencies,
    configured_dependencies,
    dependencies_for,
    evaluate_tools,
)


def test_all_external_services_are_checked_by_default(monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_SERVICES", raising=False)

    configured = configured_dependencies()

    assert len(configured) == len(DEPENDENCIES)
    assert {item.name for item in configured} == {
        "Yahoo Finance",
        "KAP",
        "TCMB Inflation (TUFE)",
        "TCMB Inflation (UFE)",
        "TCMB EVDS",
        "TCMB Inflation Calculator",
        "Mynet Finans",
        "Is Yatirim Financial Statements",
        "Is Yatirim Screener",
        "Is Yatirim Corporate Actions",
        "TEFAS",
        "Takasbank",
        "Doviz.com Calendar API",
        "Doviz.com Calendar Page",
        "Doviz.com Bond Yields",
        "Doviz.com API",
        "BTCTurk",
        "BTCTurk Graph API",
        "BTCTurk OHLC Fallback (Binance via TradingView)",
        "Coinbase Products",
        "Coinbase Currencies",
        "World Bank",
        "TradingView Earnings",
        "FRED US CPI",
        "FRED Euro Area HICP",
        "BLS",
        "TradingView Websocket",
        "Canlidoviz",
        "Hedeffiyat",
        "Borsa Istanbul Index Components",
        "Eurostat",
    }


def test_unknown_configured_service_is_rejected(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_SERVICES", "kap,not-a-provider")

    with pytest.raises(ValueError, match="not-a-provider"):
        configured_dependencies()


def test_dependencies_can_be_selected_for_per_service_azure_alerts():
    selected = dependencies_for(["fred_us", "fred_eu", "tcmb_ufe"])

    assert [item.name for item in selected] == [
        "FRED US CPI",
        "FRED Euro Area HICP",
        "TCMB Inflation (UFE)",
    ]


def test_legacy_fred_key_selects_us_region():
    assert dependencies_for(["fred"])[0].name == "FRED US CPI"


async def test_production_aligned_probes_send_expected_requests():
    seen = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.add(request.url.path)

        if request.url.path == "/tr/api/company/generic/excel/IGS/A":
            assert request.method == "GET"
            assert request.headers["referer"].endswith("/tr/bist-sirketler")
            return httpx.Response(200, content=b"PK\x03\x04kap-xlsx")

        if request.url.path == "/api/funds/fonBilgiGetir":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "fonKodu": TEFAS_CANARY_FUNDS[0]
            }
            return httpx.Response(
                200, json={"resultList": [{"fonKodu": TEFAS_CANARY_FUNDS[0]}]}
            )

        if request.url.path.startswith(
            "/plugins/ExcelExportTefasFundsTradingInvestmentPlatform"
        ):
            assert request.method == "GET"
            assert request.url.params["language"] == "tr"
            return httpx.Response(200, content=b"PK\x03\x04takasbank-xlsx")

        if request.url.path == "/api/v3/brokerage/market/products":
            assert request.method == "GET"
            return httpx.Response(200, json={"products": [{"product_id": "BTC-USD"}]})

        if request.url.path == "/v2/currencies":
            assert request.method == "GET"
            return httpx.Response(200, json={"data": [{"id": "USD"}]})

        if request.url.path == "/turkey/scan":
            assert request.method == "POST"
            payload = json.loads(request.content)
            assert payload["symbols"]["tickers"] == ["BIST:THYAO"]
            assert payload["columns"] == [
                "name",
                "earnings_release_date",
                "earnings_release_next_date",
                "earnings_per_share_basic_ttm",
                "earnings_per_share_diluted_ttm",
                "earnings_per_share_forecast_next_fq",
            ]
            return httpx.Response(200, json={"data": [{"d": ["THYAO", 1, 2, 3, 4, 5]}]})

        raise AssertionError(f"Unexpected health request: {request.method} {request.url}")

    dependencies = dependencies_for(
        ["kap", "tefas", "takasbank", "coinbase", "coinbase_currencies", "tradingview"]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies, client=client, timeout_seconds=1
        )

    assert status_code == 200
    assert body["status"] == "ok"
    assert seen == {
        "/tr/api/company/generic/excel/IGS/A",
        "/api/funds/fonBilgiGetir",
        "/plugins/ExcelExportTefasFundsTradingInvestmentPlatform",
        "/api/v3/brokerage/market/products",
        "/v2/currencies",
        "/turkey/scan",
    }


async def test_unconfigured_optional_api_is_reported_without_failing_health(monkeypatch):
    monkeypatch.delenv("EVDS_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent without an API key")

    dependency = Dependency(
        "TCMB EVDS",
        "https://example.test/evds",
        required_env="EVDS_API_KEY",
        env_header="key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=[dependency], client=client, timeout_seconds=1
        )

    assert status_code == 200
    assert body["status"] == "ok"
    assert body["services"]["TCMB EVDS"] == {
        "status": "not_configured",
        "latency_ms": body["services"]["TCMB EVDS"]["latency_ms"],
        "reason": "EVDS_API_KEY is not configured",
    }


async def test_api_key_is_sent_in_evds_header(monkeypatch):
    monkeypatch.setenv("EVDS_API_KEY", "test-secret-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["key"] == "test-secret-key"
        return httpx.Response(200, json={"items": [{"value": "1"}]})

    dependency = Dependency(
        "TCMB EVDS",
        "https://example.test/evds",
        validator=lambda response: response.json(),
        required_env="EVDS_API_KEY",
        env_header="key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=[dependency], client=client, timeout_seconds=1
        )

    assert status_code == 200
    assert body["services"]["TCMB EVDS"]["status"] == "ok"


async def test_health_is_healthy_when_all_dependencies_respond():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    dependencies = [
        Dependency("Yahoo Finance", "https://example.test/yahoo"),
        Dependency("KAP", "https://example.test/kap"),
        Dependency("TCMB", "https://example.test/tcmb"),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies, client=client, timeout_seconds=1
        )

    assert status_code == 200
    assert body["status"] == "ok"
    assert body["application"] == {
        "status": "not_checked",
        "reason": "No MCP application was supplied",
    }
    assert set(body["services"]) == {"Yahoo Finance", "KAP", "TCMB"}
    assert all(item["status"] == "ok" for item in body["services"].values())


async def test_health_returns_503_and_identifies_failed_dependency():
    def handler(request: httpx.Request) -> httpx.Response:
        status = 503 if request.url.path == "/tcmb" else 200
        return httpx.Response(status, text="response")

    dependencies = [
        Dependency("Yahoo Finance", "https://example.test/yahoo"),
        Dependency("TCMB", "https://example.test/tcmb"),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies, client=client, timeout_seconds=1
        )

    assert status_code == 503
    assert body["status"] == "degraded"
    assert body["services"]["Yahoo Finance"]["status"] == "ok"
    assert body["services"]["TCMB"] == {
        "status": "error",
        "latency_ms": body["services"]["TCMB"]["latency_ms"],
        "error": "HTTP 503",
    }


async def test_delisted_tefas_canary_fund_falls_back_to_the_next_one():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fund_code = json.loads(request.content)["fonKodu"]
        seen.append(fund_code)
        if fund_code == TEFAS_CANARY_FUNDS[0]:
            return httpx.Response(200, json={"resultList": []})
        return httpx.Response(200, json={"resultList": [{"fonKodu": fund_code}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies_for(["tefas"]), client=client, timeout_seconds=1
        )

    assert status_code == 200
    assert body["services"]["TEFAS"]["status"] == "ok"
    assert seen == list(TEFAS_CANARY_FUNDS[:2])


async def test_tefas_fails_only_when_every_canary_fund_is_empty():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["fonKodu"])
        return httpx.Response(200, json={"resultList": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies_for(["tefas"]), client=client, timeout_seconds=1
        )

    assert status_code == 503
    assert body["services"]["TEFAS"]["status"] == "error"
    assert seen == list(TEFAS_CANARY_FUNDS)


async def test_mcp_health_lists_contract_and_calls_a_tool():
    from unified_mcp_server import app

    result = await check_mcp_tools(app)

    assert result["status"] == "ok"
    assert result["tool_count"] > 0
    assert result["protocol_checks"] == ["initialize", "tools/list", "tools/call"]
    assert result["smoke_tool"] == "scan_stocks"


async def test_http_health_defaults_to_mcp_readiness_and_all_upstreams(monkeypatch):
    import app as app_module

    captured = {}

    async def fake_check_dependencies(*, dependencies, mcp_app, tool_aware):
        captured["dependencies"] = dependencies
        captured["mcp_app"] = mcp_app
        captured["tool_aware"] = tool_aware
        return {
            "status": "ok",
            "application": {"status": "ok", "tool_count": 23},
            "services": {"all": {"status": "ok"}},
        }, 200

    monkeypatch.setattr(app_module, "check_dependencies", fake_check_dependencies)

    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://health.test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["services"] == {"all": {"status": "ok"}}
    assert body["application"]["status"] == "ok"
    assert body["application"]["tool_count"] == 23
    assert captured["dependencies"] is None
    assert captured["mcp_app"] is app_module.mcp
    assert captured["tool_aware"] is True


async def test_http_liveness_answers_200_without_checking_any_upstream(monkeypatch):
    import app as app_module

    async def fail_if_called(**_kwargs):
        raise AssertionError("/health/live must not check upstreams")

    monkeypatch.setattr(app_module, "check_dependencies", fail_if_called)

    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://health.test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "Borsa MCP Server"}


def _all_ok(**overrides):
    results = {key: {"status": "ok"} for key in DEPENDENCIES}
    results.update({key: {"status": status} for key, status in overrides.items()})
    return results


async def test_every_registered_tool_has_a_health_mapping():
    from tests.conftest import tools_by_name
    from unified_mcp_server import app

    assert {check.tool for check in TOOL_CHECKS} == set(await tools_by_name(app))


def test_tool_checks_only_reference_registered_dependencies():
    referenced = {key for check in TOOL_CHECKS for group in check.requires for key in group}

    assert referenced <= DEPENDENCIES.keys()


def test_tool_running_on_its_fallback_is_degraded_not_down():
    tools = evaluate_tools(_all_ok(fred_us="error"))

    us = tools["get_macro_data"]["branches"]["us"]
    assert us["status"] == "degraded"
    assert us["sources"] == {"fred_us": "error", "bls": "ok"}


def test_tool_is_down_when_primary_and_fallback_both_fail():
    tools = evaluate_tools(_all_ok(fred_eu="error", eurostat="error"))

    assert tools["get_macro_data"]["branches"]["eu"]["status"] == "down"
    assert tools["get_macro_data"]["status"] == "down"


@pytest.mark.parametrize(
    ("tool", "branch"),
    [
        ("get_historical_data", "crypto_tr"),
        ("get_technical_analysis", "crypto_tr"),
        ("get_crypto_market", "btcturk_ohlc"),
    ],
)
def test_empty_btcturk_candles_are_covered_by_the_tradingview_fallback(tool, branch):
    degraded = evaluate_tools(_all_ok(btcturk_graph="error"))
    down = evaluate_tools(_all_ok(btcturk_graph="error", btcturk_ohlc_fallback="error"))

    assert degraded[tool]["branches"][branch]["status"] == "degraded"
    assert down[tool]["branches"][branch]["status"] == "down"


async def test_btcturk_fallback_probe_runs_the_provider_fallback(monkeypatch):
    import health

    calls = []
    monkeypatch.setattr(
        health, "fetch_fallback_bars", lambda *args: calls.append(args) or [{"close": 1.0}]
    )
    body, status_code = await check_dependencies(
        dependencies=dependencies_for(["btcturk_ohlc_fallback"]), timeout_seconds=1
    )

    assert status_code == 200, body
    pair, interval_minutes, from_ts, to_ts = calls[0]
    assert (pair, interval_minutes) == ("BTCTRY", 1440)
    assert to_ts - from_ts == 7 * 86400


def test_unconfigured_fallback_does_not_cover_a_failed_primary():
    tools = evaluate_tools(_all_ok(fred_us="error", bls="not_configured"))

    assert tools["get_macro_data"]["branches"]["us"]["status"] == "down"


def test_unconfigured_optional_source_is_not_an_outage():
    tools = evaluate_tools(_all_ok(tcmb_evds="not_configured"))

    assert tools["get_evds_data"]["status"] == "not_configured"


def test_unprobed_upstream_is_listed_next_to_a_branch(monkeypatch):
    import health

    monkeypatch.setattr(
        health,
        "TOOL_CHECKS",
        (health._tool("get_quote", "fx", "canlidoviz", not_probed=("x",)),),
    )

    fx_quote = evaluate_tools(_all_ok())["get_quote"]["branches"]["fx"]
    assert fx_quote["status"] == "ok"
    assert fx_quote["not_probed"] == ["x"]


def test_economic_calendar_survives_on_the_page_scrape_when_the_api_fails():
    tools = evaluate_tools(_all_ok(doviz="error"))

    assert tools["get_economic_calendar"]["status"] == "degraded"


def test_energy_quotes_fail_alone_when_the_doviz_api_is_down():
    tools = evaluate_tools(_all_ok(doviz_api="error"))

    assert tools["get_quote"]["branches"]["fx_energy"]["status"] == "down"
    assert tools["get_quote"]["branches"]["fx"]["status"] == "ok"


def test_branch_without_probed_sources_is_not_monitored(monkeypatch):
    import health

    monkeypatch.setattr(
        health, "TOOL_CHECKS", (health._tool("get_quote", "fx", not_probed=("x",)),)
    )

    assert evaluate_tools(_all_ok())["get_quote"]["status"] == "not_monitored"


def test_bist_quote_is_down_when_the_tradingview_websocket_fails():
    tools = evaluate_tools(_all_ok(tradingview_websocket="error"))

    assert tools["get_quote"]["branches"]["bist"]["status"] == "down"
    assert tools["get_quote"]["branches"]["us"]["status"] == "ok"


def _named_dependencies(*keys):
    return [
        Dependency(DEPENDENCIES[key].name, f"https://example.test/{key}")
        for key in keys
    ]


async def test_tool_aware_health_stays_200_when_a_fallback_covers_an_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.path == "/fred_us" else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=_named_dependencies("fred_us", "bls"),
            client=client,
            timeout_seconds=1,
            tool_aware=True,
        )

    assert status_code == 200
    assert body["status"] == "degraded"
    assert body["down_tools"] == []
    assert body["services"]["FRED US CPI"]["status"] == "error"
    assert body["tools"]["get_macro_data"]["branches"]["us"]["status"] == "degraded"


async def test_tool_aware_health_fails_when_a_tool_has_no_source_left():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.path == "/mynet" else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=_named_dependencies("mynet", "kap"),
            client=client,
            timeout_seconds=1,
            tool_aware=True,
        )

    assert status_code == 503
    assert body["status"] == "unhealthy"
    assert body["down_tools"] == ["get_news (bist)"]


async def test_selected_service_health_still_fails_on_any_service(monkeypatch):
    import app as app_module

    captured = {}

    async def fake_check_dependencies(*, dependencies, mcp_app, tool_aware):
        captured["tool_aware"] = tool_aware
        return {"status": "degraded"}, 503

    monkeypatch.setattr(app_module, "check_dependencies", fake_check_dependencies)

    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://health.test"
    ) as client:
        response = await client.get("/health?service=fred_us")

    assert response.status_code == 503
    assert captured["tool_aware"] is False


async def test_new_upstream_probes_validate_what_borsapy_reads():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request
        if request.url.host == "a.canlidoviz.com":
            return httpx.Response(
                200, json={"1791849600": "49.3|49.4|49.3|49.4", "1791936000": "49.4|49.5|49.3|49.42"}
            )
        if request.url.host == "www.hedeffiyat.com.tr":
            # The real site serves the stock list with HTTP 404.
            return httpx.Response(
                404, text='<option value="/senet/thyao-turk-hava-yollari-a.o.-410">'
            )
        if request.url.host == "www.borsaistanbul.com":
            return httpx.Response(
                200,
                text=(
                    "BILESEN KODU;BULTEN_ADI;ENDEKS KODU;ENDEKS ADI\n"
                    "CONSTITUENT CODE;CONSTITUENT NAME;INDEX CODE;INDEX NAME\n"
                    "AEFES.E;ANADOLU EFES;XU100;BIST 100\n"
                ),
            )
        raise AssertionError(f"Unexpected health request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies_for(["canlidoviz", "hedeffiyat", "borsa_istanbul_csv"]),
            client=client,
            timeout_seconds=1,
        )

    assert status_code == 200, body
    assert seen["a.canlidoviz.com"].url.params["itemDataId"] == "1"
    assert seen["a.canlidoviz.com"].url.params["period"] == "DAILY"


async def test_hedeffiyat_probe_fails_when_stock_links_disappear():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html>Not Found</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies_for(["hedeffiyat"]), client=client, timeout_seconds=1
        )

    assert status_code == 503
    assert body["services"]["Hedeffiyat"]["error"].startswith("ValueError")


async def test_bist_csv_probe_fails_without_xu100_components():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="BILESEN KODU;ENDEKS KODU\nAEFES.E;XU030\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies_for(["borsa_istanbul_csv"]),
            client=client,
            timeout_seconds=1,
        )

    assert status_code == 503
    assert "XU100" in body["services"]["Borsa Istanbul Index Components"]["error"]


async def test_custom_probe_reports_ok_and_error():
    def failing_probe():
        raise ValueError("no last price")

    dependencies = [
        Dependency("Socket OK", "wss://example.test/ok", probe=lambda: None),
        Dependency("Socket Down", "wss://example.test/down", probe=failing_probe),
    ]
    body, status_code = await check_dependencies(
        dependencies=dependencies, timeout_seconds=1
    )

    assert status_code == 503
    assert body["services"]["Socket OK"]["status"] == "ok"
    assert body["services"]["Socket Down"]["error"] == "ValueError: no last price"


async def test_endpoint_probes_hit_the_endpoints_tools_use():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen[path] = request
        if path == "/v1/ohlcs":
            return httpx.Response(200, json=[{"time": 1, "close": 4098622.0}])
        if path.endswith("/MaliTablo"):
            return httpx.Response(200, json={"value": [{"itemCode": "1", "value1": 5.1}]})
        if path.endswith("/getScreenerDataNEW"):
            return httpx.Response(200, json={"d": json.dumps([{"Hisse": "THYAO - THY"}])})
        if path.endswith("/GetSermayeArttirimlari"):
            return httpx.Response(200, json={"d": json.dumps([{"HISSE_KODU": "THYAO"}])})
        if path == "/ekonomik-takvim":
            return httpx.Response(200, text='<div id="calendar-content-0"></div>')
        raise AssertionError(f"Unexpected health request: {request.url}")

    keys = [
        "btcturk_graph",
        "is_yatirim",
        "is_yatirim_screener",
        "is_yatirim_company_info",
        "doviz_calendar_page",
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies_for(keys), client=client, timeout_seconds=1
        )

    assert status_code == 200, body
    assert seen["/v1/ohlcs"].url.params["pair"] == "BTCTRY"
    statements = seen["/_layouts/15/IsYatirim.Website/Common/Data.aspx/MaliTablo"]
    # Fewer than four periods come back empty, so the probe must ask for four.
    assert {f"year{i}" for i in range(1, 5)} <= set(statements.url.params)
    assert "year5" not in statements.url.params


@pytest.mark.parametrize(
    ("key", "path", "response"),
    [
        ("btcturk_graph", "/v1/ohlcs", httpx.Response(200, json=[])),
        ("is_yatirim", "/MaliTablo", httpx.Response(200, json={"value": []})),
        ("is_yatirim_screener", "/getScreenerDataNEW", httpx.Response(200, json={"d": "[]"})),
        ("is_yatirim_company_info", "/GetSermayeArttirimlari", httpx.Response(200, json={"d": "[]"})),
        ("doviz_calendar_page", "/ekonomik-takvim", httpx.Response(200, text="<html></html>")),
    ],
)
async def test_endpoint_probes_fail_on_empty_answers(key, path, response):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(path)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, status_code = await check_dependencies(
            dependencies=dependencies_for([key]), client=client, timeout_seconds=1
        )

    assert status_code == 503
    assert body["services"][DEPENDENCIES[key].name]["status"] == "error"


async def test_coroutine_probe_is_awaited():
    calls = []

    async def async_probe():
        calls.append("ran")

    body, status_code = await check_dependencies(
        dependencies=[Dependency("Async", "https://example.test", probe=async_probe)],
        timeout_seconds=1,
    )

    assert status_code == 200
    assert calls == ["ran"]


async def test_doviz_api_probe_fails_when_no_price_comes_back(monkeypatch):
    import health
    from providers import dovizcom_legacy_provider

    class Unauthorized:
        guncel_deger = None
        error_message = "HTTP 401"

    async def fake_current(self, asset):
        assert asset == "WTI"
        return Unauthorized()

    monkeypatch.setattr(
        dovizcom_legacy_provider.DovizcomProvider, "get_asset_current", fake_current
    )

    with pytest.raises(ValueError, match="HTTP 401"):
        await health._probe_doviz_api()


def test_bond_probe_fails_when_doviz_returns_no_yields(monkeypatch):
    import health

    monkeypatch.setattr(health.DovizcomBondsPageProvider, "get_bond_yields", lambda self: [])

    with pytest.raises(ValueError, match="no bond yields"):
        health._probe_doviz_bonds()


def _serve_tahvil_page(monkeypatch, html):
    import health

    monkeypatch.setattr(
        health.DovizcomBondsPageProvider,
        "_get",
        lambda self, url, **kw: httpx.Response(
            200, content=html.encode("utf-8"), request=httpx.Request("GET", url)
        ),
    )


def test_bond_probe_parses_the_markup_the_tool_parses(monkeypatch):
    import health
    from test_bond_yields import BONDS_PAGE

    _serve_tahvil_page(monkeypatch, BONDS_PAGE)

    health._probe_doviz_bonds()


def test_bond_probe_fails_when_the_bond_table_markup_changes(monkeypatch):
    import health
    from borsapy.exceptions import DataNotAvailableError

    _serve_tahvil_page(monkeypatch, "<html><table id='commodities-v2'></table></html>")

    with pytest.raises(DataNotAvailableError, match="markup changed"):
        health._probe_doviz_bonds()
