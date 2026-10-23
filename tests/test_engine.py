import asyncio

import httpx
import pytest

from jevbot.decisions import DecisionError, DemoProvider, JevProvider
from jevbot.demo import fixture
from jevbot.engine import Engine
from jevbot.journal import Journal
from jevbot.models import Decision, Settings

NOW = 1800000000000


@pytest.fixture
def journal():
    obj = Journal(":memory:")
    yield obj
    obj.close()


class ChoiceProvider:
    name = "jev"

    def __init__(self, wait=False, delay=0):
        self.wait, self.delay, self.calls = wait, delay, 0

    async def choose(self, state, options, purpose):
        self.calls += 1
        await asyncio.sleep(self.delay)
        choice = "WAIT" if self.wait else next(k for k in options if k != "WAIT")
        return Decision(
            choice=choice,
            confidence=1,
            probabilities={k: float(k == choice) for k in options},
            model="test-contract",
            provider="jev",
            elapsed_ms=0,
        )


async def test_demo_end_to_end_proposal(journal):
    engine = Engine(Settings(), journal, DemoProvider())
    instrument, bars = fixture("breakout", NOW)
    result = await engine.analyze(instrument, bars, {}, data_source="synthetic", now_ms=NOW)
    assert result["status"] == "PROPOSAL_ONLY"
    assert result["payload"]["execution_authorized"] is False
    assert result["payload"]["sizing_preview"]["executable"] is False
    assert journal.recent()[0]["source"] == "synthetic:demo_rule_stub"


async def test_engine_forbids_demo_on_live(journal):
    engine = Engine(Settings(), journal, DemoProvider())
    instrument, bars = fixture("breakout", NOW)
    with pytest.raises(DecisionError, match="FORBIDDEN"):
        await engine.analyze(instrument, bars, {}, now_ms=NOW)


