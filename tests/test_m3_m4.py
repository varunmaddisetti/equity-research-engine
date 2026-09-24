from datetime import date

import numpy as np
import pandas as pd
import pytest
from fixtures_market import BANK, CR, STOCKS, load_market
from typer.testing import CliRunner

from ere.analytics.build import build_analytics, latest_metrics
from ere.analytics.quality import evaluate_flags
from ere.analytics.ratios import cagr, ttm
from ere.analytics.risk import beta, blume, max_drawdown, trailing_return
from ere.config import load_universe_config, load_valuation_config
from ere.db import connect, init_db
from ere.ingest.shareholding import pledge_to_frame, shp_to_frame
from ere.paths import CONFIG_DIR
from ere.valuation.build import build_valuation, football_field
from ere.valuation.dcf import DCFInputs, reverse_dcf, run_dcf, sensitivity_grid
from ere.valuation.multiples import band, compute_multiples, implied_value
from ere.valuation.residual_income import run_residual_income

VAL = load_valuation_config()
UNI = load_universe_config()


# ------------------------------------------------------------------ risk
def test_beta_recovers_known_slope():
    rng = np.random.default_rng(1)
    d = pd.bdate_range("2022-01-01", periods=800)
    ri = rng.normal(0, 0.01, len(d))
    idx = pd.Series(100 * np.exp(np.cumsum(ri)), index=d)
    stock = pd.Series(100 * np.exp(np.cumsum(1.5 * ri)), index=d)
    b, n = beta(stock, idx, 104)
    assert b == pytest.approx(1.5, abs=0.01) and n == 104
    assert blume(1.5) == pytest.approx(1.335)


def test_drawdown_and_returns():
    px = pd.Series([100, 120, 60, 90], index=pd.date_range("2025-01-01", periods=4, freq="180D"))
    assert max_drawdown(px) == pytest.approx(-0.5)
    # one year before the last date (2026-06-25) the latest price is the first one, 100
    assert trailing_return(px, 1) == pytest.approx(90 / 100 - 1)


# ------------------------------------------------------------------ ratios
def test_ttm_requires_consecutive_quarters():
    q = pd.DataFrame({"period_end": pd.to_datetime(["2025-06-30", "2025-09-30", "2025-12-31",
                                                     "2026-03-31"]), "revenue": [1, 2, 3, 4]})
    t, end = ttm(q)
    assert t["revenue"] == 10 and end == pd.Timestamp("2026-03-31")
    gap = q.copy()
    gap.loc[1, "period_end"] = pd.Timestamp("2025-08-31")
    assert ttm(gap) == ({}, None)


def test_cagr():
    assert cagr(100, 121, 2) == pytest.approx(0.10)
    assert np.isnan(cagr(-5, 10, 2))


# ------------------------------------------------------------------ shareholding parsing
def test_shareholding_parsers():
    shp = [{"date": "30-JUN-2026", "pr_and_prgrp": "53.46", "public_val": "46.54",
            "employeeTrusts": "0", "submissionDate": "20-JUL-2026"},
           {"date": "30-JUN-2025", "pr_and_prgrp": "56.00", "public_val": "44.00",
            "employeeTrusts": "0", "submissionDate": "20-JUL-2025"}]
    df = shp_to_frame(shp, "INE0", "X")
    assert set(df.category) == {"promoter", "public", "employee_trusts"}
    pl = pledge_to_frame({"data": [{"shp": "30-Jun-2026", "percPromoterShares": "  12.50",
                                    "percTotShares": " 6.7", "percPromoterHolding": " 53.46"}]})
    assert pl.pledged_pct.iloc[0] == 12.5


