import json

import pytest

from jevbot.decisions import DecisionError
from jevbot.improvement import (
    evaluate,
    improvement_report,
    markdown_improvement,
    propose,
    research_memory,
    review_if_due,
)
from jevbot.journal import Journal
from jevbot.models import Decision, ImprovementSettings, Settings

NOW = 1800000000


@pytest.fixture
def journal():
    value = Journal(":memory:")
    yield value
    value.close()


def add(
    journal,
    number,
    net=1,
    *,
    state="CLOSED",
    source="hyperliquid:jev",
    experiment="e1",
    instrument="perp:native:BTC",
    profile="standard",
    direction="long",
    regime="expansion",
    risk=1,
    weather="unmeasured",
    session="unmeasured",
    exit_ms=None,
):
    payload = {
        "candidate": {
            "strategy": "volatility_breakout",
            "profile": profile,
            "direction": direction,
        },
        "net_pnl": net,
        "initial_stop_risk": risk,
        "exit_ms": exit_ms
        if exit_ms is not None
        else int((NOW - 86400 * 10 + number * 3600) * 1000),
        "weather_state": weather,
        "reference_session": session,
    }
    with journal.db:
        journal.db.execute(
            "INSERT INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(number),
                experiment,
                source,
                instrument,
                "rules:volatility_breakout",
                regime,
                number * 60000,
                NOW - 86400 * 11,
                state,
                json.dumps(payload),
            ),
        )


def permissive():
    # Labels only, not a strategy-entry gate.
    return Settings(improvement=ImprovementSettings(min_closed=2, min_days=1))


class Reviewer:
    name = "jev"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def choose(self, state, options, purpose):
        self.calls += 1
        assert purpose == "improvement_research_shortlist"
        assert state["simulation_only"]
        if self.fail:
            raise DecisionError("JEV_CALL_BUDGET")
        choice = next(key for key in options if key != "WAIT")
        return Decision(
            choice=choice,
            confidence=1,
            probabilities={k: float(k == choice) for k in options},
            model="test-fixture",
            provider="jev",
            elapsed_ms=0,
        )


def test_empty_and_synthetic_are_not_winners(journal):
    add(journal, 0, source="synthetic:demo_rule_stub")
    assert evaluate(journal, Settings(), NOW)["rows"] == []
    assert not propose(evaluate(journal, Settings(), NOW), 3)


def test_thin_sample_does_not_claim_success_or_block_ideas(journal):
    add(journal, 0)
    report = evaluate(journal, Settings(), NOW)
    assert report["rows"][0]["status"] == "EARLY_POSITIVE"
    assert propose(report, 3)[0]["activation"] == "NOT_IMPLEMENTED_RECOMMENDATION_ONLY"


def test_mature_positive_preserved_with_cost_adjusted_r(journal):
    add(journal, 0, net=2, risk=2)
    add(journal, 1, net=3, risk=1)
    row = evaluate(journal, permissive(), NOW)["rows"][0]
    assert row["mean_net_r"] == 2
    assert row["net_pnl"] == 5
    assert row["status"] == "KEEP_CANDIDATE"


def test_high_win_rate_does_not_hide_large_loss(journal):
    for i, net in enumerate([1, 1, 1, 1, -5]):
        add(journal, i, net=net)
    row = evaluate(journal, permissive(), NOW)["rows"][0]
    assert row["wins"] == 4
    assert row["status"] == "REWORK_CANDIDATE"
    assert row["closed_sequence_drawdown_r"] == 5


def test_recent_decay_not_hidden_by_all_time_profit(journal):
    settings = permissive()
    settings.improvement.recent_closed = 2
    for i, net in enumerate([4, 4, -1, -1]):
        add(journal, i, net=net)
    row = evaluate(journal, settings, NOW)["rows"][0]
    assert row["status"] == "DECAY_REVIEW"
    assert row["recent_mean_net_r"] == -1


def test_risk_review(journal):
    for i, net in enumerate([10, -7]):
        add(journal, i, net=net)
    assert evaluate(journal, permissive(), NOW)["rows"][0]["status"] == "RISK_REVIEW"


def test_missing_paths_not_scored_as_wins(journal):
    add(journal, 0)
    add(journal, 1, net=100, state="UNRESOLVED")
    add(journal, 2, net=100, state="OPEN")
    row = evaluate(journal, permissive(), NOW)["rows"][0]
    assert row["status"] == "DATA_REVIEW"
    assert row["closed"] == 1 and row["net_pnl"] == 1
    assert row["states"]["OPEN"] == 1
    assert not propose(evaluate(journal, permissive(), NOW), 3)


@pytest.mark.parametrize(
    "change",
    [
        {"experiment": "e2"},
        {"instrument": "perp:xyz:xyz:TEST"},
        {"profile": "tight"},
        {"direction": "short"},
        {"regime": "range"},
        {"weather": "storm"},
        {"session": "overnight"},
    ],
)
def test_cohorts_are_never_pooled(journal, change):
    add(journal, 0)
    add(journal, 1, **change)
    rows = evaluate(journal, permissive(), NOW)["rows"]
    assert len(rows) == 2
    assert all(row["closed"] == 1 for row in rows)


