from datetime import date
from pathlib import Path

import pytest
import yaml

from ere.config import load_universe_config, load_valuation_config
from ere.paths import CONFIG_DIR
from ere.universe import load_universe


def test_valuation_config_loads():
    cfg = load_valuation_config()
    assert 0.05 < cfg.cost_of_capital.risk_free_rate < 0.10
    assert set(cfg.dcf.scenarios) == {"bear", "base", "bull"}


def test_cost_of_equity_respects_beta_bounds():
    coc = load_valuation_config().cost_of_capital
    low = coc.cost_of_equity(-1.0)
    high = coc.cost_of_equity(10.0)
    assert low == pytest.approx(
        coc.risk_free_rate + coc.beta_floor * coc.equity_risk_premium + coc.size_premium
    )
    assert high == pytest.approx(
        coc.risk_free_rate + coc.beta_cap * coc.equity_risk_premium + coc.size_premium
    )


def test_terminal_growth_above_rf_is_rejected(tmp_path: Path):
    raw = yaml.safe_load((CONFIG_DIR / "valuation.yaml").read_text())
    raw["dcf"]["scenarios"]["bull"]["terminal_growth"] = 0.09
    (tmp_path / "valuation.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="terminal growth"):
        load_valuation_config(tmp_path)


def test_universe_has_100_unique_stocks():
    stocks = load_universe()
    assert len(stocks) == 100
    assert len({s.symbol for s in stocks}) == 100
    assert len({s.isin for s in stocks}) == 100


def test_valuation_paths_assigned():
    by = {s.symbol: s for s in load_universe()}
    assert by["KARURVYSYA"].valuation_model == "residual_income"
    assert by["STARHEALTH"].valuation_model == "insurance"
    assert by["CHOLAHLDNG"].valuation_model == "sotp"
    assert by["OLAELEC"].valuation_model == "ev_sales"
    assert by["CDSL"].valuation_model == "dcf"  # fee business, not a lender
    assert by["KARURVYSYA"].is_lender and not by["CDSL"].is_lender


def test_symbol_in_two_models_is_rejected(tmp_path: Path):
    raw = yaml.safe_load((CONFIG_DIR / "universe.yaml").read_text())
    raw["valuation_models"]["ev_sales"].append("CUB")
    (tmp_path / "universe.yaml").write_text(yaml.safe_dump(raw, default_flow_style=False))
    with pytest.raises(ValueError, match="CUB"):
        load_universe_config(tmp_path)


def test_stale_override_is_rejected(tmp_path: Path):
    raw = yaml.safe_load((CONFIG_DIR / "universe.yaml").read_text())
    raw["short_history"].append("NOTASTOCK")
    (tmp_path / "universe.yaml").write_text(yaml.safe_dump(raw, default_flow_style=False))
    (tmp_path / "reference").mkdir()
    csv = CONFIG_DIR / "reference" / "ind_niftysmallcap100list.csv"
    (tmp_path / "reference" / csv.name).write_bytes(csv.read_bytes())
    with pytest.raises(ValueError, match="NOTASTOCK"):
        load_universe(tmp_path)


def test_snapshot_date_is_a_date():
    assert isinstance(load_universe_config().snapshot_date, date)
