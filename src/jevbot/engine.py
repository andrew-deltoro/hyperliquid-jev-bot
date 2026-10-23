from __future__ import annotations

import hashlib
import json
import time

from jevbot.decisions import PROMPT_VERSION, DecisionError, DecisionProvider
from jevbot.features import DataQualityError, calculate, closed_candles, routing_options
from jevbot.journal import Journal
from jevbot.models import Candle, Instrument, Settings
from jevbot.strategies import generate, size_preview


class Engine:
    def __init__(self, settings: Settings, journal: Journal, provider: DecisionProvider):
        self.settings, self.journal, self.provider = settings, journal, provider
        self.config_hash = hashlib.sha256(
            json.dumps(settings.model_dump(), sort_keys=True).encode()
        ).hexdigest()

    async def analyze(
        self,
        instrument: Instrument,
        bars: list[Candle],
        macro: dict,
        *,
        data_source: str = "hyperliquid",
        now_ms: int | None = None,
    ) -> dict:
        if data_source not in ("hyperliquid", "synthetic"):
            raise ValueError("Unknown data source")
        if data_source != "synthetic" and self.provider.name != "jev":
            raise DecisionError("DEMO_PROVIDER_FORBIDDEN_ON_LIVE_DATA")
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        started = time.monotonic()
        source = f"{data_source}:{self.provider.name}"
        payload = {
            "data_source": data_source,
            "decision_provider": self.provider.name,
            "analysis_only": True,
            "execution_authorized": False,
            "prompt_version": PROMPT_VERSION,
            "config_hash": self.config_hash,
            "instrument": instrument.model_dump(),
            "observed_at_ms": now_ms,
            "macro": macro,
        }
        bar_ms = bars[-1].start_ms if bars else now_ms // 60000 * 60000

        def finish(status: str):
            payload["status"] = status
            self.journal.record(source, instrument.id, bar_ms, status, payload)
            return {"instrument": instrument.symbol, "status": status, "payload": payload}

        if instrument.kind != "perp" or not instrument.analysis_supported:
            return finish("UNSUPPORTED_MARKET")
        if instrument.delisted:
            return finish("DELISTED")
        try:
            valid = closed_candles(bars, now_ms, self.settings.data_max_age_seconds)
        except DataQualityError as error:
            return finish(str(error))
        bar_ms = valid[-1].start_ms
        if self.journal.seen(source, instrument.id, bar_ms):
            return {"instrument": instrument.symbol, "status": "DUPLICATE_BAR"}
        try:
            features = calculate(valid)
        except DataQualityError as error:
            return finish(str(error))
        payload["features"] = features.model_dump()
        payload["bar_start_ms"] = bar_ms
        payload["snapshot_id"] = hashlib.sha256(
            json.dumps(
                [instrument.id, bar_ms, features.model_dump(), self.config_hash], sort_keys=True
            ).encode()
        ).hexdigest()[:24]
        if features.regime == "shock":
            return finish("SHOCK_NO_ENTRY")
        options = routing_options(features)
        state_key = f"route:{source}:{instrument.id}"
        previous = self.journal.get(state_key, {})
        # A new configuration or model must not inherit a cached old decision.
        if previous.get("config_hash") != self.config_hash:
            previous = {}
        held_route = previous.get("route", "WAIT")
        elapsed_bars = max(0, (bar_ms - previous.get("selected_bar", 0)) // 60000)
        refresh_due = (
            not previous
            or features.regime != previous.get("regime")
            or bar_ms - previous.get("evaluated_bar", 0) >= self.settings.route_refresh_bars * 60000
            or held_route not in options
        )
        try:
            if refresh_due:
                route_answer = await self.provider.choose(payload, options, "strategy_and_profile")
                payload["router_decision"] = route_answer.model_dump()
                if time.monotonic() - started >= self.settings.decision_ttl_seconds:
                    return finish("DECISION_EXPIRED")
                proposal = route_answer.choice
                if (
                    previous
                    and proposal != held_route
                    and proposal != "WAIT"
                    and held_route != "WAIT"
                    and held_route in options
                    and elapsed_bars < self.settings.route_dwell_bars
                ):
                    route = held_route
                    payload["route_reason"] = "MINIMUM_DWELL"
                else:
                    route = proposal
                    payload["route_reason"] = "JEV_SELECTION"
                applied_decision = (
                    previous.get("applied_decision", previous.get("decision"))
                    if route != proposal
                    else route_answer.model_dump()
                )
                previous = {
                    "route": route,
                    "selected_bar": (
                        previous.get("selected_bar", bar_ms) if route == held_route else bar_ms
                    ),
                    "evaluated_bar": bar_ms,
                    "regime": features.regime,
                    "config_hash": self.config_hash,
                    "decision": route_answer.model_dump(),
                    "applied_decision": applied_decision,
                }
                self.journal.set(state_key, previous)
            else:
                route = held_route
                payload["router_decision"] = previous["decision"]
                payload["route_reason"] = "CACHED_ROUTE"
            payload["route"] = route
            payload["applied_route_decision"] = previous.get(
                "applied_decision", previous["decision"]
            )
            if route == "WAIT":
                return finish("JEV_WAIT")
            candidates = generate(instrument, valid, features, route)
            payload["candidates"] = [candidate.model_dump() for candidate in candidates]
            if not candidates:
                return finish("NO_SETUP")
            if time.monotonic() - started >= self.settings.decision_ttl_seconds:
                return finish("DECISION_EXPIRED")
            choices = {
                candidate.id: (
                    f"Select supplied {candidate.strategy} {candidate.direction} candidate "
                    f"only if its trigger and current context agree. Do not invent entry levels."
                )
                for candidate in candidates
            }
            choices["WAIT"] = "Do not propose any candidate now."
            answer = await self.provider.choose(payload, choices, "candidate_selection")
            payload["candidate_decision"] = answer.model_dump()
            if time.monotonic() - started >= self.settings.decision_ttl_seconds:
                return finish("DECISION_EXPIRED")
            if answer.choice == "WAIT":
                return finish("JEV_WAIT")
            chosen = next(candidate for candidate in candidates if candidate.id == answer.choice)
            payload["selected_candidate"] = chosen.model_dump()
            payload["sizing_preview"] = size_preview(chosen, instrument, self.settings.preview)
            if not payload["sizing_preview"]["risk_math_valid"]:
                return finish("SIZE_ROUNDS_TO_ZERO")
            return finish("PROPOSAL_ONLY")
        except DecisionError as error:
            return finish(str(error))
