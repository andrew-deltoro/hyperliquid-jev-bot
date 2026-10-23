"""Observed decision-path diagnostics; never manufactured model explanations."""

from __future__ import annotations

import time
from collections import Counter

from jevbot.journal import Journal


def explain(status: str, payload: dict) -> dict:
    stage = payload.get("decision_stage", "legacy_unknown")
    actor = "Demo rule stub" if payload.get("decision_provider") == "demo_rule_stub" else "JEV"
    if status == "PROPOSAL_ONLY":
        category = "proposal"
        reason = f"{actor} selected a candidate; no real order was submitted."
    elif status == "JEV_WAIT":
        if payload.get("candidate_decision", {}).get("choice") == "WAIT":
            stage = "candidate"
            reason = f"{actor} declined the generated candidate."
        elif (
            payload.get("route") == "WAIT"
            or payload.get("router_decision", {}).get("choice") == "WAIT"
        ):
            stage = "router"
            reason = f"{actor} selected no strategy/profile."
        else:
            reason = "Legacy WAIT record without enough information to identify its stage."
        category = "model_wait"
    elif status == "NO_SETUP":
        category, stage = "no_setup", "trigger"
        reason = "The selected strategy's deterministic entry trigger was not satisfied."
    elif status == "JEV_CALL_BUDGET":
        category = "operational_limit"
        reason = "The rolling-hour JEV call cap was exhausted; this is not a market judgment."
    elif status.startswith("JEV_"):
        category = "model_service_error"
        reason = "JEV authentication, transport, availability, or response validation failed."
    elif status == "DECISION_EXPIRED":
        category = "expired"
        reason = "The decision exceeded its time-to-live."
    elif status in {"SHOCK_NO_ENTRY", "SIZE_ROUNDS_TO_ZERO", "UNSUPPORTED_MARKET", "DELISTED"}:
        category = "policy_or_size"
        reason = "A deterministic market, capability, or sizing rule stopped the proposal."
    else:
        category = "data_quality"
        reason = "Market observations failed validation or feature warmup."
    context = []
    if payload.get("features", {}).get("liquidity") == "unmeasured":
        context.append("spread_and_depth_unmeasured")
    if payload.get("macro", {}).get("status") != "available":
        context.append("macro_unavailable_or_stale")
    return {
        "category": category,
        "stage": stage,
        "observed_reason": reason,
        "route_origin": payload.get("route_reason"),
        "context_flags_not_causal_explanations": context,
        "model_reasoning_returned": False,
    }


def source_matches(source: str, selection: str) -> bool:
    return selection == "all" or source.startswith(
        "hyperliquid:" if selection == "live" else "synthetic:"
    )


def breakdown(
    journal: Journal, *, hours: float = 24, source: str = "live", now: float | None = None
) -> dict:
    now = time.time() if now is None else now
    since = now - hours * 3600 if hours else 0
    import json

    counts = Counter()
    examples = []
    total = 0
    setups_seen = 0
    setup_evidence_decisions = 0
    for row in journal.db.execute(
        "SELECT * FROM decisions WHERE created_at>=? ORDER BY id DESC", (since,)
    ):
        if not source_matches(row["source"], source):
            continue
        payload = json.loads(row["payload"])
        diagnostic = payload.get("diagnostics") or explain(row["status"], payload)
        counts[
            (
                row["source"],
                diagnostic["category"],
                diagnostic["stage"],
                row["status"],
                diagnostic.get("route_origin") or "unknown",
            )
        ] += 1
        total += 1
        setup_evidence_decisions += "rule_setups" in payload
        setups_seen += sum(
            bool(item.get("triggered")) for item in payload.get("rule_setups", {}).values()
        )
        if len(examples) < 10:
            examples.append(
                {
                    "source": row["source"],
                    "instrument": row["instrument"],
                    "status": row["status"],
                    "regime": payload.get("features", {}).get("regime"),
                    "diagnostics": diagnostic,
                    "rule_setups": payload.get("rule_setups", {}),
                    "router": payload.get("router_decision"),
                    "candidate": payload.get("candidate_decision"),
                }
            )
    events = Counter()
    # Operational events in this application originate in public-feed collection.
    if source in ("live", "all"):
        for row in journal.db.execute("SELECT kind FROM events WHERE created_at>=?", (since,)):
            events[row["kind"]] += 1
    return {
        "source_filter": source,
        "hours": hours,
        "decisions": total,
        "rule_trigger_occurrences": setups_seen,
        "rule_evidence_decisions": setup_evidence_decisions,
        "rows": [
            {
                "source": key[0],
                "category": key[1],
                "stage": key[2],
                "status": key[3],
                "route_origin": key[4],
                "count": count,
                "percent_of_decisions": round(100 * count / total, 2) if total else 0,
            }
            for key, count in sorted(counts.items())
        ],
        "operational_events_not_in_decision_denominator": dict(events),
        "examples": examples,
        "note": "Counts explain pipeline outcomes, not JEV's private reasoning. Cached WAIT records are not new model calls.",
    }


def markdown_breakdown(data: dict) -> str:
    lines = [
        "# Why WAIT? Decision Breakdown",
        "",
        f"Source filter: {data['source_filter']}. Window: {data['hours']} hours (0 means all history). "
        f"Recorded decisions: {data['decisions']}.",
        "",
        "| Source | Stage | Outcome | Route origin | Count | Share |",
        "|---|---|---|---|---:|---:|",
    ]
    for row in data["rows"]:
        lines.append(
            f"| {row['source']} | {row['stage']} | {row['status']} | {row['route_origin']} | "
            f"{row['count']} | {row['percent_of_decisions']:.1f}% |"
        )
    lines += [
        "",
        f"Rule-trigger occurrences: {data['rule_trigger_occurrences']} across "
        f"{data['rule_evidence_decisions']} decisions with recorded rule evidence. "
        "Missing legacy evidence is unknown, not proof that no setup existed. "
        "Multiple strategies may trigger on one observation; these are not trade counts.",
        "",
        f"Separate operational events: {data['operational_events_not_in_decision_denominator']}.",
        "",
        data["note"],
        "",
        "## Recent evidence",
        "",
    ]
    for item in data["examples"][:5]:
        diagnostic = item["diagnostics"]
        lines.append(
            f"- **{item['instrument']} / {item['status']}:** {diagnostic['observed_reason']} "
            f"Context flags: {', '.join(diagnostic['context_flags_not_causal_explanations']) or 'none'}. "
            "These flags do not establish why the model chose WAIT."
        )
        for strategy, setup in item["rule_setups"].items():
            if setup["triggered"]:
                lines.append(f"  - {strategy}: rule setup present.")
            else:
                lines.append(
                    f"  - {strategy}: unmet long conditions {setup['unmet_long']}; "
                    f"unmet short conditions {setup['unmet_short']}."
                )
    return "\n".join(lines) + "\n"
