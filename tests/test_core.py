import math
import random
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jevbot.demo import fixture
from jevbot.features import DataQualityError, calculate, closed_candles, routing_options
from jevbot.journal import Journal
from jevbot.models import Candidate, Candle, Decision, Instrument, PreviewSettings, Settings
from jevbot.strategies import generate, size_preview

NOW = 1800000000000


def test_live_mode_cannot_be_enabled():
    with pytest.raises(ValidationError):
        Settings(mode="live")


@pytest.mark.parametrize(
    "field,value",
    [
        ("poll_seconds", 1),
        ("active_limit", 100),
        ("lookback_bars", 20),
        ("max_jev_calls_per_hour", -1),
        ("decision_ttl_seconds", 0),
    ],
)
def test_config_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_bad_config_key_is_not_ignored():
    with pytest.raises(ValidationError):
        Settings(live_trading=True)


def test_duplicate_symbols_rejected():
    with pytest.raises(ValidationError):
        Settings(symbols=["BTC", "BTC"])


@pytest.mark.parametrize("field,value", [("close", math.nan), ("volume", -1), ("high", 1)])
def test_invalid_candle(field, value):
    _, bars = fixture("range", NOW)
    data = bars[-1].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        Candle.model_validate(data)


def test_current_candle_excluded():
    _, bars = fixture("range", NOW)
    future = bars[-1].model_copy(update={"start_ms": NOW, "end_ms": NOW + 59999})
    assert len(closed_candles([*bars, future], NOW, 150)) == 180


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("duplicate", "DUPLICATE_OR_UNORDERED_BARS"),
        ("unordered", "DUPLICATE_OR_UNORDERED_BARS"),
        ("gap", "GAPPED_BARS"),
        ("short", "INSUFFICIENT_WARMUP"),
    ],
)
def test_bad_bar_sequences(mutation, code):
    _, bars = fixture("range", NOW)
    if mutation == "duplicate":
        bars.append(bars[-1])
    elif mutation == "unordered":
        bars.reverse()
    elif mutation == "gap":
        bars.pop(70)
    else:
        bars = bars[-30:]
    with pytest.raises(DataQualityError, match=code):
        closed_candles(bars, NOW, 150)


def test_stale_bars():
    _, bars = fixture("range", NOW)
    with pytest.raises(DataQualityError, match="STALE_DATA"):
        closed_candles(bars, NOW + 300000, 150)


def test_zero_volatility():
    _, bars = fixture("range", NOW)
    flat = [
        bar.model_copy(update={"open": 100, "high": 100, "low": 100, "close": 100}) for bar in bars
    ]
    with pytest.raises(DataQualityError, match="ZERO_VOLATILITY"):
        calculate(flat)


def test_shock_routes_to_wait_only():
    _, bars = fixture("shock", NOW)
    f = calculate(bars)
    assert f.regime == "shock"
    assert set(routing_options(f)) == {"WAIT"}


def test_breakout_fixture_generates_candidate():
    instrument, bars = fixture("breakout", NOW)
    f = calculate(bars)
    assert f.regime == "expansion"
    candidate = generate(instrument, bars, f, "volatility_breakout:standard")[0]
    assert candidate.direction == "long"
    assert candidate.stop < candidate.entry < candidate.target
    assert f.prior_high < bars[-1].high  # Current bar not in prior range.


def test_wider_profile_changes_levels():
    instrument, bars = fixture("breakout", NOW)
    f = calculate(bars)
    tight = generate(instrument, bars, f, "volatility_breakout:tight")[0]
    wide = generate(instrument, bars, f, "volatility_breakout:wide")[0]
    assert wide.stop < tight.stop < tight.entry
    assert wide.max_hold_minutes > tight.max_hold_minutes


def test_wait_has_no_candidates():
    instrument, bars = fixture("breakout", NOW)
    assert generate(instrument, bars, calculate(bars), "WAIT") == []


