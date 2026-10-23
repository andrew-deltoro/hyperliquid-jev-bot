from __future__ import annotations

import math
import random
import time

from jevbot.models import Candle, Instrument


def fixture(kind: str, now_ms: int | None = None) -> tuple[Instrument, list[Candle]]:
    """Synthetic deterministic scenarios. Not historical prices or returns."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    boundary = now_ms // 60000 * 60000
    rng = random.Random(42)
    bars = []
    price = 100.0
    for index in range(180):
        previous = price
        if kind == "breakout":
            price = 100 + math.sin(index * 0.4) * 0.15 + rng.uniform(-0.04, 0.04)
        elif kind == "range":
            price = 100 + math.sin(index * 0.45) * 0.4
        elif kind == "trend":
            price = 100 + index * 0.035 + math.sin(index * 0.35) * 0.15
        elif kind == "shock":
            price = 100 + rng.uniform(-0.1, 0.1)
        else:
            raise ValueError("Unknown fixture")
        if index == 179 and kind == "breakout":
            price = 101.5
        if index == 179 and kind == "shock":
            price = 91
        start = boundary - (180 - index) * 60000
        bars.append(
            Candle(
                start_ms=start,
                end_ms=start + 59999,
                open=previous,
                high=max(previous, price) + 0.08,
                low=min(previous, price) - 0.08,
                close=price,
                volume=500 if index == 179 and kind == "breakout" else 100,
            )
        )
    return (
        Instrument(
            id=f"perp:synthetic:{kind}",
            symbol=f"DEMO_{kind.upper()}",
            kind="perp",
            size_decimals=3,
            max_leverage=5,
        ),
        bars,
    )
