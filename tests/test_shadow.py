import json
import sqlite3
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jevbot.cli import preserve_shadow_watchlist
from jevbot.journal import Journal
from jevbot.models import Candidate, Candle, Instrument, Settings, ShadowSettings
from jevbot.shadow import advance, enqueue, markdown_scoreboard, scoreboard

NOW = 1800000000000
SOURCE = "hyperliquid:jev"
INSTRUMENT = Instrument(id="perp:native:TEST", symbol="TEST", kind="perp", size_decimals=3)


@pytest.fixture
def journal():
    value = Journal(":memory:")
    yield value
    value.close()


def candidate(direction="long", hold=90):
    return Candidate(
        id="candidate_test",
        instrument_id=INSTRUMENT.id,
        strategy="volatility_breakout",
        profile="standard",
        direction=direction,
        entry=100,
        stop=98 if direction == "long" else 102,
        target=104 if direction == "long" else 96,
        max_hold_minutes=hold,
        reasons=["TEST"],
    )


def bar(index=1, *, open=100, high=101, low=99, close=100):
    start = NOW + index * 60000
    return Candle(
        start_ms=start, end_ms=start + 59999, open=open, high=high, low=low, close=close, volume=100
    )


def queue(journal, *, direction="long", hold=90, settings=None, **kwargs):
    values = {
        "settings": settings or Settings(),
        "experiment": "experiment1",
        "source": SOURCE,
        "instrument": INSTRUMENT,
        "lane": "rules:volatility_breakout",
        "candidate": candidate(direction, hold),
        "regime": "expansion",
        "signal_ms": NOW - 60000,
        "observed_ms": NOW + 1000,
    }
    with journal.db:
        return enqueue(journal, **{**values, **kwargs})


def ticket(journal):
    row = journal.db.execute("SELECT * FROM shadow_trades ORDER BY rowid LIMIT 1").fetchone()
    return {**dict(row), "payload": json.loads(row["payload"])}


def progress(journal, bars):
    advance(journal, SOURCE, INSTRUMENT.id, bars)
    return ticket(journal)


def test_no_fill_on_signal_or_already_open_bar(journal):
    queue(journal)
    record = progress(journal, [bar(-1), bar(0, high=110, low=90)])
    assert record["state"] == "PENDING"
    assert "entry_price" not in record["payload"]
    assert record["payload"]["entry_bar_ms"] == NOW + 60000


@pytest.mark.parametrize("direction", ["long", "short"])
def test_future_open_uses_adverse_slippage_and_fee(journal, direction):
    queue(journal, direction=direction)
    record = progress(journal, [bar()])
    assert record["state"] == "OPEN"
    entry = Decimal(record["payload"]["entry_price"])
    assert entry == Decimal("100.02") if direction == "long" else entry == Decimal("99.98")
    assert Decimal(record["payload"]["entry_fee"]) > 0


@pytest.mark.parametrize("direction", ["long", "short"])
def test_both_hit_intrabar_is_stop_first(journal, direction):
    queue(journal, direction=direction)
    record = progress(journal, [bar(high=105, low=95)])
    assert record["state"] == "CLOSED"
    assert record["payload"]["exit_reason"] == "BOTH_HIT_STOP_FIRST"
    assert record["payload"]["ambiguous_bar"]
    assert Decimal(record["payload"]["net_pnl"]) < 0


@pytest.mark.parametrize("direction", ["long", "short"])
def test_adverse_gap_stop_uses_open_not_stop(journal, direction):
    queue(journal, direction=direction)
    progress(journal, [bar()])
    gap = (
        bar(2, open=95, high=97, low=94, close=96)
        if direction == "long"
        else bar(2, open=105, high=106, low=103, close=104)
    )
    record = progress(journal, [bar(), gap])
    assert record["payload"]["exit_reason"] == "GAP_STOP"
    exit_price = float(record["payload"]["exit_price"])
    assert exit_price < 95 if direction == "long" else exit_price > 105


def test_target_open_precedes_later_stop_range(journal):
    queue(journal)
    progress(journal, [bar()])
    record = progress(journal, [bar(), bar(2, open=105, high=106, low=95, close=100)])
    assert record["payload"]["exit_reason"] == "GAP_TARGET"
    assert not record["payload"]["ambiguous_bar"]
    assert float(record["payload"]["exit_price"]) < 104  # Conservative fill and slippage.


@pytest.mark.parametrize("direction", ["long", "short"])
def test_target_costs_net_arithmetic(journal, direction):
    queue(journal, direction=direction)
    hit = (
        bar(high=105, low=99, close=104) if direction == "long" else bar(high=101, low=95, close=96)
    )
    record = progress(journal, [hit])
    p = record["payload"]
    assert p["exit_reason"] == "TARGET"
    assert Decimal(p["net_pnl"]) == Decimal(p["gross_pnl"]) - Decimal(p["fees"]) - Decimal(
        p["funding_allowance"]
    )
    assert Decimal(p["fees"]) > 0
    assert Decimal(p["funding_allowance"]) > 0


def test_time_exit(journal):
    queue(journal, hold=2)
    assert progress(journal, [bar()])["state"] == "OPEN"
    record = progress(journal, [bar(), bar(2)])
    assert record["payload"]["exit_reason"] == "TIME_EXIT"
    assert record["payload"]["exit_ms"] == NOW + 3 * 60000