@pytest.mark.parametrize(
    "strategy,direction",
    [
        ("trend_pullback", "long"),
        ("trend_pullback", "short"),
        ("mean_reversion", "long"),
        ("mean_reversion", "short"),
        ("liquidity_sweep", "long"),
        ("liquidity_sweep", "short"),
    ],
)
def test_each_strategy_direction(strategy, direction):
    instrument, bars = fixture("range", NOW)
    f = calculate(bars)
    if strategy == "trend_pullback":
        f = f.model_copy(
            update={
                "regime": "trend_up" if direction == "long" else "trend_down",
                "ema_fast": 100,
                "previous_ema_fast": 100,
            }
        )
        bars[-2] = bars[-2].model_copy(update={"low": 99.9, "high": 100.1})
        bars[-1] = bars[-1].model_copy(update={"close": 101 if direction == "long" else 99})
    elif strategy == "mean_reversion":
        f = f.model_copy(
            update={
                "regime": "range",
                "prior_zscore": -2 if direction == "long" else 2,
                "zscore": -1 if direction == "long" else 1,
            }
        )
    else:
        f = f.model_copy(update={"prior_low": 99, "prior_high": 101})
        bars[-1] = bars[-1].model_copy(
            update={
                "open": 100,
                "close": 100.5 if direction == "long" else 99.5,
                "low": 98 if direction == "long" else 99.2,
                "high": 100.8 if direction == "long" else 102,
            }
        )
    found = generate(instrument, bars, f, f"{strategy}:standard")
    assert len(found) == 1
    assert found[0].direction == direction


def test_preview_rounds_down_and_obeys_limits():
    rng = random.Random(1)
    for _ in range(100):
        price = rng.uniform(10, 100000)
        stop = price * rng.uniform(0.9, 0.999)
        c = Candidate(
            id="test",
            instrument_id="test",
            strategy="trend_pullback",
            profile="standard",
            direction="long",
            entry=price,
            stop=stop,
            target=price * 1.1,
            max_hold_minutes=30,
            reasons=[],
        )
        instrument = Instrument(
            id="test", symbol="test", kind="perp", size_decimals=3, max_leverage=2
        )
        result = size_preview(c, instrument, PreviewSettings())
        assert Decimal(result["intended_loss_with_buffer"]) <= Decimal("2.5")
        assert Decimal(result["notional"]) <= Decimal("1000")
        assert result["leverage_ceiling_preview"] == 2
        assert result["executable"] is False
        assert Decimal(result["quantity"]).as_tuple().exponent == -3


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": float("nan")},
        {"probabilities": {"WAIT": 0.2}},
        {"choice": "invented"},
        {"probabilities": {"WAIT": -1}},
    ],
)
def test_decision_schema_rejects_bad_values(change):
    values = {
        "choice": "WAIT",
        "confidence": 1,
        "probabilities": {"WAIT": 1},
        "model": "test",
        "provider": "jev",
        "elapsed_ms": 0,
    }
    with pytest.raises(ValidationError):
        Decision.model_validate({**values, **change})


def test_journal_deduplicates_and_persists(tmp_path):
    path = str(tmp_path / "journal.db")
    journal = Journal(path)
    journal.record("synthetic:demo", "BTC", NOW, "NO_SETUP", {})
    journal.record("synthetic:demo", "BTC", NOW, "PROPOSAL_ONLY", {})
    journal.set("route", {"route": "WAIT"})
    journal.close()
    journal = Journal(path)
    assert len(journal.recent()) == 1
    assert journal.seen("synthetic:demo", "BTC", NOW)
    assert not journal.seen("hyperliquid:jev", "BTC", NOW)
    assert journal.get("route") == {"route": "WAIT"}
    journal.close()


def test_rolling_call_cap_survives_restart(tmp_path):
    path = str(tmp_path / "journal.db")
    journal = Journal(path)
    assert journal.reserve_call(1, now=10000)
    journal.close()
    journal = Journal(path)
    assert not journal.reserve_call(1, now=10001)
    assert journal.reserve_call(1, now=13601)
    assert not journal.reserve_call(0, now=20000)
    journal.close()
