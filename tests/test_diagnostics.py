import pytest

from jevbot.decisions import DecisionError
from jevbot.demo import fixture
from jevbot.diagnostics import breakdown, explain, markdown_breakdown
from jevbot.engine import Engine
from jevbot.journal import Journal
from jevbot.models import Decision, Settings
from jevbot.shadow import scoreboard

NOW = 1800000000000


class TestProvider:
    __test__ = False
    name = "jev"

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    async def choose(self, state, options, purpose):
        self.calls += 1
        if self.behavior == "budget":
            raise DecisionError("JEV_CALL_BUDGET")
        choice = (
            "WAIT"
            if self.behavior == "router_wait"
            or (self.behavior == "candidate_wait" and purpose == "candidate_selection")
            else next(key for key in options if key != "WAIT")
        )
        return Decision(
            choice=choice,
            confidence=1,
            probabilities={key: float(key == choice) for key in options},
            model="controlled-test",
            provider="jev",
            elapsed_ms=0,
        )


@pytest.fixture
def journal():
    value = Journal(":memory:")
    yield value
    value.close()


@pytest.mark.parametrize(
    "behavior,stage,category",
    [
        ("router_wait", "router", "model_wait"),
        ("candidate_wait", "candidate", "model_wait"),
        ("budget", "router", "operational_limit"),
    ],
)
async def test_stage_and_category_do_not_confuse_wait_with_budget(
    journal, behavior, stage, category
):
    instrument, bars = fixture("breakout", NOW)
    result = await Engine(Settings(), journal, TestProvider(behavior)).analyze(
        instrument, bars, {}, now_ms=NOW
    )
    diagnostic = result["payload"]["diagnostics"]
    assert diagnostic["stage"] == stage
    assert diagnostic["category"] == category
    assert not diagnostic["model_reasoning_returned"]
    assert "spread_and_depth_unmeasured" in diagnostic["context_flags_not_causal_explanations"]


async def test_rule_baselines_continue_when_jev_declines(journal):
    instrument, bars = fixture("breakout", NOW)
    provider = TestProvider("router_wait")
    result = await Engine(Settings(), journal, provider).analyze(instrument, bars, {}, now_ms=NOW)
    assert result["payload"]["rule_setups"]["volatility_breakout"]["triggered"]
    tickets = journal.db.execute("SELECT lane,state FROM shadow_trades").fetchall()
    assert any(
        row["lane"] == "rules:volatility_breakout" and row["state"] == "PENDING" for row in tickets
    )
    assert not any(row["lane"] == "jev_policy" for row in tickets)
    assert provider.calls == 1  # Baseline collection did not create extra API calls.
    rows = scoreboard(journal)["rows"]
    assert len(rows) == 5  # Even the inactive JEV lane is visible, with zero tickets.
    jev = next(row for row in rows if row["lane"] == "jev_policy")
    assert jev["closed"] == jev["pending"] == jev["open"] == 0


async def test_jev_and_baseline_share_entry_clock(journal):
    instrument, bars = fixture("breakout", NOW)
    result = await Engine(Settings(), journal, TestProvider("select")).analyze(
        instrument, bars, {}, now_ms=NOW
    )
    assert result["status"] == "PROPOSAL_ONLY"
    import json

    tickets = journal.db.execute("SELECT lane,payload FROM shadow_trades").fetchall()
    assert "jev_policy" in {row["lane"] for row in tickets}
    assert len({json.loads(row["payload"])["entry_bar_ms"] for row in tickets}) == 1


async def test_duplicate_observation_does_not_duplicate_shadow_intents(journal):
    instrument, bars = fixture("breakout", NOW)
    engine = Engine(Settings(), journal, TestProvider("select"))
    await engine.analyze(instrument, bars, {}, now_ms=NOW)
    count = journal.db.execute("SELECT count(*) FROM shadow_trades").fetchone()[0]
    await engine.analyze(instrument, bars, {}, now_ms=NOW)
    assert journal.db.execute("SELECT count(*) FROM shadow_trades").fetchone()[0] == count


def test_legacy_wait_stage_is_not_invented():
    result = explain("JEV_WAIT", {})
    assert result["stage"] == "legacy_unknown"


def test_legacy_candidate_wait_is_identified():
    result = explain("JEV_WAIT", {"candidate_decision": {"choice": "WAIT"}})
    assert result["stage"] == "candidate"


def test_synthetic_provider_is_not_misrepresented_as_jev():
    result = explain("PROPOSAL_ONLY", {"decision_provider": "demo_rule_stub"})
    assert result["observed_reason"].startswith("Demo rule stub")


def test_source_filter_and_event_denominators(journal):
    journal.record(
        "hyperliquid:jev", "BTC", 1, "JEV_WAIT", {"route": "WAIT", "route_reason": "CACHED_ROUTE"}
    )
    journal.record("synthetic:demo_rule_stub", "TEST", 1, "NO_SETUP", {})
    journal.event("MARKET_DATA_ERROR", {"code": "TEST"})
    data = breakdown(journal, hours=0)
    assert data["decisions"] == 1
    assert data["rule_evidence_decisions"] == 0
    assert data["rows"][0]["percent_of_decisions"] == 100
    assert data["rows"][0]["route_origin"] == "CACHED_ROUTE"
    assert data["operational_events_not_in_decision_denominator"]["MARKET_DATA_ERROR"] == 1
    assert breakdown(journal, hours=0, source="all")["decisions"] == 2
    assert "not JEV's private reasoning" in markdown_breakdown(data)