@pytest.mark.parametrize("open", [97, 105])
def test_invalid_entry_cancels_not_retroactively_profitable(journal, open):
    queue(journal)
    record = progress(journal, [bar(open=open, high=open + 1, low=open - 1, close=open)])
    assert record["state"] == "CANCELLED"
    assert record["payload"]["exit_reason"] == "ENTRY_OUTSIDE_ORIGINAL_LEVELS"
    assert "net_pnl" not in record["payload"]


def test_missing_entry_is_cancelled(journal):
    queue(journal)
    record = progress(journal, [bar(2)])
    assert record["state"] == "CANCELLED"
    assert record["payload"]["exit_reason"] == "MISSING_ENTRY_BAR"


def test_missing_exit_path_unresolved_not_counted_as_win(journal):
    queue(journal)
    progress(journal, [bar()])
    record = progress(journal, [bar(3, high=105, low=99, close=104)])
    assert record["state"] == "UNRESOLVED"
    row = scoreboard(journal)["rows"][0]
    assert row["unresolved"] == 1 and row["closed"] == 0
    assert row["net_pnl"] == 0 and row["win_rate_percent"] is None


def test_all_observed_intermediate_bars_are_replayed(journal):
    queue(journal)
    progress(journal, [bar()])
    record = progress(
        journal, [bar(1), bar(2, high=101, low=97, close=99), bar(3, high=106, low=99, close=105)]
    )
    assert record["payload"]["exit_reason"] == "STOP"


def test_repeated_candles_do_not_duplicate_costs(journal):
    queue(journal)
    first = progress(journal, [bar(high=105, low=99, close=104)])
    second = progress(journal, [bar(high=105, low=99, close=104)])
    assert first == second
    assert scoreboard(journal)["rows"][0]["closed"] == 1


def test_open_mark_is_separate_from_closed_net(journal):
    queue(journal)
    progress(journal, [bar()])
    row = scoreboard(journal, now=(NOW + 120000) / 1000)["rows"][0]
    assert row["open"] == 1
    assert row["net_pnl"] == 0
    assert row["open_exit_estimate"] < 0
    assert row["stale_open_marks"] == 0
    assert scoreboard(journal, now=(NOW + 900000) / 1000)["rows"][0]["stale_open_marks"] == 1


def test_active_slot_spans_experiment_changes(journal):
    assert queue(journal) == "QUEUED"
    assert queue(journal, experiment="experiment2", signal_ms=NOW) == "SLOT_OCCUPIED"
    assert journal.db.execute("SELECT count(*) FROM shadow_trades").fetchone()[0] == 1


def test_new_experiment_and_source_are_not_pooled(journal):
    queue(journal)
    queue(journal, source="synthetic:demo_rule_stub", experiment="experiment2")
    assert len(scoreboard(journal)["rows"]) == 1
    assert len(scoreboard(journal, source="all")["rows"]) == 2


def test_instruments_are_not_pooled(journal):
    queue(journal)
    other = INSTRUMENT.model_copy(update={"id": "perp:other:TEST2", "symbol": "TEST2"})
    queue(journal, instrument=other)
    assert len(scoreboard(journal)["rows"]) == 2


def test_disabled_shadow_creates_no_tickets(journal):
    settings = Settings(shadow=ShadowSettings(enabled=False))
    assert queue(journal, settings=settings) == "DISABLED"
    assert not scoreboard(journal)["rows"]


def test_readme_assumptions_are_in_output(journal):
    queue(journal)
    text = markdown_scoreboard(scoreboard(journal))
    assert "Simulated Only" in text
    assert "not actual exchange funding" in text
    assert "not a pure AI-only comparison" in text


def test_watchlist_preserves_pending_market(journal):
    queue(journal)
    another = INSTRUMENT.model_copy(update={"id": "new", "symbol": "NEW"})
    assert preserve_shadow_watchlist([another], journal)[0].symbol == "TEST"


def test_restart_persists_open_trade(tmp_path):
    path = str(tmp_path / "journal.db")
    journal = Journal(path)
    queue(journal)
    progress(journal, [bar()])
    journal.close()
    journal = Journal(path)
    record = progress(journal, [bar(), bar(2, high=105, low=99, close=104)])
    assert record["state"] == "CLOSED"
    journal.close()


def test_v1_database_migration_preserves_decisions(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE decisions(
        id INTEGER PRIMARY KEY,created_at REAL NOT NULL,source TEXT NOT NULL,
        instrument TEXT NOT NULL,bar_ms INTEGER NOT NULL,status TEXT NOT NULL,
        payload TEXT NOT NULL,UNIQUE(source,instrument,bar_ms))""")
    db.execute("INSERT INTO decisions VALUES(1,1,'hyperliquid:jev','TEST',1,'JEV_WAIT','{}')")
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()
    journal = Journal(path)
    assert len(journal.recent()) == 1
    assert journal.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert not scoreboard(journal)["rows"]
    journal.close()


def test_transaction_rollback_removes_both_intent_and_decision(journal):
    with pytest.raises(RuntimeError):
        with journal.db:
            enqueue(
                journal,
                Settings(),
                "exp",
                SOURCE,
                INSTRUMENT,
                "rules:test",
                candidate(),
                "expansion",
                NOW,
                NOW,
            )
            journal.record(SOURCE, INSTRUMENT.id, NOW, "PROPOSAL_ONLY", {}, commit=False)
            raise RuntimeError("simulated interruption")
    assert journal.db.execute("SELECT count(*) FROM shadow_trades").fetchone()[0] == 0
    assert journal.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_excessive_cost_buffer_is_rejected():
    with pytest.raises(ValidationError):
        ShadowSettings(fee_bps_per_side=100, slippage_bps_per_side=100)