# ------------------------------------------------------------------ flags
def test_quality_flags():
    m = {"pledged_pct": 15.0, "cfo_to_pat_3y": 0.5, "median_traded_value_6m_cr": 2.0,
         "promoter_change_1y_pp": -3.0, "net_debt_to_ebitda": 1.0}
    flags = {f: t for f, t, *_ in evaluate_flags(m, VAL.quality_flags, False, True)}
    assert flags["promoter_pledge_high"] and flags["low_cash_conversion"]
    assert flags["low_liquidity"] and flags["promoter_holding_drop"]
    assert flags["high_leverage"] is False and flags["short_history"] is True
    assert flags["receivable_days_rising"] is None      # no data -> undetermined
    assert "high_gnpa" not in flags                       # bank-only flag


# ------------------------------------------------------------------ DCF, checked by hand
TOY = DCFInputs(revenue=100.0, ebitda_margin=0.20, target_margin=0.20, g1=0.10, da_pct=0.05,
                capex_pct=0.05, nwc_pct=0.10, tax_rate=0.25, net_debt=20.0,
                minority_interest=0.0, shares=10.0)


def test_dcf_matches_hand_calculation():
    """Two-year toy model, every line worked out by hand (see comments)."""
    r = run_dcf(TOY, wacc=0.12, g_term=0.04, years=2, margin_years=1)
    # Year 1: growth 10% -> revenue 110; EBITDA 22; D&A 5.5; EBIT 16.5; NOPAT 12.375
    #         capex % = 5% + (7% - 5%) * 1/2 = 6% -> 6.6 ; dNWC = 10% * 10 = 1.0
    #         FCFF = 12.375 + 5.5 - 6.6 - 1.0 = 10.275
    # Year 2: growth 4% -> revenue 114.4; EBITDA 22.88; D&A 5.72; EBIT 17.16; NOPAT 12.87
    #         capex 7% -> 8.008 ; dNWC = 0.44 ; FCFF = 12.87 + 5.72 - 8.008 - 0.44 = 10.142
    # TV = 10.142 * 1.04 / (0.12 - 0.04) = 131.846
    fcff1, fcff2 = 10.275, 10.142
    tv = fcff2 * 1.04 / 0.08
    ev = fcff1 / 1.12 + fcff2 / 1.12**2 + tv / 1.12**2
    assert r.years[0]["fcff"] == pytest.approx(fcff1)
    assert r.years[1]["fcff"] == pytest.approx(fcff2)
    assert r.enterprise_value == pytest.approx(ev)
    assert r.value_per_share == pytest.approx((ev - 20.0) / 10.0)


def test_dcf_rejects_growth_above_wacc():
    with pytest.raises(ValueError):
        run_dcf(TOY, 0.05, 0.06)


def test_reverse_dcf_recovers_growth():
    price = run_dcf(TOY, 0.13, 0.05).value_per_share
    g = reverse_dcf(TOY, price, 0.13, 0.05, VAL.dcf)
    assert g == pytest.approx(0.10, abs=1e-4)


