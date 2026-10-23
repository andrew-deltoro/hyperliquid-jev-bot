"""Forward-only hypothetical tickets. Not real fills or portfolio accounting."""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from decimal import Decimal

from jevbot.diagnostics import source_matches
from jevbot.journal import Journal
from jevbot.models import STRATEGIES, Candidate, Candle, Instrument, PreviewSettings, Settings
from jevbot.strategies import size_preview

D = lambda value: Decimal(str(value))  # noqa: E731


def enqueue(
    journal: Journal,
    settings: Settings,
    experiment: str,
    source: str,
    instrument: Instrument,
    lane: str,
    candidate: Candidate,
    regime: str,
    signal_ms: int,
    observed_ms: int,
) -> str:
    """Caller owns transaction; no historical fill and no model calls here."""
    if not settings.shadow.enabled:
        return "DISABLED"
    if journal.db.execute(
        "SELECT 1 FROM shadow_trades WHERE source=? AND instrument=? AND lane=? AND state IN ('PENDING','OPEN')",
        (source, instrument.id, lane),
    ).fetchone():
        return "SLOT_OCCUPIED"
    ticket_id = hashlib.sha256(
        f"{experiment}|{source}|{instrument.id}|{lane}|{signal_ms}".encode()
    ).hexdigest()
    config = {"preview": settings.preview.model_dump(), "shadow": settings.shadow.model_dump()}
    journal.db.execute(
        "INSERT OR IGNORE INTO shadow_experiments VALUES (?,?)",
        (experiment, json.dumps(config, allow_nan=False)),
    )
    payload = {
        "candidate": candidate.model_dump(),
        "instrument": instrument.model_dump(),
        "config": config,
        # Strictly after the decision, not an already-open or signal candle.
        "entry_bar_ms": (observed_ms // 60000 + 1) * 60000,
        "last_bar_ms": None,
        "simulation_only": True,
    }
    cursor = journal.db.execute(
        """INSERT OR IGNORE INTO shadow_trades
        (id,experiment,source,instrument,lane,regime,signal_ms,created_at,state,payload)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            ticket_id,
            experiment,
            source,
            instrument.id,
            lane,
            regime,
            signal_ms,
            observed_ms / 1000,
            "PENDING",
            json.dumps(payload, allow_nan=False),
        ),
    )
    return "QUEUED" if cursor.rowcount else "DUPLICATE"


def _exit_values(payload: dict, raw_price: float, exit_ms: int) -> dict:
    candidate = payload["candidate"]
    costs = payload["config"]["shadow"]
    sign = D(1 if candidate["direction"] == "long" else -1)
    quantity, entry = D(payload["quantity"]), D(payload["entry_price"])
    price = D(raw_price) * (1 - sign * D(costs["slippage_bps_per_side"]) / D(10000))
    gross = sign * quantity * (price - entry)
    fees = D(payload["entry_fee"]) + quantity * price * D(costs["fee_bps_per_side"]) / D(10000)
    hours = D(max(0, exit_ms - payload["entry_ms"])) / D(3600000)
    # Flat configurable debit on both directions, NOT observed exchange funding.
    funding = quantity * entry * D(costs["funding_cost_bps_per_hour"]) / D(10000) * hours
    net = gross - fees - funding
    return {
        "exit_price": str(price),
        "gross_pnl": str(gross),
        "fees": str(fees),
        "funding_allowance": str(funding),
        "net_pnl": str(net),
    }


def _advance_ticket(state: str, payload: dict, bars: list[Candle]) -> tuple[str, dict]:
    candidate = payload["candidate"]
    costs = payload["config"]["shadow"]
    expected = payload["entry_bar_ms"] if state == "PENDING" else payload["last_bar_ms"] + 60000
    future = [bar for bar in bars if bar.start_ms >= expected]
    if not future:
        return state, payload
    if future[0].start_ms != expected:
        payload["exit_reason"] = (
            "MISSING_ENTRY_BAR" if state == "PENDING" else "UNOBSERVED_PRICE_PATH"
        )
        return ("CANCELLED" if state == "PENDING" else "UNRESOLVED"), payload
    for bar in future:
        if bar.start_ms != expected:
            payload["exit_reason"] = "UNOBSERVED_PRICE_PATH"
            return "UNRESOLVED", payload
        sign = 1 if candidate["direction"] == "long" else -1
        if state == "PENDING":
            entry = D(bar.open) * (1 + D(sign) * D(costs["slippage_bps_per_side"]) / D(10000))
            if not (
                candidate["stop"] < float(entry) < candidate["target"]
                if sign == 1
                else candidate["target"] < float(entry) < candidate["stop"]
            ):
                payload["exit_reason"] = "ENTRY_OUTSIDE_ORIGINAL_LEVELS"
                return "CANCELLED", payload
            filled_candidate = Candidate.model_validate({**candidate, "entry": float(entry)})
            preview_settings = PreviewSettings.model_validate(
                {
                    **payload["config"]["preview"],
                    "cost_buffer_bps": 2 * costs["fee_bps_per_side"]
                    + 2 * costs["slippage_bps_per_side"],
                }
            )
            instrument = Instrument.model_validate(payload["instrument"])
            preview = size_preview(filled_candidate, instrument, preview_settings)
            if not preview["risk_math_valid"]:
                payload["exit_reason"] = "SIZE_ROUNDS_TO_ZERO"
                return "CANCELLED", payload
            payload.update(
                {
                    "entry_ms": bar.start_ms,
                    "entry_price": str(entry),
                    "quantity": preview["quantity"],
                    "entry_fee": str(
                        D(preview["quantity"]) * entry * D(costs["fee_bps_per_side"]) / D(10000)
                    ),
                    "initial_stop_risk": str(
                        D(preview["quantity"]) * abs(entry - D(candidate["stop"]))
                    ),
                }
            )
            state = "OPEN"
        stop_hit = bar.low <= candidate["stop"] if sign == 1 else bar.high >= candidate["stop"]
        target_hit = (
            bar.high >= candidate["target"] if sign == 1 else bar.low <= candidate["target"]
        )
        gap_stop = bar.open <= candidate["stop"] if sign == 1 else bar.open >= candidate["stop"]
        gap_target = (
            bar.open >= candidate["target"] if sign == 1 else bar.open <= candidate["target"]
        )
        # At the opening timestamp ordering is known. Otherwise both-hit bars
        # are deliberately scored stop-first rather than taking favorable order.
        if gap_stop:
            raw, reason, exit_ms = bar.open, "GAP_STOP", bar.start_ms
        elif gap_target:
            raw, reason, exit_ms = candidate["target"], "GAP_TARGET", bar.start_ms
        elif stop_hit:
            raw = candidate["stop"]
            reason = "BOTH_HIT_STOP_FIRST" if target_hit else "STOP"
            exit_ms = bar.end_ms + 1
        elif target_hit:
            raw, reason, exit_ms = candidate["target"], "TARGET", bar.end_ms + 1
        elif bar.end_ms + 1 >= payload["entry_ms"] + candidate["max_hold_minutes"] * 60000:
            raw, reason, exit_ms = bar.close, "TIME_EXIT", bar.end_ms + 1
        else:
            raw, reason, exit_ms = bar.close, None, bar.end_ms + 1
        values = _exit_values(payload, raw, exit_ms)
        payload["last_bar_ms"] = bar.start_ms
        payload["last_mark_ms"] = bar.end_ms + 1
        if reason:
            payload.update(values)
            payload["exit_ms"] = exit_ms
            payload["exit_reason"] = reason
            payload["ambiguous_bar"] = reason == "BOTH_HIT_STOP_FIRST"
            return "CLOSED", payload
        payload["open_exit_estimate"] = values
        expected = bar.start_ms + 60000
    return state, payload


def advance(journal: Journal, source: str, instrument: str, bars: list[Candle]):
    """Replay only bars after each ticket's persisted progress, transactionally."""
    with journal.db:
        rows = journal.db.execute(
            "SELECT * FROM shadow_trades WHERE source=? AND instrument=? AND state IN ('PENDING','OPEN')",
            (source, instrument),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            state, payload = _advance_ticket(row["state"], payload, bars)
            journal.db.execute(
                "UPDATE shadow_trades SET state=?,payload=? WHERE id=?",
                (state, json.dumps(payload, allow_nan=False), row["id"]),
            )


def tracked_instruments(journal: Journal) -> list[Instrument]:
    result = {}
    for row in journal.db.execute(
        "SELECT payload FROM shadow_trades WHERE source='hyperliquid:jev' AND state IN ('PENDING','OPEN')"
    ):
        instrument = Instrument.model_validate(json.loads(row["payload"])["instrument"])
        result[instrument.id] = instrument
    return list(result.values())


def scoreboard(
    journal: Journal,
    *,
    hours: float = 0,
    source: str = "live",
    now: float | None = None,
    by_regime: bool = False,
) -> dict:
    now = time.time() if now is None else now
    since = now - hours * 3600 if hours else 0
    groups = defaultdict(
        lambda: {
            "pending": 0,
            "open": 0,
            "closed": 0,
            "cancelled": 0,
            "unresolved": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "gross_pnl": 0.0,
            "fees": 0.0,
            "funding_allowance": 0.0,
            "net_pnl": 0.0,
            "open_exit_estimate": 0.0,
            "ambiguous_exits": 0,
            "stale_open_marks": 0,
        }
    )
    # Show inactive lanes too, but only for genuinely observed v0.2 snapshots.
    # Old journal records cannot manufacture historical shadow opportunities.
    for row in journal.db.execute(
        "SELECT source,instrument,payload FROM decisions WHERE created_at>=?", (since,)
    ):
        if not source_matches(row["source"], source):
            continue
        observed = json.loads(row["payload"])
        if not observed.get("shadow_collection_enabled") or "rule_setups" not in observed:
            continue
        for lane in [*(f"rules:{strategy}" for strategy in STRATEGIES), "jev_policy"]:
            key = (
                observed["config_hash"],
                row["source"],
                row["instrument"],
                lane,
                observed["features"]["regime"] if by_regime else "all",
            )
            groups[key]
    for row in journal.db.execute("SELECT * FROM shadow_trades WHERE created_at>=?", (since,)):
        if not source_matches(row["source"], source):
            continue
        key = (
            row["experiment"],
            row["source"],
            row["instrument"],
            row["lane"],
            row["regime"] if by_regime else "all",
        )
        group = groups[key]
        payload = json.loads(row["payload"])
        group[row["state"].lower()] += 1
        if row["state"] == "CLOSED":
            for field in ("gross_pnl", "fees", "funding_allowance", "net_pnl"):
                group[field] += float(payload[field])
            net = D(payload["net_pnl"])
            group["wins" if net > 0 else "losses" if net < 0 else "breakeven"] += 1
            group["ambiguous_exits"] += bool(payload.get("ambiguous_bar"))
        if row["state"] == "OPEN":
            group["open_exit_estimate"] += float(
                payload.get("open_exit_estimate", {}).get("net_pnl", 0)
            )
            group["stale_open_marks"] += now * 1000 - payload.get("last_mark_ms", 0) > 150000
    rows = []
    for key, values in sorted(groups.items()):
        rows.append(
            {
                "experiment": key[0],
                "source": key[1],
                "instrument": key[2],
                "lane": key[3],
                "regime": key[4],
                **values,
                "win_rate_percent": (100 * values["wins"] / values["closed"])
                if values["closed"]
                else None,
            }
        )
    experiment_ids = {row["experiment"] for row in rows}
    configs = {
        row["id"]: json.loads(row["config"])
        for row in journal.db.execute("SELECT * FROM shadow_experiments")
        if row["id"] in experiment_ids
    }
    return {
        "simulation_only": True,
        "hours": hours,
        "source_filter": source,
        "rows": rows,
        "assumptions_by_experiment": configs,
        "notes": [
            "Forward-observed hypothetical tickets, not exchange fills or returns on a funded account.",
            "Each baseline uses a fixed profile; JEV may choose different profiles. This is not a pure AI-only comparison.",
            "Same notional/risk sizing policy, independent per-instrument lane slots; no shared portfolio budget.",
            "Future-bar open entries only, adverse slippage both sides, stop-first ambiguous intrabar exits.",
            "Funding is a configurable hourly cost allowance, not actual exchange funding.",
            "CLOSED net includes fees and funding allowance. Open estimates are separate and can be stale.",
            "Missing paths are UNRESOLVED, excluded from closed P&L, and remain visible.",
            "Hours selects tickets created in that window, not trades closed in that window.",
            "P&L is in modeled instrument price-quote units; instruments and experiments are never pooled.",
            "Win rate only counts resolved closed tickets; unresolved paths can bias comparisons.",
            "No historical tickets are fabricated from old journal proposals.",
        ],
    }


def markdown_scoreboard(data: dict) -> str:
    lines = [
        "# Strategy Scoreboard: Simulated Only",
        "",
        f"Source: {data['source_filter']}. Signal-cohort window: {data['hours']} hours (0 means all). "
        "No real orders, fills, account balance, or funded return is shown.",
        "",
        "| Experiment | Instrument | Lane | Regime | Closed | W/L | Net | Fees | Funding allowance | Open / pending | Unresolved / cancelled |",
        "|---|---|---|---|---:|---|---:|---:|---:|---|---|",
    ]
    for row in data["rows"]:
        lines.append(
            f"| {row['experiment'][:10]} | {row['instrument']} | {row['lane']} | {row['regime']} | {row['closed']} | "
            f"{row['wins']}/{row['losses']} | {row['net_pnl']:.4f} | {row['fees']:.4f} | "
            f"{row['funding_allowance']:.4f} | {row['open']} / {row['pending']} | "
            f"{row['unresolved']} / {row['cancelled']} |"
        )
    if not data["rows"]:
        lines += [
            "",
            "No shadow tickets yet. Collection begins after installing v0.2; old decisions are not backfilled.",
        ]
    lines += ["", "## Open marks and ambiguity", ""]
    visible_marks = False
    for row in data["rows"]:
        if not (row["open"] or row["unresolved"] or row["ambiguous_exits"]):
            continue
        visible_marks = True
        lines.append(
            f"- **{row['experiment'][:10]} / {row['instrument']} / {row['lane']} / {row['regime']}:** "
            f"Open exit estimate {row['open_exit_estimate']:.4f}; stale marks {row['stale_open_marks']}; "
            f"stop-first ambiguous exits {row['ambiguous_exits']}. "
            f"Source: {row['source']}."
        )
    if not visible_marks:
        lines.append("No open marks, unresolved paths, or ambiguous exits in this selection.")
    lines += ["", "## Assumptions", ""]
    for experiment, config in data["assumptions_by_experiment"].items():
        lines.append(
            f"- **{experiment[:10]}:** Reference equity {config['preview']['reference_equity']}; "
            f"risk fraction {config['preview']['risk_fraction']}; "
            f"cost/profile settings `{json.dumps(config['shadow'], sort_keys=True)}`."
        )
    lines += [""] + [f"- {note}" for note in data["notes"]]
    return "\n".join(lines) + "\n"
