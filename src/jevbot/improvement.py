"""Recurring evidence reviews, bounded research memory, and proposal-only challengers.

No code generation, strategy activation, capital allocation, or order authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from statistics import mean

from jevbot.decisions import DecisionError
from jevbot.models import PROFILES, Settings

EVALUATOR_VERSION = "1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def evaluate(journal, settings: Settings, now: float | None = None) -> dict:
    """All-history live-only cohorts; future outcomes never enter an as-of review."""
    now = time.time() if now is None else now
    groups = defaultdict(list)
    evidence = []
    resolved_evidence = []
    malformed = 0
    rows = journal.db.execute(
        "SELECT * FROM shadow_trades WHERE source='hyperliquid:jev' AND created_at<=?",
        (now,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            candidate = payload["candidate"]
            key = (
                row["experiment"],
                row["source"],
                row["instrument"],
                row["lane"],
                candidate["strategy"],
                candidate["profile"],
                candidate["direction"],
                row["regime"],
                payload.get("weather_state", "unmeasured"),
                payload.get("reference_session", "unmeasured"),
            )
            # Include only final outcome fields in the evidence hash. Re-marking an
            # open ticket does not constitute a new resolved observation.
            outcome = None
            if row["state"] == "CLOSED":
                net = float(payload["net_pnl"])
                risk = float(payload["initial_stop_risk"])
                exit_ms = int(payload["exit_ms"])
                if not math.isfinite(net) or not math.isfinite(risk) or risk <= 0:
                    raise ValueError("Invalid risk or outcome")
                if not math.isfinite(net / risk):
                    raise ValueError("Invalid normalized outcome")
                if exit_ms > now * 1000:
                    continue
                outcome = {
                    "net": net,
                    "r": net / risk,
                    "exit_ms": exit_ms,
                    "day": datetime.fromtimestamp(exit_ms / 1000, UTC).date().isoformat(),
                }
            groups[key].append({"id": row["id"], "state": row["state"], "outcome": outcome})
            evidence.append([row["id"], key, row["state"], outcome])
            if outcome is not None:
                resolved_evidence.append([row["id"], key, outcome])
        except (ValueError, KeyError, TypeError, OverflowError):
            malformed += 1
    result = []
    rules = settings.improvement
    for key, tickets in sorted(groups.items()):
        outcomes = sorted(
            (t["outcome"] for t in tickets if t["outcome"] is not None),
            key=lambda t: t["exit_ms"],
        )
        states = Counter(t["state"] for t in tickets)
        rs = [t["r"] for t in outcomes]
        net = sum(t["net"] for t in outcomes)
        n = len(rs)
        days = len({t["day"] for t in outcomes})
        recent = rs[-rules.recent_closed :]
        total = peak = drawdown = 0.0
        for r in rs:
            total += r
            peak = max(peak, total)
            drawdown = max(drawdown, peak - total)
        average = mean(rs) if rs else None
        recent_mean = mean(recent) if recent else None
        enough = n >= rules.min_closed and days >= rules.min_days
        if states["UNRESOLVED"]:
            status = "DATA_REVIEW"
        elif not n:
            status = "OBSERVE"
        elif not enough:
            status = "EARLY_POSITIVE" if net > 0 and average > 0 else "EARLY_MIXED_OR_NEGATIVE"
        elif drawdown >= rules.drawdown_review_r:
            status = "RISK_REVIEW"
        elif net > 0 and average > 0 and recent_mean <= 0:
            status = "DECAY_REVIEW"
        elif net > 0 and average > 0:
            status = "KEEP_CANDIDATE"
        else:
            status = "REWORK_CANDIDATE"
        result.append(
            {
                "id": digest(key),
                **dict(
                    zip(
                        (
                            "experiment",
                            "source",
                            "instrument",
                            "lane",
                            "strategy",
                            "profile",
                            "direction",
                            "regime",
                            "weather_state",
                            "reference_session",
                        ),
                        key,
                        strict=True,
                    )
                ),
                "status": status,
                "states": dict(states),
                "closed": n,
                "distinct_exit_days_utc": days,
                "wins": sum(r > 0 for r in rs),
                "net_pnl": net,
                "mean_net_r": average,
                "recent_mean_net_r": recent_mean,
                "recent_n": len(recent),
                "closed_sequence_drawdown_r": drawdown,
                "sample_label": "descriptive_only" if enough else "thin_sample",
            }
        )
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "as_of": now,
        "evidence_hash": digest(
            [EVALUATOR_VERSION, sorted(evidence), malformed, rules.model_dump()]
        ),
        "resolved_evidence_hash": digest(sorted(resolved_evidence)),
        "simulation_only": True,
        "execution_authorized": False,
        "rows": result,
        "malformed_tickets_excluded": malformed,
        "limits": [
            "No causal claim, significance test, or profitability approval.",
            "Net R uses initial price-stop risk; net includes modeled fees, slippage and funding allowance.",
            "Drawdown is the closed-ticket R sequence, not account equity or mark-to-market drawdown.",
            "Open/pending/unresolved tickets are not wins; unresolved outcomes can bias closed results.",
            "Experiments, instruments, profiles, directions, regimes and sessions are not pooled.",
            "Recent and all-history results overlap; trades and lanes are not independent samples.",
            "UTC exit days are not exchange sessions. Unmeasured weather remains unknown.",
            "JEV selection probabilities are not calibrated probabilities of future profit.",
        ],
    }


def propose(report: dict, limit: int) -> list[dict]:
    """Enumerated, versioned, one-change challengers, not invented trading code."""
    ranked = sorted(
        (
            r
            for r in report["rows"]
            if r["status"]
            in {
                "KEEP_CANDIDATE",
                "DECAY_REVIEW",
                "REWORK_CANDIDATE",
                "RISK_REVIEW",
                "EARLY_POSITIVE",
                "EARLY_MIXED_OR_NEGATIVE",
            }
        ),
        key=lambda r: (
            r["status"] not in ("KEEP_CANDIDATE", "EARLY_POSITIVE"),
            -(r["mean_net_r"] or 0),
            r["id"],
        ),
    )
    ideas = []
    for parent in ranked:
        # A single profile change is a predefined bundle of stop, target and hold,
        # not a pure single-parameter causal experiment.
        proposed_profile = "tight" if parent["profile"] != "tight" else "standard"
        spec = {
            "template": "profile_challenger_v1",
            "parent_id": parent["id"],
            "proposed_profile": proposed_profile,
        }
        ideas.append(
            {
                "id": digest(spec),
                **spec,
                "parent": parent,
                "status": "PROPOSED",
                "hypothesis": "Compare a different bounded exit-profile bundle in the same context.",
                "profile_parameters": PROFILES[proposed_profile],
                "test_protocol": [
                    "Forward-only sibling experiment with a new identity and explicit enablement.",
                    "Same instrument, direction, entry opportunities, sizing policy and cost assumptions.",
                    "Keep the parent and control results; never overwrite losing evidence.",
                    "Do not change leverage, risk caps, wallet permissions or live behavior.",
                    "Profile alters stop, target and holding time together; results do not isolate one parameter.",
                ],
                "activation": "NOT_IMPLEMENTED_RECOMMENDATION_ONLY",
            }
        )
        if len(ideas) >= limit:
            break
    return ideas


async def review_if_due(journal, settings: Settings, provider=None, *, now=None):
    now = time.time() if now is None else now
    rules = settings.improvement
    if not rules.enabled:
        return None
    previous = journal.db.execute(
        "SELECT * FROM improvement_reviews ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if previous and now - previous["created_at"] < rules.interval_hours * 3600:
        return None
    report = evaluate(journal, settings, now)
    report["new_evidence"] = not previous or previous["evidence_hash"] != report["evidence_hash"]
    old_report = json.loads(previous["payload"]) if previous else {}
    report["new_resolved_evidence"] = (
        old_report.get("resolved_evidence_hash") != report["resolved_evidence_hash"]
    )
    ideas = propose(report, rules.max_ideas_per_review)
    report["proposed_idea_ids"] = [idea["id"] for idea in ideas]
    report["jev_review"] = {"status": "NO_NEW_ELIGIBLE_EVIDENCE"}
    if report["new_resolved_evidence"] and ideas and rules.jev_review and provider is not None:
        if provider.name != "jev":
            raise ValueError("Synthetic providers cannot review live evidence")
        options = {"WAIT": "Do not shortlist a challenger from the supplied evidence."}
        options.update(
            {
                idea[
                    "id"
                ]: f"Recommend researching this challenger, not activating it: {idea['hypothesis']}"
                for idea in ideas
            }
        )
        try:
            answer = await provider.choose(
                {
                    "simulation_only": True,
                    "research_candidates": ideas,
                    "limits": report["limits"],
                    "purpose": "Shortlist research only. Selection cannot alter risk or approve a trade.",
                },
                options,
                "improvement_research_shortlist",
            )
            if answer.choice not in options:
                raise DecisionError("JEV_INVALID_RESEARCH_CHOICE")
            report["jev_review"] = {"status": "REVIEWED", "decision": answer.model_dump()}
            for idea in ideas:
                if idea["id"] == answer.choice:
                    idea["status"] = "JEV_SHORTLISTED"
                    idea["last_shortlisted_at"] = now
        except DecisionError as error:
            report["jev_review"] = {"status": str(error)}
    elif not rules.jev_review:
        report["jev_review"] = {"status": "DISABLED"}
    with journal.db:
        for idea in ideas:
            existing = journal.db.execute(
                "SELECT payload FROM research_ideas WHERE id=?", (idea["id"],)
            ).fetchone()
            if existing:
                old = json.loads(existing["payload"])
                # Keep provenance of a previous shortlist, but refresh parent evidence.
                if old["status"] == "JEV_SHORTLISTED":
                    idea["status"] = old["status"]
                    idea.setdefault("last_shortlisted_at", old.get("last_shortlisted_at"))
            journal.db.execute(
                """INSERT INTO research_ideas VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload""",
                (idea["id"], now, now, json.dumps(idea, allow_nan=False)),
            )
        journal.db.execute(
            "INSERT INTO improvement_reviews(created_at,evidence_hash,payload) VALUES (?,?,?)",
            (now, report["evidence_hash"], json.dumps(report, allow_nan=False)),
        )
    return report


def research_memory(
    journal,
    experiment: str,
    instrument: str,
    regime: str,
    *,
    weather_state="unmeasured",
    reference_session="unmeasured",
) -> dict:
    row = journal.db.execute(
        "SELECT payload FROM improvement_reviews ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {"status": "NO_REVIEW"}
    report = json.loads(row["payload"])
    matched = [
        r
        for r in report["rows"]
        if r["experiment"] == experiment
        and r["instrument"] == instrument
        and r["regime"] == regime
        and r["weather_state"] == weather_state
        and r["reference_session"] == reference_session
    ]
    matched.sort(key=lambda r: (r["status"] != "KEEP_CANDIDATE", -(r["mean_net_r"] or 0), r["id"]))
    return {
        "status": "DESCRIPTIVE_NOT_AUTHORITY",
        "as_of": report["as_of"],
        "evaluator_version": report["evaluator_version"],
        "rows": matched[:5],
        "warning": "Past simulated outcomes are not proof of an edge. Preserve current eligibility and risk caps.",
    }


def improvement_report(journal, settings: Settings) -> dict:
    latest = journal.db.execute(
        "SELECT payload FROM improvement_reviews ORDER BY id DESC LIMIT 1"
    ).fetchone()
    current = evaluate(journal, settings)
    parents = {r["id"]: r for r in current["rows"]}
    ideas = []
    for row in journal.db.execute("SELECT * FROM research_ideas ORDER BY updated_at DESC,id"):
        idea = json.loads(row["payload"])
        # A historical shortlist is never a fresh endorsement. Even ideas no
        # longer in this review's top three get their current parent status.
        parent = parents.get(idea["parent_id"])
        idea.update(
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            current_parent_status=parent["status"] if parent else "NO_CURRENT_EVIDENCE",
            current_parent=parent,
        )
        ideas.append(idea)
    return {
        "current_evidence": current,
        "last_scheduled_review": json.loads(latest["payload"]) if latest else None,
        "ideas": ideas,
    }


def markdown_improvement(data: dict) -> str:
    current = data["current_evidence"]
    lines = [
        "# Recursive Improvement: Simulated Evidence Only",
        "",
        "Recommendations and research memory, not self-modifying code or order permission. "
        "No backtest approval gate is introduced.",
        "",
        "| Experiment | Instrument | Lane/profile/direction | Regime/weather/session | Closed | Days | Mean net R | Recent R | Status |",
        "|---|---|---|---|---:|---:|---:|---:|---|",
    ]

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    for r in current["rows"]:
        values = [
            r["experiment"][:10],
            r["instrument"],
            f"{r['lane']}/{r['profile']}/{r['direction']}",
            f"{r['regime']}/{r['weather_state']}/{r['reference_session']}",
            r["closed"],
            r["distinct_exit_days_utc"],
            f"{r['mean_net_r']:.3f}" if r["mean_net_r"] is not None else "unknown",
            f"{r['recent_mean_net_r']:.3f}" if r["recent_mean_net_r"] is not None else "unknown",
            r["status"],
        ]
        lines.append("| " + " | ".join(map(cell, values)) + " |")
    if not current["rows"]:
        lines += ["", "No eligible live-source shadow tickets yet."]
    last = data["last_scheduled_review"]
    lines += [
        "",
        "## Scheduled review",
        "",
        json.dumps(
            {
                "as_of": last["as_of"],
                "new_evidence": last["new_evidence"],
                "jev_review": last["jev_review"],
            }
        )
        if last
        else "Not run yet.",
        "",
        "## Idea library",
        "",
    ]
    for idea in data["ideas"]:
        lines.append(
            f"- **{idea['id'][:10]} ({idea['status']})**: "
            f"{cell(idea['parent']['instrument'])}, {cell(idea['parent']['strategy'])}, "
            f"{cell(idea['parent']['profile'])} → {cell(idea['proposed_profile'])}. "
            f"Current parent: {idea['current_parent_status']}. "
            "Shortlist labels are historical; recommendation only, not enabled."
        )
    if not data["ideas"]:
        lines.append("No challengers proposed yet. Thin samples do not block existing strategies.")
    lines += [
        "",
        "## Interpretation",
        "",
        f"Malformed tickets excluded: {current['malformed_tickets_excluded']}.",
        "",
    ]
    lines.extend(f"- {limit}" for limit in current["limits"])
    return "\n".join(lines) + "\n"
