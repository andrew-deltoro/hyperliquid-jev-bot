import asyncio
import json
from datetime import date

import httpx
import pytest

from jevbot.adapters import (
    HyperliquidReader,
    UpstreamError,
    local_macro,
    select_active,
    skylark_health,
)
from jevbot.decisions import DecisionError, DemoProvider, JevProvider
from jevbot.journal import Journal
from jevbot.models import Instrument, Settings


@pytest.fixture
def journal():
    obj = Journal(":memory:")
    yield obj
    obj.close()


def good_response():
    return {
        "model": "jev-test",
        "answers": {
            "selection": {
                "type": "choice",
                "choice": "WAIT",
                "confidence": 0.8,
                "probabilities": {"WAIT": 1},
            }
        },
    }


async def test_jev_real_request_contract(journal):
    def handler(request):
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == "Bearer fake-test-only"
        body = json.loads(request.content)
        assert set(body) == {"model", "state", "questions"}
        assert body["questions"]["selection"]["type"] == "choice"
        return httpx.Response(200, json=good_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = JevProvider(client, journal, Settings(), api_key="fake-test-only")
        result = await provider.choose({"test": True}, {"WAIT": "Wait"}, "test")
        assert result.provider == "jev"
        assert result.model == "jev-test"


async def test_jev_missing_key_no_network(journal, monkeypatch):
    monkeypatch.delenv("JEV_AUTH_VIA_PROXY", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Network called"))
    ) as client:
        provider = JevProvider(client, journal, Settings(), api_key="")
        with pytest.raises(DecisionError, match="JEV_UNAVAILABLE"):
            await provider.choose({}, {"WAIT": "Wait"}, "test")
        assert journal.reserve_call(1)  # Missing credentials do not spend call reservations.


@pytest.mark.parametrize(
    "status,expected", [(401, "JEV_HTTP_401"), (429, "JEV_HTTP_429"), (500, "JEV_HTTP_500")]
)
async def test_jev_http_failure_is_closed(journal, status, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as client:
        provider = JevProvider(client, journal, Settings(), api_key="fake-test-only")
        with pytest.raises(DecisionError, match=expected):
            await provider.choose({}, {"WAIT": "Wait"}, "test")


@pytest.mark.parametrize("mutation", ["invented", "missing", "nan", "wrongtype", "extra_option"])
async def test_jev_malformed_responses(journal, mutation):
    body = good_response()
    answer = body["answers"]["selection"]
    if mutation == "invented":
        answer["choice"] = "BUY_EVERYTHING"
    elif mutation == "missing":
        del answer["confidence"]
    elif mutation == "nan":
        answer["confidence"] = "NaN"
    elif mutation == "wrongtype":
        answer["type"] = "noul"
    else:
        answer["probabilities"]["UNREQUESTED"] = 0
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        provider = JevProvider(client, journal, Settings(), api_key="fake-test-only")
        with pytest.raises(DecisionError, match="JEV_INVALID_RESPONSE"):
            await provider.choose({}, {"WAIT": "Wait"}, "test")


async def test_jev_timeout(journal):
    def handler(request):
        raise httpx.ReadTimeout("timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DecisionError, match="JEV_TIMEOUT"):
            await JevProvider(client, journal, Settings(), api_key="fake").choose(
                {}, {"WAIT": "wait"}, "test"
            )


async def test_total_jev_deadline(journal):
    async def handler(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=good_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = JevProvider(
            client, journal, Settings(jev_timeout_seconds=0.01), api_key="fake-test-only"
        )
        with pytest.raises(DecisionError, match="JEV_TIMEOUT"):
            await provider.choose({}, {"WAIT": "Wait"}, "test")


async def test_jev_never_follows_redirect_with_credentials(journal):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://untrusted.example/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = JevProvider(client, journal, Settings(), api_key="fake-test-only")
        with pytest.raises(DecisionError, match="JEV_HTTP_307"):
            await provider.choose({}, {"WAIT": "Wait"}, "test")
    assert calls == ["https://api.typesafe.ai/v1/systemone"]


async def test_jev_budget_prevents_network(journal):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Network called"))
    ) as client:
        provider = JevProvider(client, journal, Settings(max_jev_calls_per_hour=0), api_key="fake")
        with pytest.raises(DecisionError, match="JEV_CALL_BUDGET"):
            await provider.choose({}, {"WAIT": "Wait"}, "test")


async def test_demo_cannot_touch_real_data():
    with pytest.raises(DecisionError, match="FORBIDDEN"):
        await DemoProvider().choose({"data_source": "hyperliquid"}, {"WAIT": "Wait"}, "test")


async def test_reader_rejects_nonallowlisted_action():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Network called"))
    ) as client:
        with pytest.raises(ValueError):
            await HyperliquidReader(client).info({"type": "order"})


async def test_discovery_uses_read_only_endpoint():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        body = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.host == "api.hyperliquid.xyz"
        assert "Authorization" not in request.headers
        if body["type"] == "perpDexs":
            return httpx.Response(200, json=[None, {"name": "xyz"}])
        if body["type"] == "metaAndAssetCtxs":
            dex = body["dex"]
            return httpx.Response(
                200,
                json=[
                    {
                        "universe": [
                            {"name": "GOLD" if dex else "BTC", "szDecimals": 3, "maxLeverage": 5}
                        ]
                    },
                    [{"dayNtlVlm": "1000"}],
                ],
            )
        return httpx.Response(
            200,
            json={
                "tokens": [{"index": 0, "szDecimals": 2}],
                "universe": [{"name": "@1", "index": 1, "tokens": [0, 0]}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        instruments, warnings = await HyperliquidReader(client).discover()
    assert not warnings
    assert {x.symbol for x in instruments} == {"BTC", "xyz:GOLD", "@1"}
    assert set(paths) == {"/info"}
    assert not next(x for x in instruments if x.kind == "spot").analysis_supported


async def test_candle_identity_is_validated():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[{"s": "ETH", "i": "1m"}]))
    ) as client:
        with pytest.raises(UpstreamError, match="INVALID_CANDLE_SCHEMA"):
            await HyperliquidReader(client).candles("BTC", 180, 1800000000000)


def test_active_selection_excludes_delisted_and_spot():
    registry = [
        Instrument(id="1", symbol="BTC", kind="perp", size_decimals=3, volume_usd=2),
        Instrument(
            id="2", symbol="OLD", kind="perp", size_decimals=3, delisted=True, volume_usd=100
        ),
        Instrument(id="3", symbol="SPOT", kind="spot", size_decimals=3, volume_usd=200),
        Instrument(id="4", symbol="xyz:GOLD", kind="perp", size_decimals=3, volume_usd=3),
    ]
    active, missing = select_active(registry, [], True, 1)
    assert active[0].symbol == "xyz:GOLD"
    active, missing = select_active(registry, ["BTC", "OLD"], False, 8)
    assert [x.symbol for x in active] == ["BTC"]
    assert missing == ["OLD"]


async def test_skylark_health_does_not_buy_data():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/source-health"
        assert "payment-signature" not in request.headers
        return httpx.Response(200, json={"status": "ready", "as_of": "2026-10-18"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await skylark_health(client)
        assert result["paid_readings_loaded"] is False


def bundle(day="2026-10-18"):
    return {
        "readings": [
            {"official_name": name, "value": 1.2, "as_of": day}
            for name in (
                "Financial Liquidity Proxy",
                "Financial Liquidity Index",
                "Global Funding Conditions",
            )
        ]
    }


@pytest.mark.parametrize(
    "day,status",
    [
        ("2026-10-18", "available"),
        ("2020-01-01", "stale"),
        ("2099-01-01", "stale"),
    ],
)
def test_local_macro_dates(tmp_path, day, status):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle(day)))
    result = local_macro(str(path), 10, today=date(2026, 10, 22))
    assert result["status"] == status
    assert result["direction"] == "unknown"


def test_macro_missing_is_unknown():
    assert local_macro("", 10)["direction"] == "unknown"


def test_macro_duplicate_is_rejected(tmp_path):
    data = bundle()
    data["readings"][1] = data["readings"][0]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    assert local_macro(str(path), 10)["reason"] == "INVALID_LOCAL_BUNDLE"
