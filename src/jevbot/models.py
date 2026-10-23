from __future__ import annotations

import math
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Strategy = Literal[
    "trend_pullback", "volatility_breakout", "mean_reversion", "liquidity_sweep", "WAIT"
]
STRATEGIES = (
    "trend_pullback",
    "volatility_breakout",
    "mean_reversion",
    "liquidity_sweep",
)
PROFILES = {
    "tight": {"stop_atr": 1.2, "target_r": 1.5, "max_hold_minutes": 30},
    "standard": {"stop_atr": 1.8, "target_r": 2.0, "max_hold_minutes": 90},
    "wide": {"stop_atr": 2.5, "target_r": 2.5, "max_hold_minutes": 180},
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PreviewSettings(StrictModel):
    reference_equity: float = Field(default=1000, gt=0)
    risk_fraction: float = Field(default=0.0025, gt=0, le=0.02)
    max_leverage: int = Field(default=3, ge=1, le=10)
    max_notional_fraction: float = Field(default=1, gt=0, le=3)
    cost_buffer_bps: float = Field(default=12, ge=0, le=200)


class Settings(StrictModel):
    mode: Literal["analysis"] = "analysis"
    database: str = "data/journal.sqlite3"
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    auto_select: bool = False
    active_limit: int = Field(default=8, ge=1, le=12)
    poll_seconds: int = Field(default=60, ge=30)
    lookback_bars: int = Field(default=180, ge=80, le=500)
    data_max_age_seconds: int = Field(default=150, ge=60, le=300)
    decision_ttl_seconds: int = Field(default=20, ge=1, le=60)
    route_dwell_bars: int = Field(default=3, ge=1, le=30)
    route_refresh_bars: int = Field(default=5, ge=1, le=60)
    jev_model: str = "jev-1.13.0"
    jev_timeout_seconds: float = Field(default=8, gt=0, le=30)
    max_jev_calls_per_hour: int = Field(default=30, ge=0, le=1000)
    skylark_health: bool = True
    skylark_readings_file: str = ""
    skylark_max_age_days: int = Field(default=10, ge=1, le=30)
    preview: PreviewSettings = Field(default_factory=PreviewSettings)

    @model_validator(mode="after")
    def symbol_budget(self):
        if len(self.symbols) > self.active_limit:
            raise ValueError("Configured symbols exceed active_limit")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Duplicate configured symbols")
        return self

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
        if os.getenv("JEV_MODEL"):
            data["jev_model"] = os.environ["JEV_MODEL"]
        return cls.model_validate(data)


class Instrument(StrictModel):
    id: str
    symbol: str
    dex: str = ""
    kind: Literal["perp", "spot"]
    size_decimals: int = Field(ge=0, le=18)
    max_leverage: int = Field(default=1, ge=1)
    delisted: bool = False
    margin_mode: str = "unknown"
    collateral_token: int | None = None
    volume_usd: float = Field(default=0, ge=0)
    analysis_supported: bool = True


class Candle(StrictModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def valid_bar(self):
        if self.end_ms < self.start_ms:
            raise ValueError("Reversed candle timestamps")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Invalid OHLC envelope")
        if self.high < self.low:
            raise ValueError("High is below low")
        return self


class Features(StrictModel):
    close: float
    atr: float
    atr_fraction: float
    ema_fast: float
    ema_slow: float
    previous_ema_fast: float
    trend_strength: float
    efficiency: float
    volatility_ratio: float
    volume_ratio: float
    prior_high: float
    prior_low: float
    zscore: float
    prior_zscore: float
    regime: Literal["trend_up", "trend_down", "range", "expansion", "shock", "mixed"]
    liquidity: Literal["unmeasured"] = "unmeasured"


class Candidate(StrictModel):
    id: str
    instrument_id: str
    strategy: Strategy
    profile: Literal["tight", "standard", "wide"]
    direction: Literal["long", "short"]
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    max_hold_minutes: int = Field(gt=0)
    reasons: list[str]

    @model_validator(mode="after")
    def validate_levels(self):
        if self.strategy == "WAIT":
            raise ValueError("WAIT is not a trade candidate")
        if self.direction == "long" and not self.stop < self.entry < self.target:
            raise ValueError("Invalid long price levels")
        if self.direction == "short" and not self.target < self.entry < self.stop:
            raise ValueError("Invalid short price levels")
        return self


class Decision(StrictModel):
    choice: str
    confidence: float = Field(ge=0, le=1)
    probabilities: dict[str, float]
    model: str
    provider: Literal["jev", "demo_rule_stub"]
    elapsed_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_distribution(self):
        if self.choice not in self.probabilities:
            raise ValueError("Choice missing from distribution")
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in self.probabilities.values()):
            raise ValueError("Invalid choice probabilities")
        if abs(sum(self.probabilities.values()) - 1) > 0.03:
            raise ValueError("Probabilities do not sum to one")
        return self
