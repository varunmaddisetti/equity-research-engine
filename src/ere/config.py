"""Typed loaders for config/*.yaml. Invalid config fails fast with a clear pydantic error."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from ere.paths import CONFIG_DIR

ValuationModel = Literal["dcf", "residual_income", "insurance", "sotp", "ev_sales"]


# ---------------------------------------------------------------- valuation.yaml
class CostOfCapital(BaseModel):
    risk_free_rate: float = Field(gt=0, lt=0.2)
    equity_risk_premium: float = Field(gt=0, lt=0.2)
    size_premium: float = Field(ge=0, lt=0.1)
    beta_lookback_weeks: int = Field(ge=52)
    beta_benchmark: str
    blume_adjust: bool
    beta_floor: float
    beta_cap: float
    cost_of_debt_floor: float
    cost_of_debt_cap: float
    marginal_tax_rate: float = Field(gt=0, lt=0.5)

    def cost_of_equity(self, beta: float) -> float:
        b = min(max(beta, self.beta_floor), self.beta_cap)
        return self.risk_free_rate + b * self.equity_risk_premium + self.size_premium


class Scenario(BaseModel):
    growth_multiplier: float
    margin_shift: float
    terminal_growth: float


class Sensitivity(BaseModel):
    wacc_step: float
    growth_step: float
    grid_size: int = Field(ge=3)


class DCFConfig(BaseModel):
    explicit_years: int = Field(ge=5, le=20)
    terminal_growth: float
    terminal_growth_max_spread_below_rf: float
    margin_mean_reversion_years: int
    sensitivity: Sensitivity
    scenarios: dict[Literal["bear", "base", "bull"], Scenario]


class ResidualIncomeConfig(BaseModel):
    explicit_years: int
    roe_fade_to_spread_over_ke: float
    payout_ratio_default: float


class MultiplesConfig(BaseModel):
    history_years: int
    band_std: float
    min_peers: int


class SotpConfig(BaseModel):
    holdco_discount: float = Field(ge=0, lt=1)


class QualityFlagsConfig(BaseModel):
    promoter_pledge_pct_max: float
    promoter_holding_drop_pp_1y: float
    cfo_to_pat_3y_min: float
    receivable_days_yoy_increase_max: float
    contingent_liab_to_networth_max: float
    min_median_daily_traded_value_cr: float
    flag_asm_gsm_surveillance: bool


class ValuationConfig(BaseModel):
    as_of: date
    cost_of_capital: CostOfCapital
    dcf: DCFConfig
    residual_income: ResidualIncomeConfig
    multiples: MultiplesConfig
    sotp: SotpConfig
    quality_flags: QualityFlagsConfig

    @model_validator(mode="after")
    def _terminal_growth_below_rf(self) -> ValuationConfig:
        cap = (
            self.cost_of_capital.risk_free_rate
            - self.dcf.terminal_growth_max_spread_below_rf
        )
        gs = [self.dcf.terminal_growth] + [s.terminal_growth for s in self.dcf.scenarios.values()]
        bad = [g for g in gs if g > cap + 1e-12]
        if bad:
            raise ValueError(
                f"terminal growth {bad} exceeds cap {cap:.4f} (rf - spread); "
                "a terminal rate above the risk-free rate implies the firm outgrows the economy"
            )
        return self


# ---------------------------------------------------------------- universe.yaml
class UniverseConfig(BaseModel):
    index_name: str
    constituents_csv: str
    constituents_url: str
    snapshot_date: date
    valuation_models: dict[ValuationModel, list[str]] = {}
    short_history: list[str] = []
    peer_overrides: dict[str, list[str]] = {}

    @model_validator(mode="after")
    def _no_symbol_in_two_models(self) -> UniverseConfig:
        seen: dict[str, str] = {}
        for model, symbols in self.valuation_models.items():
            for s in symbols:
                if s in seen:
                    raise ValueError(f"{s} assigned to both {seen[s]} and {model}")
                seen[s] = model
        return self

    def model_for(self, symbol: str) -> ValuationModel:
        for model, symbols in self.valuation_models.items():
            if symbol in symbols:
                return model
        return "dcf"


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_valuation_config(config_dir: Path = CONFIG_DIR) -> ValuationConfig:
    return ValuationConfig.model_validate(_load_yaml(config_dir / "valuation.yaml"))


def load_universe_config(config_dir: Path = CONFIG_DIR) -> UniverseConfig:
    return UniverseConfig.model_validate(_load_yaml(config_dir / "universe.yaml"))


def load_xbrl_mapping(config_dir: Path = CONFIG_DIR) -> dict[str, dict[str, list[str]]]:
    return _load_yaml(config_dir / "xbrl_mapping.yaml")