async def test_missing_jev_never_falls_back(journal, monkeypatch):
    monkeypatch.delenv("JEV_AUTH_VIA_PROXY", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as client:
        engine = Engine(Settings(), journal, JevProvider(client, journal, Settings(), api_key=""))
        instrument, bars = fixture("breakout", NOW)
        result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
        assert result["status"] == "JEV_UNAVAILABLE"
        assert "selected_candidate" not in result["payload"]


async def test_duplicate_completed_bar_costs_no_calls(journal):
    provider = ChoiceProvider()
    engine = Engine(Settings(), journal, provider)
    instrument, bars = fixture("breakout", NOW)
    await engine.analyze(instrument, bars, {}, now_ms=NOW)
    calls = provider.calls
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["status"] == "DUPLICATE_BAR"
    assert provider.calls == calls


async def test_wait_is_journaled(journal):
    engine = Engine(Settings(), journal, ChoiceProvider(wait=True))
    instrument, bars = fixture("breakout", NOW)
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["status"] == "JEV_WAIT"
    assert journal.recent()[0]["status"] == "JEV_WAIT"


async def test_shock_skips_model(journal):
    provider = ChoiceProvider()
    engine = Engine(Settings(), journal, provider)
    instrument, bars = fixture("shock", NOW)
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["status"] == "SHOCK_NO_ENTRY"
    assert provider.calls == 0


async def test_stale_data_skips_model(journal):
    provider = ChoiceProvider()
    instrument, bars = fixture("range", NOW)
    result = await Engine(Settings(), journal, provider).analyze(
        instrument, bars, {}, now_ms=NOW + 600000
    )
    assert result["status"] == "STALE_DATA"
    assert provider.calls == 0


async def test_expired_model_decision_is_not_a_proposal(journal):
    instrument, bars = fixture("breakout", NOW)
    provider = ChoiceProvider(delay=1.1)
    result = await Engine(Settings(decision_ttl_seconds=1), journal, provider).analyze(
        instrument, bars, {}, now_ms=NOW
    )
    assert result["status"] == "DECISION_EXPIRED"
    assert "selected_candidate" not in result["payload"]


async def test_spot_marked_unsupported(journal):
    instrument, bars = fixture("range", NOW)
    instrument = instrument.model_copy(update={"kind": "spot", "analysis_supported": False})
    provider = ChoiceProvider()
    result = await Engine(Settings(), journal, provider).analyze(instrument, bars, {}, now_ms=NOW)
    assert result["status"] == "UNSUPPORTED_MARKET"
    assert provider.calls == 0


async def test_cached_router_provenance_is_present(journal):
    provider = ChoiceProvider()
    engine = Engine(Settings(), journal, provider)
    instrument, bars = fixture("breakout", NOW)
    await engine.analyze(instrument, bars, {}, now_ms=NOW)
    # Shift identical scenario one bar forward to preserve regime but use a new bar identity.
    shifted = [
        bar.model_copy(update={"start_ms": bar.start_ms + 60000, "end_ms": bar.end_ms + 60000})
        for bar in bars
    ]
    before = provider.calls
    result = await engine.analyze(instrument, shifted, {}, now_ms=NOW + 60000)
    assert result["payload"]["route_reason"] == "CACHED_ROUTE"
    assert result["payload"]["router_decision"]["model"] == "test-contract"
    assert provider.calls == before + 1  # Only candidate check; no second router call.


def seed_route(journal, engine, instrument, route, *, age=1, regime="mixed"):
    saved_decision = Decision(
        choice=route,
        confidence=1,
        probabilities={route: 1},
        model="previous-routing-model",
        provider="jev",
        elapsed_ms=0,
    ).model_dump()
    journal.set(
        f"route:hyperliquid:jev:{instrument.id}",
        {
            "route": route,
            "selected_bar": NOW - 60000 * (age + 1),
            "evaluated_bar": NOW - 60000 * (age + 1),
            "regime": regime,
            "config_hash": engine.config_hash,
            "decision": saved_decision,
            "applied_decision": saved_decision,
        },
    )


async def test_minimum_dwell_preserves_eligible_route_and_provenance(journal):
    instrument, bars = fixture("breakout", NOW)
    provider = ChoiceProvider()
    engine = Engine(Settings(), journal, provider)
    seed_route(journal, engine, instrument, "volatility_breakout:wide")
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["payload"]["route"] == "volatility_breakout:wide"
    assert result["payload"]["route_reason"] == "MINIMUM_DWELL"
    assert result["payload"]["router_decision"]["choice"] == "volatility_breakout:standard"
    assert result["payload"]["applied_route_decision"]["model"] == "previous-routing-model"


async def test_dwell_expiry_allows_new_profile(journal):
    instrument, bars = fixture("breakout", NOW)
    engine = Engine(Settings(), journal, ChoiceProvider())
    seed_route(journal, engine, instrument, "volatility_breakout:wide", age=4)
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["payload"]["route"] == "volatility_breakout:standard"
    assert result["payload"]["route_reason"] == "JEV_SELECTION"


async def test_wait_bypasses_minimum_dwell(journal):
    instrument, bars = fixture("breakout", NOW)
    engine = Engine(Settings(), journal, ChoiceProvider(wait=True))
    seed_route(journal, engine, instrument, "volatility_breakout:wide")
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["status"] == "JEV_WAIT"
    assert result["payload"]["route"] == "WAIT"


async def test_new_regime_drops_ineligible_strategy_immediately(journal):
    instrument, bars = fixture("breakout", NOW)
    engine = Engine(Settings(), journal, ChoiceProvider())
    seed_route(journal, engine, instrument, "mean_reversion:tight", regime="range")
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["payload"]["route"] == "volatility_breakout:standard"
    assert result["payload"]["route_reason"] == "JEV_SELECTION"


async def test_configuration_change_invalidates_cached_route(journal):
    instrument, bars = fixture("breakout", NOW)
    previous = Engine(Settings(), journal, ChoiceProvider())
    seed_route(journal, previous, instrument, "volatility_breakout:wide", regime="expansion")
    provider = ChoiceProvider()
    engine = Engine(Settings(jev_model="new-model-version"), journal, provider)
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["payload"]["route_reason"] == "JEV_SELECTION"
    assert provider.calls == 2


async def test_expired_route_is_not_cached(journal):
    instrument, bars = fixture("breakout", NOW)
    engine = Engine(Settings(decision_ttl_seconds=1), journal, ChoiceProvider(delay=1.1))
    result = await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert result["status"] == "DECISION_EXPIRED"
    assert journal.get(f"route:hyperliquid:jev:{instrument.id}") is None
