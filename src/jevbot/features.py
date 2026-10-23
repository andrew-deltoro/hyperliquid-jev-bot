from __future__ import annotations

import math
import statistics

from jevbot.models import Candle, Features


class DataQualityError(ValueError):
    pass


def closed_candles(bars: list[Candle], now_ms: int, max_age_seconds: int) -> list[Candle]:
    starts = [bar.start_ms for bar in bars]
    if starts != sorted(set(starts)):
        raise DataQualityError("DUPLICATE_OR_UNORDERED_BARS")
    closed = [bar for bar in bars if bar.end_ms < now_ms]
    if len(closed) < 80:
        raise DataQualityError("INSUFFICIENT_WARMUP")
    if any(b.start_ms % 60000 != 0 or b.end_ms - b.start_ms not in (59999, 60000) for b in closed):
        raise DataQualityError("INVALID_BAR_INTERVAL")
    if any(b.start_ms - a.start_ms != 60000 for a, b in zip(closed, closed[1:], strict=False)):
        raise DataQualityError("GAPPED_BARS")
    if now_ms - closed[-1].end_ms > max_age_seconds * 1000:
        raise DataQualityError("STALE_DATA")
    return closed


def ema(values: list[float], span: int) -> list[float]:
    result = [values[0]]
    alpha = 2 / (span + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def calculate(bars: list[Candle]) -> Features:
    if len(bars) < 80:
        raise DataQualityError("INSUFFICIENT_WARMUP")
    closes = [bar.close for bar in bars]
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False)]
    ranges = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in zip(bars, bars[1:], strict=False)
    ]
    atr = statistics.mean(ranges[-14:])
    if atr <= 0:
        raise DataQualityError("ZERO_VOLATILITY")
    fast = ema(closes, 12)
    slow = ema(closes, 36)
    trend = (fast[-1] - slow[-1]) / atr
    distance = sum(abs(b - a) for a, b in zip(closes[-21:], closes[-20:], strict=False))
    efficiency = abs(closes[-1] - closes[-21]) / distance if distance else 0
    reference_vol = statistics.pstdev(returns[-70:-10])
    recent_vol = statistics.pstdev(returns[-10:])
    vol_ratio = recent_vol / max(reference_vol, 1e-8)
    baseline_volumes = [bar.volume for bar in bars[-21:-1]]
    volume_ratio = bars[-1].volume / max(statistics.mean(baseline_volumes), 1e-8)
    prior = bars[-21:-1]
    baseline = closes[-21:-1]
    mean, std = statistics.mean(baseline), statistics.pstdev(baseline)
    zscore = (closes[-1] - mean) / max(std, atr * 0.1)
    prior_baseline = closes[-22:-2]
    prior_z = (closes[-2] - statistics.mean(prior_baseline)) / max(
        statistics.pstdev(prior_baseline), atr * 0.1
    )
    atr_fraction = atr / closes[-1]
    if atr_fraction > 0.04 or abs(returns[-1]) > 0.04:
        regime = "shock"
    elif vol_ratio > 1.8 and volume_ratio > 1.2:
        regime = "expansion"
    elif trend > 0.6 and efficiency > 0.25:
        regime = "trend_up"
    elif trend < -0.6 and efficiency > 0.25:
        regime = "trend_down"
    elif abs(trend) < 0.6 and efficiency < 0.3:
        regime = "range"
    else:
        regime = "mixed"
    return Features(
        close=closes[-1],
        atr=atr,
        atr_fraction=atr_fraction,
        ema_fast=fast[-1],
        ema_slow=slow[-1],
        previous_ema_fast=fast[-2],
        trend_strength=trend,
        efficiency=efficiency,
        volatility_ratio=vol_ratio,
        volume_ratio=volume_ratio,
        prior_high=max(b.high for b in prior),
        prior_low=min(b.low for b in prior),
        zscore=zscore,
        prior_zscore=prior_z,
        regime=regime,
    )


def routing_options(features: Features) -> dict[str, str]:
    """Joint strategy/profile choices avoid independent-question contradictions."""
    families = {
        "trend_up": ["trend_pullback", "volatility_breakout"],
        "trend_down": ["trend_pullback", "volatility_breakout"],
        "expansion": ["volatility_breakout", "trend_pullback"],
        "range": ["mean_reversion", "liquidity_sweep"],
        "mixed": ["liquidity_sweep", "volatility_breakout"],
        "shock": [],
    }[features.regime]
    profiles = ["standard", "wide"] if features.volatility_ratio > 1.5 else ["tight", "standard"]
    return {
        **{
            f"{strategy}:{profile}": (
                f"Use {strategy} with the bounded {profile} profile only if suitable "
                f"for this observed {features.regime} context."
            )
            for strategy in families
            for profile in profiles
        },
        "WAIT": "No supplied strategy/profile is appropriate now.",
    }