def test_sensitivity_grid_centre_is_base():
    grid = sensitivity_grid(TOY, 0.13, 0.05, VAL.dcf)
    n = VAL.dcf.sensitivity.grid_size
    centre = grid["value_per_share"][n // 2][n // 2]
    assert centre == pytest.approx(run_dcf(TOY, 0.13, 0.05).value_per_share)
    # higher WACC -> lower value, along the middle column
    col = [row[n // 2] for row in grid["value_per_share"]]
    assert all(a > b for a, b in zip(col, col[1:], strict=False))


# ------------------------------------------------------------------ residual income
def test_residual_income_is_book_when_roe_equals_ke():
    r = run_residual_income(bv0=1000, roe0=0.14, ke=0.14, spread=0.0, payout=0.2,
                            g_term=0.05, shares=10)
    assert r.value_per_share == pytest.approx(100.0)
    assert r.implied_pb == pytest.approx(1.0)


def test_residual_income_premium_when_roe_above_ke():
    hi = run_residual_income(1000, 0.20, 0.14, 0.02, 0.2, 0.05, 10)
    assert hi.implied_pb > 1.0


# ------------------------------------------------------------------ multiples
def test_band_and_implied_values():
    b = band(pd.Series(np.linspace(10, 30, 24)), 1.0)
    assert b["mid"] == pytest.approx(20.0) and b["low"] < 20 < b["high"]
    f = {"pat": 50 * CR, "book": 400 * CR, "ebitda": 100 * CR, "revenue": 500 * CR,
         "net_debt": 100 * CR, "minority": 0.0, "shares": 10 * CR}
    assert implied_value(20, "pe", f) == pytest.approx(100.0)
    assert implied_value(10, "ev_ebitda", f) == pytest.approx(90.0)
    m = compute_multiples(100.0, f)
    assert m["pe"] == pytest.approx(20.0) and m["ev_ebitda"] == pytest.approx(11.0)


# ------------------------------------------------------------------ end to end
@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "m.duckdb")
    init_db(c)
    load_market(c)
    yield c
    c.close()


def test_build_analytics_end_to_end(con):
    st = build_analytics(con, VAL, UNI)
    assert st.securities == 5 and st.no_prices == 0 and st.no_fundamentals == 0
    lm = latest_metrics(con).set_index("symbol")
    assert lm.loc["ALPHA", "beta_raw"] == pytest.approx(1.5, abs=0.25)
    assert lm.loc["ALPHA", "ebitda_margin"] == pytest.approx(0.19)
    assert lm.loc["ALPHA", "roce"] > 0 and lm.loc["ALPHA", "pe_ttm"] > 0
    assert lm.loc[BANK[0], "nim_approx"] > 0 and "roce" not in lm.columns or np.isnan(
        lm.loc[BANK[0]].get("roce", np.nan))
    flags = con.execute("SELECT flag, triggered FROM quality_flags WHERE symbol = 'GAMMA'"
                        ).df().set_index("flag").triggered
    assert bool(flags["low_liquidity"]) is True
    peers = con.execute("SELECT count(*) FROM peers WHERE isin = ?", [STOCKS[0][1]]).fetchone()
    assert peers[0] == 3


def test_build_valuation_end_to_end(con):
    build_analytics(con, VAL, UNI)
    st = build_valuation(con, VAL, UNI, CONFIG_DIR / "sotp.yaml", history_years=2)
    assert st.dcf_run == 12 and st.ri_run == 3
    ff = football_field(con, STOCKS[0][1]).set_index("method")
    assert {"dcf", "pe_band", "ev_ebitda_band", "pe_peers"} <= set(ff.index)
    d = ff.loc["dcf"]
    assert d["low"] < d["mid"] < d["high"]
    bank = football_field(con, BANK[1]).set_index("method")
    assert "residual_income" in bank.index
    a = con.execute("SELECT assumptions FROM valuations WHERE isin = ? AND method = 'dcf' "
                    "AND scenario = 'base'", [STOCKS[0][1]]).fetchone()[0]
    assert '"sensitivity"' in a and '"reverse_dcf_growth"' in a


def test_point_in_time_analytics(con):
    st = build_analytics(con, VAL, UNI, as_of=date(2025, 6, 30))
    lm = latest_metrics(con, date(2025, 6, 30)).set_index("symbol")
    # latest quarter filed by 2025-06-30 is Dec-2024 (filed mid-Feb); Mar-2025 filed mid-May
    assert lm.loc["ALPHA", "ttm_end_year"] == 2025 and lm.loc["ALPHA", "ttm_end_month"] == 3
    assert st.as_of == date(2025, 6, 30)


def test_cli_show(con, tmp_path, monkeypatch):
    import ere.cli as cli

    build_analytics(con, VAL, UNI)
    build_valuation(con, VAL, UNI, CONFIG_DIR / "sotp.yaml", history_years=2)
    db = tmp_path / "m.duckdb"
    con.close()
    monkeypatch.setattr(cli, "connect", lambda read_only=False: connect(db, read_only))
    res = CliRunner().invoke(cli.app, ["show", "ALPHA"])
    assert res.exit_code == 0, res.output
    assert "Quality flags" in res.output and "Valuation ranges" in res.output
