"""Offline controlled paths that exercise the scoreboard, not performance evidence.

Run from the repository after installation:
  python scripts/shadow_demo.py --database data/shadow-demo.sqlite3
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from jevbot.decisions import DemoProvider
from jevbot.demo import fixture
from jevbot.diagnostics import breakdown, markdown_breakdown
from jevbot.engine import Engine
from jevbot.journal import Journal
from jevbot.models import Candle, Settings
from jevbot.shadow import advance, markdown_scoreboard, scoreboard


async def run(path: str):
    if Path(path).exists():
        raise SystemExit("Choose a new demo database path; existing data will not be overwritten.")
    settings = Settings()
    journal = Journal(path)
    now = int(time.time() * 1000)
    try:
        engine = Engine(settings, journal, DemoProvider())
        for kind in ("breakout", "range", "trend", "shock"):
            instrument, bars = fixture(kind, now)
            result = await engine.analyze(
                instrument,
                bars,
                {"status": "synthetic", "direction": "unknown"},
                data_source="synthetic",
                now_ms=now,
            )
            print(
                json.dumps(
                    {
                        "scenario": kind,
                        "status": result["status"],
                        "synthetic": True,
                        "model": "demo_rule_stub",
                    }
                )
            )
            pending = journal.db.execute(
                "SELECT payload FROM shadow_trades WHERE instrument=? AND state='PENDING'",
                (instrument.id,),
            ).fetchall()
            if not pending:
                continue
            payloads = [json.loads(row["payload"]) for row in pending]
            start = payloads[0]["entry_bar_ms"]
            entry = bars[-1].close
            upper = max(p["candidate"]["target"] for p in payloads)
            lower = min(p["candidate"]["stop"] for p in payloads)
            # Controlled fixture: breakout reaches targets; trend touches both
            # stop and target to test conservative collision handling.
            next_bar = Candle(
                start_ms=start,
                end_ms=start + 59999,
                open=entry,
                close=entry,
                high=max(entry, upper) + 0.01,
                low=entry - 0.01 if kind == "breakout" else min(entry, lower) - 0.01,
                volume=100,
            )
            advance(journal, "synthetic:demo_rule_stub", instrument.id, [next_bar])
        print(markdown_scoreboard(scoreboard(journal, source="synthetic")))
        print(markdown_breakdown(breakdown(journal, source="synthetic", hours=0)))
    finally:
        journal.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/shadow-demo.sqlite3")
    args = parser.parse_args()
    asyncio.run(run(args.database))
