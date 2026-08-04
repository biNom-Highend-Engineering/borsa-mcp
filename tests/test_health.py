import httpx
import pytest

from health import (
    DEPENDENCIES,
    Dependency,
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
        "python_process": "ok",
        "asgi": "ok",
        "http_routing": "ok",
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
