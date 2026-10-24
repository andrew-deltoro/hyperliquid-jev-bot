from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from jevbot import __version__
from jevbot.adapters import (
    HyperliquidReader,
    UpstreamError,
    local_macro,
    select_active,
    skylark_health,
)
from jevbot.decisions import DecisionError, DemoProvider, JevProvider
from jevbot.demo import fixture
from jevbot.diagnostics import breakdown, markdown_breakdown
from jevbot.engine import Engine
from jevbot.improvement import improvement_report, markdown_improvement, review_if_due
from jevbot.journal import Journal
from jevbot.models import Settings
from jevbot.shadow import markdown_scoreboard, scoreboard, tracked_instruments


def preserve_shadow_watchlist(active, journal: Journal):
    """Do not abandon pending/open shadow tickets when auto-selection rotates."""
    tracked = tracked_instruments(journal)
    tracked_ids = {instrument.id for instrument in tracked}
    count = max(len(active), len(tracked))
    return (tracked + [item for item in active if item.id not in tracked_ids])[:count]


@contextlib.contextmanager
def single_writer(path: str):
    """Prevent duplicate scanners/costs for a journal on one Linux VPS."""
    import fcntl

    lock_path = Path(path + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another process owns this journal") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def emit(data: dict):
    print(json.dumps(data, allow_nan=False), flush=True)


def concise(result: dict) -> dict:
    payload = result.get("payload", {})
    return {
        "instrument": result["instrument"],
        "status": result["status"],
        "regime": payload.get("features", {}).get("regime"),
        "route": payload.get("route"),
        "source": payload.get("data_source"),
        "provider": payload.get("decision_provider"),
        "execution_authorized": False,
        "decision_stage": payload.get("diagnostics", {}).get("stage"),
    }


def markdown_report(journal: Journal) -> str:
    summary = journal.summary()
    lines = [
        "# Hyperliquid JEV Analysis Report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}. "
        "This report contains analysis only; no real orders are generated. "
        "Hypothetical fills are reported separately by the scoreboard.",
        "",
        "## Journal totals",
        "",
        "| Data and decision source | Status | Count |",
        "|---|---|---:|",
    ]
    for row in summary["counts"]:
        lines.append(f"| {row['source']} | {row['status']} | {row['count']} |")
    lines += [
        "",
        "## Latest observations",
        "",
        "| Instrument | Source | Regime | Strategy/profile | Status |",
        "|---|---|---|---|---|",
    ]
    for row in summary["recent"]:
        payload = row["payload"]
        values = [
            row["instrument"],
            row["source"],
            payload.get("features", {}).get("regime", ""),
            payload.get("route", ""),
            row["status"],
        ]
        lines.append(
            "| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "PROPOSAL_ONLY is a hypothesis, not an order approval. "
        "Synthetic scenarios and the demo_rule_stub are labeled separately from live data and JEV.",
        "",
        "There is no backtest-return threshold or edge-approval gate. "
        "The journal records operational skips, model WAIT decisions, and missing credentials explicitly.",
        "",
        "## v0.2 diagnostics and simulated scoreboard",
        "",
        "Run `jevbot why-wait` for decision-path counts and `jevbot scoreboard` for forward-only "
        "simulated tickets. No real fills or account returns are produced.",
        "",
    ]
    return "\n".join(lines)


async def operate(args, settings: Settings, journal: Journal) -> int:
    if args.command == "demo":
        engine = Engine(settings, journal, DemoProvider())
        for kind in ("breakout", "range", "trend", "shock"):
            instrument, bars = fixture(kind)
            result = await engine.analyze(
                instrument,
                bars,
                {"status": "synthetic", "direction": "unknown"},
                data_source="synthetic",
            )
            emit(concise(result))
        return 0
    # No environment secrets are printed, persisted, or embedded into client defaults.
    async with httpx.AsyncClient() as client:
        reader = HyperliquidReader(client)
        provider = JevProvider(client, journal, settings)
        if args.command == "doctor":
            result = {
                "version": __version__,
                "analysis_only": True,
                "trading_adapter": "not_implemented",
                "python": sys.version.split()[0],
                "jev_auth_configured": bool(provider.api_key or provider.proxy_auth),
                "configured_model": settings.jev_model,
                "database": "writable",
                "skylark": await skylark_health(client)
                if settings.skylark_health
                else {"status": "disabled"},
            }
            if args.check_jev:
                try:
                    answer = await provider.choose(
                        {"test": "The bot is analysis-only and cannot place orders."},
                        {
                            "WAIT": "The supplied state does not authorize order placement.",
                            "ORDER_AUTHORIZED": "Only if the supplied state explicitly authorizes an executable order.",
                        },
                        "connectivity_contract_check",
                    )
                    if answer.choice != "WAIT":
                        raise DecisionError("JEV_CONTRACT_CHECK_FAILED")
                    result["jev_check"] = {
                        "status": "passed",
                        "model": answer.model,
                        "choice": answer.choice,
                    }
                except DecisionError as error:
                    result["jev_check"] = {"status": str(error)}
                    emit(result)
                    return 2
            emit(result)
            return 0
        registry, warnings = await reader.discover()
        for warning in warnings:
            journal.event("DISCOVERY_WARNING", warning)
        if args.command == "discover":
            emit(
                {
                    "instruments": [item.model_dump() for item in registry],
                    "warnings": warnings,
                    "execution_supported": False,
                }
            )
            return 0 if registry else 2
        if not registry:
            emit({"error": "NO_MARKETS_DISCOVERED"})
            return 2
        active, missing = select_active(
            registry, settings.symbols, settings.auto_select, settings.active_limit
        )
        active = preserve_shadow_watchlist(active, journal)
        journal.set("registry", [item.model_dump() for item in registry])
        emit(
            {
                "analysis_only": True,
                "discovered": len(registry),
                "active": [item.symbol for item in active],
                "missing_configured_symbols": missing,
                "warnings": warnings,
                "jev_auth_configured": bool(provider.api_key or provider.proxy_auth),
            }
        )
        if not active:
            return 2
        engine = Engine(settings, journal, provider)
        cycles = 1 if args.command == "scan" else args.cycles
        cycle = 0
        refreshed = time.monotonic()
        health_at = 0.0
        while cycles == 0 or cycle < cycles:
            if settings.skylark_health and time.monotonic() - health_at > 3600:
                journal.set("skylark_health", await skylark_health(client))
                health_at = time.monotonic()
            macro = local_macro(settings.skylark_readings_file, settings.skylark_max_age_days)
            for instrument in active:
                try:
                    now_ms = int(time.time() * 1000)
                    bars = await reader.candles(instrument.symbol, settings.lookback_bars, now_ms)
                    # Evaluate freshness at receipt, not at request creation.
                    result = await engine.analyze(instrument, bars, macro)
                    emit(concise(result))
                except UpstreamError as error:
                    journal.event(
                        "MARKET_DATA_ERROR", {"instrument": instrument.id, "code": str(error)}
                    )
                    emit({"instrument": instrument.symbol, "status": str(error)})
            review = await review_if_due(journal, settings, provider)
            if review is not None:
                emit(
                    {
                        "event": "IMPROVEMENT_REVIEW",
                        "groups": len(review["rows"]),
                        "new_evidence": review["new_evidence"],
                        "jev_review": review["jev_review"]["status"],
                        "execution_authorized": False,
                    }
                )
            journal.set("heartbeat", {"at": time.time(), "analysis_only": True})
            cycle += 1
            if cycles and cycle >= cycles:
                break
            await asyncio.sleep(settings.poll_seconds)
            if time.monotonic() - refreshed > 3600:
                updated, refresh_warnings = await reader.discover()
                if updated:
                    registry = updated
                    active, _ = select_active(
                        registry, settings.symbols, settings.auto_select, settings.active_limit
                    )
                    active = preserve_shadow_watchlist(active, journal)
                    journal.set("registry", [item.model_dump() for item in registry])
                for warning in refresh_warnings:
                    journal.event("DISCOVERY_WARNING", warning)
                refreshed = time.monotonic()
        return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Analysis-only Hyperliquid/JEV bot; cannot place orders."
    )
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--config", default="configs/analysis.toml")
    result.add_argument("--database", help="Override journal path")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Offline synthetic scenarios; no JEV or network")
    commands.add_parser("discover", help="Read-only native/HIP-3/spot metadata")
    commands.add_parser("scan", help="One live read-only scan")
    run = commands.add_parser("run", help="Continuous read-only analysis")
    run.add_argument("--cycles", type=int, default=0, help="0 continues until stopped")
    doctor = commands.add_parser("doctor", help="Configuration and source health")
    doctor.add_argument(
        "--check-jev", action="store_true", help="Makes one budgeted JEV API request"
    )
    report = commands.add_parser("report", help="View decision journal")
    report.add_argument("--output", help="Write a Markdown report")
    report.add_argument("--json", action="store_true")
    improvement = commands.add_parser(
        "improvement", help="Research memory and proposed challengers"
    )
    improvement.add_argument("--json", action="store_true")
    improvement.add_argument("--output", help="Write Markdown or JSON to a file")
    for name, help_text, hours in (
        ("scoreboard", "Forward-only simulated strategy comparison", 0),
        ("why-wait", "Observed decision-path diagnostics, not invented AI reasoning", 24),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--hours", type=float, default=hours, help="0 means all history")
        command.add_argument("--source", choices=["live", "synthetic", "all"], default="live")
        command.add_argument("--json", action="store_true")
        command.add_argument("--output", help="Write Markdown or JSON to a file")
        if name == "scoreboard":
            command.add_argument("--by-regime", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        settings = Settings.load(args.config)
        if args.database:
            settings.database = args.database
        if getattr(args, "cycles", 0) < 0:
            raise ValueError("cycles must be nonnegative")
        import math

        if not math.isfinite(getattr(args, "hours", 0)) or getattr(args, "hours", 0) < 0:
            raise ValueError("hours must be finite and nonnegative")
        os.umask(0o077)
        if args.command in ("report", "scoreboard", "why-wait", "improvement"):
            if not Path(settings.database).exists():
                raise ValueError("Journal does not exist; run demo or scan first")
            journal = Journal(settings.database)
            try:
                if args.command in ("scoreboard", "why-wait", "improvement"):
                    if args.command == "improvement":
                        data = improvement_report(journal, settings)
                        text = markdown_improvement(data)
                    elif args.command == "scoreboard":
                        data = scoreboard(
                            journal, hours=args.hours, source=args.source, by_regime=args.by_regime
                        )
                        text = markdown_scoreboard(data)
                    else:
                        data = breakdown(journal, hours=args.hours, source=args.source)
                        text = markdown_breakdown(data)
                    if args.json:
                        text = json.dumps(data, allow_nan=False, indent=2) + "\n"
                    if args.output:
                        Path(args.output).write_text(text)
                    else:
                        print(text)
                elif args.json:
                    emit(journal.summary())
                else:
                    text = markdown_report(journal)
                    if args.output:
                        Path(args.output).write_text(text)
                    else:
                        print(text)
            finally:
                journal.close()
            return 0
        with single_writer(settings.database):
            journal = Journal(settings.database)
            try:
                return asyncio.run(operate(args, settings, journal))
            finally:
                journal.close()
    except KeyboardInterrupt:
        emit({"status": "STOPPED", "orders_submitted": 0})
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        # Config paths are nonsecret; do not serialize arbitrary validation input.
        emit(
            {
                "error": type(error).__name__,
                "message": "Operation failed. Check configuration, permissions, and process ownership.",
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
