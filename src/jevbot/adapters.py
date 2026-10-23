from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from jevbot.models import Candle, Instrument

HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
SKYLARK_HEALTH = "https://api.skylarkdigitalassets.com/source-health"
READ_TYPES = frozenset({"perpDexs", "metaAndAssetCtxs", "spotMeta", "candleSnapshot"})


class UpstreamError(RuntimeError):
    pass


class WeightBudget:
    """Conservative local 600-weight/minute budget, below documented IP ceiling."""

    def __init__(self, ceiling: int = 600):
        self.ceiling = ceiling
        self.calls: deque[tuple[float, int]] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self, weight: int):
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.calls and self.calls[0][0] <= now - 60:
                    self.calls.popleft()
                if sum(w for _, w in self.calls) + weight <= self.ceiling:
                    self.calls.append((now, weight))
                    return
                await asyncio.sleep(max(0.01, self.calls[0][0] + 60 - now))


class HyperliquidReader:
    """Only the public /info endpoint exists here. No account keys or /exchange."""

    def __init__(self, client: httpx.AsyncClient, budget: WeightBudget | None = None):
        self.client = client
        self.budget = budget or WeightBudget()

    async def info(self, payload: dict):
        if payload.get("type") not in READ_TYPES:
            raise ValueError("Non-allowlisted info request")
        weight = 40 if payload["type"] == "candleSnapshot" else 20
        for attempt in range(3):
            await self.budget.acquire(weight)
            try:
                response = await self.client.post(
                    HYPERLIQUID_INFO, json=payload, timeout=15, follow_redirects=False
                )
            except httpx.TransportError:
                if attempt == 2:
                    raise UpstreamError("HYPERLIQUID_TRANSPORT") from None
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise UpstreamError(f"HYPERLIQUID_HTTP_{response.status_code}")
                # Never forward arbitrary response headers or bodies to logs.
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code != 200:
                raise UpstreamError(f"HYPERLIQUID_HTTP_{response.status_code}")
            try:
                return response.json()
            except ValueError:
                raise UpstreamError("HYPERLIQUID_INVALID_JSON") from None
        raise UpstreamError("HYPERLIQUID_RETRY_EXHAUSTED")

    async def discover(self) -> tuple[list[Instrument], list[dict]]:
        instruments: list[Instrument] = []
        warnings: list[dict] = []
        dexs = [""]
        try:
            raw_dexs = await self.info({"type": "perpDexs"})
            if not isinstance(raw_dexs, list):
                raise ValueError("Invalid perpDexs")
            dexs += [
                item["name"]
                for item in raw_dexs
                if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
            ]
        except (UpstreamError, ValueError, KeyError, TypeError):
            warnings.append({"scope": "perpDexs", "reason": "DEX_DISCOVERY_FAILED"})
        for dex in dict.fromkeys(dexs):
            try:
                result = await self.info({"type": "metaAndAssetCtxs", "dex": dex})
                if not isinstance(result, list) or len(result) != 2:
                    raise ValueError("Invalid metadata")
                meta, contexts = result
                universe = meta["universe"]
                if len(universe) != len(contexts):
                    raise ValueError("Metadata/context length mismatch")
                for item, context in zip(universe, contexts, strict=True):
                    try:
                        symbol = item["name"]
                        if dex and not symbol.startswith(f"{dex}:"):
                            symbol = f"{dex}:{symbol}"
                        volume = float(context.get("dayNtlVlm") or 0)
                        if not math.isfinite(volume) or volume < 0:
                            volume = 0
                        instruments.append(
                            Instrument(
                                id=f"perp:{dex or 'native'}:{symbol}",
                                symbol=symbol,
                                dex=dex,
                                kind="perp",
                                size_decimals=item["szDecimals"],
                                max_leverage=item.get("maxLeverage", 1),
                                delisted=item.get("isDelisted", False),
                                margin_mode=item.get(
                                    "marginMode",
                                    "isolated_legacy" if item.get("onlyIsolated") else "unknown",
                                ),
                                collateral_token=meta.get("collateralToken"),
                                volume_usd=volume,
                            )
                        )
                    except (ValueError, TypeError, KeyError):
                        warnings.append({"scope": dex or "native", "reason": "INVALID_INSTRUMENT"})
            except (UpstreamError, ValueError, TypeError, KeyError):
                warnings.append({"scope": dex or "native", "reason": "PERP_METADATA_FAILED"})
        try:
            spot = await self.info({"type": "spotMeta"})
            tokens = {token["index"]: token for token in spot["tokens"]}
            for pair in spot["universe"]:
                try:
                    token = tokens[pair["tokens"][0]]
                    instruments.append(
                        Instrument(
                            id=f"spot:{pair['index']}",
                            symbol=pair["name"],
                            kind="spot",
                            size_decimals=token["szDecimals"],
                            analysis_supported=False,
                        )
                    )
                except (ValueError, TypeError, KeyError, IndexError):
                    warnings.append({"scope": "spot", "reason": "INVALID_INSTRUMENT"})
        except (UpstreamError, ValueError, TypeError, KeyError):
            warnings.append({"scope": "spot", "reason": "SPOT_METADATA_FAILED"})
        unique = {instrument.id: instrument for instrument in instruments}
        return list(unique.values()), warnings

    async def candles(self, symbol: str, lookback: int, now_ms: int) -> list[Candle]:
        raw = await self.info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": "1m",
                    "startTime": now_ms - (lookback + 3) * 60000,
                    "endTime": now_ms,
                },
            }
        )
        if not isinstance(raw, list):
            raise UpstreamError("INVALID_CANDLE_RESPONSE")
        try:
            if any(item.get("s") != symbol or item.get("i") != "1m" for item in raw):
                raise ValueError("Wrong candle identity")
            return [
                Candle(
                    start_ms=item["t"],
                    end_ms=item["T"],
                    open=item["o"],
                    high=item["h"],
                    low=item["l"],
                    close=item["c"],
                    volume=item["v"],
                )
                for item in raw
            ]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UpstreamError("INVALID_CANDLE_SCHEMA") from None


