from argparse import Namespace

import pytest

from jevbot.cli import markdown_report, operate, single_writer
from jevbot.journal import Journal
from jevbot.models import Settings


def test_single_writer_lock(tmp_path):
    path = str(tmp_path / "test.sqlite3")
    with single_writer(path):
        with pytest.raises(RuntimeError, match="Another process"):
            with single_writer(path):
                pass


async def test_demo_command_runs_without_network(tmp_path, monkeypatch):
    def no_client(*args, **kwargs):
        pytest.fail("Demo must not construct a network client")

    monkeypatch.setattr("jevbot.cli.httpx.AsyncClient", no_client)
    journal = Journal(str(tmp_path / "demo.sqlite3"))
    try:
        code = await operate(Namespace(command="demo"), Settings(), journal)
        assert code == 0
        assert len(journal.recent()) == 4
        assert {row["source"] for row in journal.recent()} == {"synthetic:demo_rule_stub"}
    finally:
        journal.close()


def test_report_labels_simulation_and_nonexecution():
    journal = Journal(":memory:")
    try:
        text = markdown_report(journal)
        assert "analysis only" in text
        assert "not an order approval" in text
        assert "Synthetic" in text
    finally:
        journal.close()
