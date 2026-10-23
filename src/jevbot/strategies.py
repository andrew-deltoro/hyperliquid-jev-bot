from __future__ import annotations

import hashlib
from decimal import ROUND_DOWN, Decimal

from jevbot.models import PROFILES, Candidate, Candle, Features, Instrument, PreviewSettings


def generate(
    instrument: Instrument, bars: list[Candle], features: Features, route: str
) -> list[Candidate]:
    if route == "WAIT" or features.regime == "shock":
        return []
    strategy, profile = route.split(":")
    policy = PROFILES[profile]
    bar, previous = bars[-1], bars[-2]
    direction = None
    reasons = []
    # All reference highs/lows exclude the current completed candle.
    if strategy == "volatility_breakout":
        buffer = features.atr * 0.1
        if features.volume_ratio >= 1.2:
            if bar.close > features.prior_high + buffer:
                direction, reasons = "long", ["PRIOR_RANGE_BREAK_UP", "VOLUME_CONFIRMED"]
            elif bar.close < features.prior_low - buffer:
                direction, reasons = "short", ["PRIOR_RANGE_BREAK_DOWN", "VOLUME_CONFIRMED"]
    elif strategy == "trend_pullback":
        if features.regime == "trend_up" and (
            previous.low <= features.previous_ema_fast + features.atr * 0.25
            and bar.close > features.ema_fast
            and bar.close > previous.high
        ):
            direction, reasons = "long", ["UPTREND", "PULLBACK_RESUMPTION"]
        elif features.regime == "trend_down" and (
            previous.high >= features.previous_ema_fast - features.atr * 0.25
            and bar.close < features.ema_fast
            and bar.close < previous.low
        ):
            direction, reasons = "short", ["DOWNTREND", "PULLBACK_RESUMPTION"]
    elif strategy == "mean_reversion" and features.regime == "range":
        if features.prior_zscore < -1.5 and features.zscore > features.prior_zscore + 0.25:
            direction, reasons = "long", ["LOWER_EXCURSION", "REVERSION_CONFIRMATION"]
        elif features.prior_zscore > 1.5 and features.zscore < features.prior_zscore - 0.25:
            direction, reasons = "short", ["UPPER_EXCURSION", "REVERSION_CONFIRMATION"]
    elif strategy == "liquidity_sweep":
        swept_low = bar.low < features.prior_low and bar.close > features.prior_low
        swept_high = bar.high > features.prior_high and bar.close < features.prior_high
        if swept_low and not swept_high and bar.close > bar.open:
            direction, reasons = "long", ["PRIOR_LOW_SWEEP", "RANGE_RECLAIM"]
        elif swept_high and not swept_low and bar.close < bar.open:
            direction, reasons = "short", ["PRIOR_HIGH_SWEEP", "RANGE_RECLAIM"]
    if direction is None:
        return []
    stop_distance = features.atr * float(policy["stop_atr"])
    if strategy == "liquidity_sweep":
        extreme_distance = bar.close - bar.low if direction == "long" else bar.high - bar.close
        stop_distance = max(stop_distance, extreme_distance + features.atr * 0.1)
    sign = 1 if direction == "long" else -1
    stop = bar.close - sign * stop_distance
    target = bar.close + sign * stop_distance * float(policy["target_r"])
    if min(stop, target) <= 0:
        return []
    digest = hashlib.sha256(
        f"{instrument.id}:{bar.start_ms}:{route}:{direction}".encode()
    ).hexdigest()[:20]
    return [
        Candidate(
            id=f"candidate_{digest}",
            instrument_id=instrument.id,
            strategy=strategy,
            profile=profile,
            direction=direction,
            entry=bar.close,
            stop=stop,
            target=target,
            max_hold_minutes=int(policy["max_hold_minutes"]),
            reasons=reasons,
        )
    ]


def size_preview(candidate: Candidate, instrument: Instrument, settings: PreviewSettings) -> dict:
    """Hypothetical linear-perp arithmetic, never a margin or order approval."""
    if instrument.kind != "perp":
        raise ValueError("Unsupported payoff model")
    d = lambda value: Decimal(str(value))  # noqa: E731
    entry = d(candidate.entry)
    stop_distance = abs(entry - d(candidate.stop))
    cost_per_unit = entry * d(settings.cost_buffer_bps) / Decimal(10000)
    risk_budget = d(settings.reference_equity) * d(settings.risk_fraction)
    leverage = min(settings.max_leverage, instrument.max_leverage)
    notional_cap = d(settings.reference_equity) * min(
        d(settings.max_notional_fraction), d(leverage)
    )
    quantity = min(risk_budget / (stop_distance + cost_per_unit), notional_cap / entry)
    quantity = quantity.quantize(Decimal(1).scaleb(-instrument.size_decimals), rounding=ROUND_DOWN)
    return {
        "hypothetical_only": True,
        "quantity": str(quantity),
        "notional": str(quantity * entry),
        "reference_equity": settings.reference_equity,
        "intended_loss_with_buffer": str(quantity * (stop_distance + cost_per_unit)),
        "leverage_ceiling_preview": leverage,
        "risk_math_valid": quantity > 0,
        "executable": False,
        "unverified": [
            "actual_equity",
            "portfolio_exposure",
            "spread_and_depth",
            "margin_tiers",
            "liquidation_buffer",
            "price_precision",
            "minimum_notional",
            "account_permissions",
        ],
    }
