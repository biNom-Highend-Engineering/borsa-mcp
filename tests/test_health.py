import httpx
import pytest

from health import (
    DEPENDENCIES,
    Dependency,
    EXPECTED_TOOL_NAMES,
    check_mcp_tools,
    check_dependencies,
    configured_dependencies,
    dependencies_for,
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
        "Is Yatirim",
        "TEFAS",
        "Takasbank",
        "Doviz.com",
        "BTCTurk",
        "BTCTurk Graph API",
        "Coinbase",
        "World Bank",
        "TradingView Scanner",
        "FRED US CPI",
        "FRED Euro Area HICP",
        "BLS",
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


async def test_mcp_health_lists_contract_and_calls_a_tool():
    from unified_mcp_server import app

    result = await check_mcp_tools(app)

    assert result["status"] == "ok"
    assert result["tool_count"] == len(EXPECTED_TOOL_NAMES)
    assert result["protocol_checks"] == ["initialize", "tools/list", "tools/call"]
    assert result["smoke_tool"] == "scan_stocks"


async def test_mcp_health_fails_when_a_public_tool_is_missing():
    from fastmcp import FastMCP

    incomplete_app = FastMCP("incomplete")

    @incomplete_app.tool(name="scan_stocks")
    async def scan_stocks(help: bool = False) -> str:
        return "help" if help else "scan"

    result = await check_mcp_tools(
        incomplete_app,
        expected_tool_names=frozenset({"scan_stocks", "get_quote"}),
    )

    assert result["status"] == "error"
    assert "missing MCP tools: get_quote" in result["error"]


async def test_http_health_defaults_to_mcp_readiness_and_all_upstreams(monkeypatch):
    import app as app_module

    captured = {}

    async def fake_check_dependencies(*, dependencies, mcp_app):
        captured["dependencies"] = dependencies
        captured["mcp_app"] = mcp_app
        return {
            "status": "ok",
            "application": {"status": "ok", "tool_count": len(EXPECTED_TOOL_NAMES)},
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
    assert body["application"]["tool_count"] == len(EXPECTED_TOOL_NAMES)
    assert captured["dependencies"] is None
    assert captured["mcp_app"] is app_module.mcp