def select_active(
    registry: list[Instrument], symbols: list[str], auto: bool, limit: int
) -> tuple[list[Instrument], list[str]]:
    eligible = [
        item
        for item in registry
        if item.kind == "perp" and item.analysis_supported and not item.delisted
    ]
    if auto:
        return sorted(eligible, key=lambda item: (-item.volume_usd, item.id))[:limit], []
    by_symbol = {item.symbol: item for item in eligible}
    return [by_symbol[s] for s in symbols if s in by_symbol][:limit], [
        s for s in symbols if s not in by_symbol
    ]


async def skylark_health(client: httpx.AsyncClient) -> dict:
    try:
        response = await client.get(SKYLARK_HEALTH, timeout=10, follow_redirects=False)
        if response.status_code != 200:
            return {"status": "unavailable", "reason": f"HTTP_{response.status_code}"}
        data = response.json()
        if data.get("status") != "ready":
            return {"status": "unavailable", "reason": "SOURCE_NOT_READY"}
        observed = date.fromisoformat(data["as_of"])
        if observed > datetime.now(UTC).date():
            return {"status": "unavailable", "reason": "FUTURE_SOURCE_DATE"}
        return {"status": "ready", "as_of": observed.isoformat(), "paid_readings_loaded": False}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return {"status": "unavailable", "reason": "SOURCE_HEALTH_FAILED"}


def local_macro(path: str, max_age_days: int, today: date | None = None) -> dict:
    """Optional user-supplied licensed outputs, no payment or formula inference."""
    today = today or datetime.now(UTC).date()
    if not path:
        return {"status": "unavailable", "reason": "NO_PAID_READINGS", "direction": "unknown"}
    try:
        data = json.loads(Path(path).read_text())
        items = data["readings"]
        expected = {
            "Financial Liquidity Proxy",
            "Financial Liquidity Index",
            "Global Funding Conditions",
        }
        if len(items) != 3 or {item["official_name"] for item in items} != expected:
            raise ValueError("Expected complete unique bundle")
        readings = []
        for item in items:
            observed = date.fromisoformat(item["as_of"])
            value = item["value"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Invalid reading")
            age = (today - observed).days
            if age < 0 or age > max_age_days:
                return {"status": "stale", "reason": "READING_AGE", "direction": "unknown"}
            readings.append(
                {"official_name": item["official_name"], "value": value, "as_of": item["as_of"]}
            )
        return {
            "status": "available",
            "readings": readings,
            "direction": "unknown",
            "interpretation": "Units and directional semantics are not configured. Do not infer them.",
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"status": "unavailable", "reason": "INVALID_LOCAL_BUNDLE", "direction": "unknown"}