@pytest.mark.parametrize("risk,net", [(0, 1), (-1, 1), ("nan", 1), (1, "inf"), (1e-300, 1e300)])
def test_invalid_outcomes_visible_and_excluded(journal, risk, net):
    add(journal, 0, risk=risk, net=net)
    report = evaluate(journal, Settings(), NOW)
    assert report["malformed_tickets_excluded"] == 1
    assert report["rows"] == []
    json.dumps(report, allow_nan=False)


def test_no_future_outcomes(journal):
    add(journal, 0, exit_ms=int((NOW + 3600) * 1000))
    assert evaluate(journal, Settings(), NOW)["rows"] == []


async def test_scheduled_reviews_durable_no_duplicate_ideas_or_repeat_calls(journal):
    add(journal, 0)
    provider = Reviewer()
    await review_if_due(journal, Settings(), provider, now=NOW)
    assert provider.calls == 1
    assert await review_if_due(journal, Settings(), provider, now=NOW + 10) is None
    second = await review_if_due(journal, Settings(), provider, now=NOW + 21600)
    assert not second["new_evidence"]
    assert provider.calls == 1
    assert journal.db.execute("SELECT count(*) FROM research_ideas").fetchone()[0] == 1
    assert journal.db.execute("SELECT count(*) FROM improvement_reviews").fetchone()[0] == 2
    idea = json.loads(journal.db.execute("SELECT payload FROM research_ideas").fetchone()[0])
    assert idea["status"] == "JEV_SHORTLISTED"


async def test_pending_ticket_is_not_new_resolved_learning(journal):
    add(journal, 0)
    provider = Reviewer()
    await review_if_due(journal, Settings(), provider, now=NOW)
    add(journal, 1, state="PENDING")
    second = await review_if_due(journal, Settings(), provider, now=NOW + 21600)
    assert second["new_evidence"] and not second["new_resolved_evidence"]
    assert provider.calls == 1


async def test_failed_jev_does_not_discard_local_evaluation(journal):
    add(journal, 0)
    report = await review_if_due(journal, Settings(), Reviewer(fail=True), now=NOW)
    assert report["jev_review"]["status"] == "JEV_CALL_BUDGET"
    assert journal.db.execute("SELECT count(*) FROM research_ideas").fetchone()[0] == 1


async def test_memory_is_context_specific_and_bounded(journal):
    add(journal, 0)
    await review_if_due(journal, Settings(), now=NOW)
    memory = research_memory(journal, "e1", "perp:native:BTC", "expansion")
    assert len(memory["rows"]) == 1
    assert not research_memory(journal, "e2", "perp:native:BTC", "expansion")["rows"]
    assert not research_memory(journal, "e1", "perp:native:ETH", "expansion")["rows"]
    assert not research_memory(journal, "e1", "perp:native:BTC", "range")["rows"]
    assert not research_memory(
        journal, "e1", "perp:native:BTC", "expansion", weather_state="storm"
    )["rows"]


async def test_recursive_review_preserves_old_evidence_and_updates_status(journal):
    add(journal, 0)
    settings = permissive()
    await review_if_due(journal, settings, now=NOW)
    add(journal, 1)
    await review_if_due(journal, settings, now=NOW + 21600)
    rows = journal.db.execute("SELECT payload FROM improvement_reviews ORDER BY id").fetchall()
    assert json.loads(rows[0][0])["rows"][0]["status"] == "EARLY_POSITIVE"
    assert json.loads(rows[1][0])["rows"][0]["status"] == "KEEP_CANDIDATE"
    assert journal.db.execute("SELECT count(*) FROM shadow_trades").fetchone()[0] == 2


async def test_disable_and_report_no_network(journal):
    settings = Settings(improvement=ImprovementSettings(enabled=False))
    provider = Reviewer()
    assert await review_if_due(journal, settings, provider, now=NOW) is None
    assert provider.calls == 0
    text = markdown_improvement(improvement_report(journal, settings))
    assert "No backtest approval gate" in text
    assert "Not run yet" in text


async def test_ideas_never_change_settings_or_insert_trades(journal):
    add(journal, 0)
    settings = Settings()
    before = settings.model_dump()
    await review_if_due(journal, settings, Reviewer(), now=NOW)
    assert settings.model_dump() == before
    assert journal.db.execute("SELECT count(*) FROM shadow_trades").fetchone()[0] == 1


async def test_historical_shortlist_does_not_hide_deterioration(journal, monkeypatch):
    monkeypatch.setattr("jevbot.improvement.time.time", lambda: NOW + 21600)
    add(journal, 0)
    await review_if_due(journal, permissive(), Reviewer(), now=NOW)
    add(journal, 1, state="UNRESOLVED")
    data = improvement_report(journal, permissive())
    idea = data["ideas"][0]
    assert idea["status"] == "JEV_SHORTLISTED"
    assert idea["current_parent_status"] == "DATA_REVIEW"
    assert idea["last_shortlisted_at"] == NOW
    assert "Current parent: DATA_REVIEW" in markdown_improvement(data)


async def test_review_survives_restart(tmp_path):
    path = str(tmp_path / "journal.sqlite3")
    journal = Journal(path)
    add(journal, 0)
    await review_if_due(journal, Settings(), now=NOW)
    journal.close()
    restored = Journal(path)
    try:
        assert await review_if_due(restored, Settings(), now=NOW + 60) is None
        assert len(research_memory(restored, "e1", "perp:native:BTC", "expansion")["rows"]) == 1
    finally:
        restored.close()
