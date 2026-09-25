from datetime import date

import numpy as np
import pandas as pd
import pytest
from fixtures_xbrl import (
    CR,
    FILES,
    INTEGRATED,
    K_ISIN,
    RESULTS_Q,
    T_ISIN,
    FakeNSE,
    integrated_annual,
    legacy_quarter,
)
from typer.testing import CliRunner

from ere.clean.fin_checks import golden_compare, identity_failures, unmapped_elements
from ere.clean.financials import build_financials, fundamentals, load_mapping
from ere.db import connect, init_db
from ere.ingest.filings import (
    ingest_filing_index,
    integrated_records_to_frame,
    results_records_to_frame,
)
from ere.ingest.xbrl import ingest_xbrl, parse_xbrl
from ere.paths import CONFIG_DIR

MAPPING = CONFIG_DIR / "xbrl_mapping.yaml"


# ------------------------------------------------------------------ parsing
def test_legacy_parse_keeps_numeric_facts_with_real_periods():
    df = parse_xbrl(legacy_quarter("2024-10-01", "2024-12-31", "2024-04-01", 120, 330), "f")
    rev = df[df.element == "RevenueFromOperations"].set_index("period_start")
    assert rev.loc[date(2024, 10, 1), "value"] == 120 * CR      # quarter (OneD)
    assert rev.loc[date(2024, 4, 1), "value"] == 330 * CR       # YTD (FourD)
    assert "Symbol" not in set(df.element)                      # text facts dropped
    assert not df.is_instant.any()


def test_integrated_parse_ignores_dimensional_contexts_and_flags_instants():
    df = parse_xbrl(integrated_annual(), "f")
    q4 = df[(df.element == "RevenueFromOperations") & (df.period_start == date(2025, 1, 1))]
    assert q4.value.tolist() == [130 * CR]  # the 1 cr breakdown line was ignored
    assets = df[df.element == "Assets"].set_index("period_end")
    assert assets.loc[date(2025, 3, 31), "value"] == 900 * CR
    assert assets.loc[date(2024, 3, 31), "value"] == 800 * CR   # prior-year comparative
    assert df[df.element == "Assets"].is_instant.all()


def test_mapping_loads_and_has_no_cross_section_duplicates():
    m = load_mapping(MAPPING)
    assert {"revenue", "pat", "cfo", "advances", "gross_npa_pct"} <= set(m.field)
    assert m[m.field == "pat"].sort_values("rank").element.iloc[0] == "ProfitLossForPeriod"


# ------------------------------------------------------------------ filing index
def test_results_index_skips_rows_without_xbrl_and_maps_basis():
    df = results_records_to_frame(RESULTS_Q["TESTCO"], T_ISIN)
    assert len(df) == 4
    assert set(df.basis) == {"consolidated", "standalone"}
    assert df.filed_at.min() == pd.Timestamp("2024-08-05 18:00")


def test_integrated_index_flags_revision_bank_and_falls_back_to_creation_date():
    df = integrated_records_to_frame(INTEGRATED["TESTCO"], T_ISIN)
    rev = df[df.is_revision].iloc[0]
    assert rev.filed_at == pd.Timestamp("2025-09-10 17:54:18")
    bank = integrated_records_to_frame(INTEGRATED["TESTBANK"], K_ISIN)
    assert bank.is_bank.all() and set(bank.basis) == {"standalone"}


# ------------------------------------------------------------------ end to end
@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "f.duckdb")
    init_db(c)
    c.execute("INSERT INTO securities (isin, symbol, name, valuation_model, short_history) VALUES "
              f"('{T_ISIN}', 'TESTCO', 'Test Co', 'dcf', FALSE), "
              f"('{K_ISIN}', 'TESTBANK', 'Test Bank', 'residual_income', FALSE)")
    client = FakeNSE()
    ingest_filing_index(c, tmp_path / "raw", [("TESTCO", T_ISIN), ("TESTBANK", K_ISIN)],
                        client=client)
    ingest_xbrl(c, tmp_path / "raw", client=client)
    build_financials(c, MAPPING)
    yield c
    c.close()


def test_all_filings_parsed(con):
    st = dict(con.execute("SELECT status, count(*) FROM filings GROUP BY 1").fetchall())
    assert st == {"parsed": len(FILES)}


def test_rerun_downloads_nothing(con, tmp_path):
    client = FakeNSE()
    stats = ingest_xbrl(con, tmp_path / "raw", client=client)
    assert stats["parsed"] == 0 and client.requests == []


def test_fundamentals_quarters_fy_and_balance_sheet(con):
    w = fundamentals(con, isins=[T_ISIN]).set_index(["period_end", "period_type"])
    q = w.xs("Q", level="period_type")
    assert q.revenue.div(CR).round().tolist() == [100, 110, 120, 130]
    # consolidated preferred over standalone for Q3 (standalone revenue was 90 cr)
    assert q.loc[pd.Timestamp("2024-12-31"), "basis"] == "consolidated"
    bs = w.loc[(pd.Timestamp("2025-03-31"), "BS")]
    assert bs.debt == 150 * CR and bs.cash_like == 55 * CR
    fy = w.loc[(pd.Timestamp("2025-03-31"), "FY")]
    assert fy.capex == 32 * CR
    assert fy.shares == pytest.approx(5 * CR)   # 50 cr paid-up / Rs 10 face value
    # EBITDA = PBT + finance + depreciation - other income = 20% of revenue
    assert fy.ebitda == pytest.approx(0.2 * 460 * CR)


def test_point_in_time_revision(con):
    before = fundamentals(con, ("FY",), as_of=date(2025, 6, 1), isins=[T_ISIN])
    after = fundamentals(con, ("FY",), isins=[T_ISIN])
    assert before.pat.iloc[0] != 999 * CR
    assert after.pat.iloc[0] == 999 * CR
    restated = con.execute("SELECT count(*) FROM financials WHERE is_restated AND field = 'pat'"
                           ).fetchone()[0]
    assert restated == 1


def test_as_of_hides_future_filings(con):
    w = fundamentals(con, ("Q",), as_of=date(2024, 12, 1), isins=[T_ISIN])
    assert w.period_end.max() == pd.Timestamp("2024-09-30")


def test_shares_fall_back_to_pat_over_eps():
    from ere.clean.financials import add_derived
    df = add_derived(pd.DataFrame({"pat": [375 * CR], "eps_basic": [4.6875]}))
    assert df.shares.iloc[0] == pytest.approx(80 * CR)


def test_bank_fields(con):
    w = fundamentals(con, ("FY", "BS"), isins=[K_ISIN]).set_index("period_type")
    assert w.loc["FY", "nii"] == 800 * CR
    assert w.loc["FY", "gross_npa_pct"] == pytest.approx(0.8)
    assert w.loc["BS", "advances"] == 80000 * CR


# ------------------------------------------------------------------ checks
def test_identities_pass_on_consistent_data(con):
    assert identity_failures(fundamentals(con)).empty


def test_identity_failure_detected(con):
    w = fundamentals(con)
    w.loc[w.period_type == "FY", "total_income"] *= 1.2
    fails = identity_failures(w)
    assert "income_sum" in set(fails.check)


def test_quarters_sum_failure_detected(con):
    w = fundamentals(con)
    w.loc[(w.period_type == "Q") & (w.period_end == pd.Timestamp("2024-06-30")), "revenue"] *= 2
    assert "quarters_sum" in set(identity_failures(w).check)


def test_unmapped_elements_reported(con):
    u = unmapped_elements(con, MAPPING)
    assert "SomeUnmappedElement" in set(u.element)
    assert "RevenueFromOperations" not in set(u.element)


def test_golden_compare(con, tmp_path):
    p = tmp_path / "g.csv"
    p.write_text(
        "# comment line, with commas\n"
        "symbol,basis,period_end,field,expected,unit,source,note\n"
        "TESTCO,consolidated,2025-03-31,revenue,460,cr,p.10,\n"
        "TESTCO,consolidated,2025-03-31,debt,151.5,cr,p.12,off by 1%\n"
        "TESTCO,consolidated,2025-03-31,cfo,,cr,,\n"
        "TESTCO,consolidated,2025-03-31,shares,5,cr_shares,,\n"
        "TESTBANK,standalone,2025-03-31,gross_npa_pct,0.82,pct,,\n")
    res = golden_compare(con, p).set_index("field")
    assert res.loc["revenue", "status"] == "pass"
    assert res.loc["debt", "status"] == "FAIL"
    assert res.loc["cfo", "status"] == "blank"
    assert res.loc["shares", "status"] == "pass"
    assert res.loc["gross_npa_pct", "status"] == "pass"


def test_cli_offline_financials(con, tmp_path, monkeypatch):
    """The raw cache written by the fixture run is enough to rebuild everything offline."""
    import ere.cli as cli

    db = tmp_path / "cli.duckdb"
    monkeypatch.setattr(cli, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(cli, "connect", lambda read_only=False: connect(db, read_only))
    with connect(db) as c:
        init_db(c)
        c.execute("INSERT INTO securities (isin, symbol, name, valuation_model, short_history) "
                  f"VALUES ('{T_ISIN}', 'TESTCO', 'Test Co', 'dcf', FALSE), "
                  f"('{K_ISIN}', 'TESTBANK', 'Test Bank', 'residual_income', FALSE)")
    r = CliRunner()
    for args in (["ingest", "filings", "--offline"], ["ingest", "xbrl", "--offline"],
                 ["build", "financials"], ["check", "financials"], ["check", "golden"]):
        res = r.invoke(cli.app, args)
        assert res.exit_code == 0, (args, res.output)
    assert (tmp_path / "processed" / "fin_coverage.csv").exists()
    cov = pd.read_csv(tmp_path / "processed" / "fin_coverage.csv").set_index("symbol")
    assert cov.loc["TESTCO", "quarters"] == 4 and cov.loc["TESTCO", "fy_years"] == 1


# ------------------------------------------------------------------ 2018-taxonomy filings
def _old_format_filing() -> bytes:
    """Modelled on ASTERDM's Q4 FY19 filing (2018 taxonomy): the 38 declared contexts are all
    dimensional breakdowns; OneD / FourD / OneI are referenced but never declared."""
    ns = ('xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2018-03-31/in-bse-fin" '
          'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
          'xmlns:xbrldi="http://xbrl.org/2006/xbrldi"')
    dim_ctx = ('<xbrli:context id="OneOperatingExpenses01D"><xbrli:entity><xbrli:identifier '
               'scheme="x">ASTERDM</xbrli:identifier></xbrli:entity><xbrli:period>'
               '<xbrli:startDate>2019-01-01</xbrli:startDate><xbrli:endDate>2019-03-31'
               '</xbrli:endDate></xbrli:period><xbrli:scenario><xbrldi:explicitMember '
               'dimension="in-bse-fin:DetailsOfOtherExpensesAxis">in-bse-fin:M1'
               '</xbrldi:explicitMember></xbrli:scenario></xbrli:context>')
    units = '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
    facts = (
        '<in-bse-fin:DateOfStartOfFinancialYear contextRef="OneD">2018-04-01'
        '</in-bse-fin:DateOfStartOfFinancialYear>'
        '<in-bse-fin:DateOfStartOfReportingPeriod contextRef="OneD">2019-01-01'
        '</in-bse-fin:DateOfStartOfReportingPeriod>'
        '<in-bse-fin:DateOfEndOfReportingPeriod contextRef="OneD">2019-03-31'
        '</in-bse-fin:DateOfEndOfReportingPeriod>'
        '<in-bse-fin:RevenueFromOperations contextRef="OneD" unitRef="INR" decimals="-7">'
        '22010300000.00</in-bse-fin:RevenueFromOperations>'
        '<in-bse-fin:RevenueFromOperations contextRef="FourD" unitRef="INR" decimals="-7">'
        '79627100000.00</in-bse-fin:RevenueFromOperations>'
        '<in-bse-fin:Assets contextRef="OneI" unitRef="INR">90000000000</in-bse-fin:Assets>'
        '<in-bse-fin:OtherExpenses contextRef="OneOperatingExpenses01D" unitRef="INR">5'
        '</in-bse-fin:OtherExpenses>'
        '<in-bse-fin:SomethingElse contextRef="SevenD" unitRef="INR">1</in-bse-fin:SomethingElse>'
    )
    return (f'<?xml version="1.0"?><xbrli:xbrl {ns}>{dim_ctx}{units}{facts}'
            '</xbrli:xbrl>').encode()


def test_old_format_undeclared_headline_contexts_are_inferred():
    df = parse_xbrl(_old_format_filing(), "old")
    rev = df[df.element == "RevenueFromOperations"].set_index("period_start")
    assert rev.loc[date(2019, 1, 1), "value"] == 22010300000.0          # OneD = Q4
    assert rev.loc[date(2018, 4, 1), "value"] == 79627100000.0          # FourD = FY19
    assert rev.loc[date(2018, 4, 1), "period_end"] == date(2019, 3, 31)
    a = df[df.element == "Assets"].iloc[0]
    assert a.is_instant and a.period_end == date(2019, 3, 31)            # OneI
    assert "OtherExpenses" not in set(df.element)                        # breakdown skipped
    assert "SomethingElse" not in set(df.element)                        # unknown id skipped


def test_declared_contexts_are_never_overridden():
    df = parse_xbrl(legacy_quarter("2024-10-01", "2024-12-31", "2024-04-01", 120, 330), "f")
    assert set(df.period_start) == {date(2024, 10, 1), date(2024, 4, 1)}


def test_quarters_sum_skips_mixed_basis(con):
    w = fundamentals(con)
    # pretend the June quarter was only filed standalone (pre-FY20 practice) with other numbers
    m = (w.period_type == "Q") & (w.period_end == pd.Timestamp("2024-06-30"))
    w.loc[m, "basis"] = "standalone"
    w.loc[m, "revenue"] *= 0.7
    assert "quarters_sum" not in set(identity_failures(w).check)


def test_insurance_fields_mapped():
    m = load_mapping(MAPPING)
    assert {"premium_earned", "combined_ratio", "solvency_ratio"} <= set(m.field)
    assert "ProfitLossAfterTax" in set(m[m.field == "pat"].element)


# ------------------------------------------------------------------ unit-scale errors
def test_misscaled_filing_detected_and_corrected(con):
    """IRCON Q4 FY22: every amount tagged at 1/100 of its rupee value."""
    from ere.clean.financials import detect_scale_errors

    # TESTCO has 5 filings with paid-up capital 50 cr; make the Q2 filing 1/100 of that
    con.execute("UPDATE xbrl_facts SET value = value / 100 WHERE filing_id = 'INDAS_2_Q2_C.xml' "
                "AND unit = 'INR'")
    fixes = detect_scale_errors(con)
    assert list(fixes.filing_id) == ["INDAS_2_Q2_C.xml"]
    assert fixes.scale_factor.iloc[0] == 100
    stats = build_financials(con, MAPPING)
    assert stats["scale_fixed_filings"] == 1
    w = fundamentals(con, ("Q",), isins=[T_ISIN]).set_index("period_end")
    assert w.loc[pd.Timestamp("2024-09-30"), "revenue"] == pytest.approx(110 * CR)
    eps = con.execute("SELECT value FROM financials WHERE filing_id = 'INDAS_2_Q2_C.xml' "
                      "AND field = 'eps_basic' AND period_type = 'Q'").fetchone()[0]
    assert eps == pytest.approx(1.5)          # per-share values are never rescaled


def test_bonus_doubling_is_not_a_scale_error(con):
    from ere.clean.financials import detect_scale_errors

    con.execute("UPDATE xbrl_facts SET value = value * 2 WHERE filing_id = 'INDAS_3_Q3_C.xml' "
                "AND element = 'PaidUpValueOfEquityShareCapital'")
    assert detect_scale_errors(con).empty


def test_pat_check_allows_regulatory_deferral_and_associates():
    w = pd.DataFrame({"symbol": ["U"] * 2, "isin": ["I"] * 2, "basis": ["consolidated"] * 2,
                      "period_type": ["Q", "Q"],
                      "period_end": pd.to_datetime(["2019-06-30", "2019-09-30"]),
                      "pbt": [150.0, 150.0], "tax": [30.0, 30.0],
                      "pat_continuing": [216.0, 130.0],
                      "regulatory_deferral_movement": [96.0, 0.0],
                      "associates_share": [0.0, 10.0]})
    assert identity_failures(w).empty


def test_quarters_gap_explained_by_discontinued_operations():
    q_ends = pd.to_datetime(["2019-06-30", "2019-09-30", "2019-12-31", "2020-03-31"])
    w = pd.DataFrame({
        "symbol": "T", "isin": "I", "basis": "consolidated",
        "period_type": ["Q"] * 4 + ["FY"], "period_end": list(q_ends) + [q_ends[-1]],
        "revenue": [30.0, 30.0, 30.0, 20.0, 80.0],     # year restated without a sold unit
        "pat_discontinued": [np.nan] * 4 + [12.0]})
    f = identity_failures(w)
    assert list(f.check) == ["quarters_sum"] and f.explanation.iloc[0] == "discontinued_operations"


def test_mistyped_paid_up_tag_alone_does_not_rescale_the_filing(con):
    """KAYNES-style: only the paid-up capital tag is off by 10^5; revenue is fine."""
    from ere.clean.financials import detect_scale_errors

    con.execute("UPDATE xbrl_facts SET value = value * 100000 WHERE filing_id = "
                "'INDAS_2_Q2_C.xml' AND element = 'PaidUpValueOfEquityShareCapital'")
    fixes = detect_scale_errors(con)
    assert list(fixes.scope) == ["paid_up"] and fixes.scale_factor.iloc[0] == 1e-5
    stats = build_financials(con, MAPPING)
    assert stats["scale_fixed_filings"] == 0 and stats["paid_up_tag_fixes"] == 1
    w = fundamentals(con, ("Q",), isins=[T_ISIN]).set_index("period_end")
    q2 = w.loc[pd.Timestamp("2024-09-30")]
    assert q2.revenue == pytest.approx(110 * CR)          # untouched
    assert q2.paid_up_capital == pytest.approx(50 * CR)   # corrected
    assert q2.shares == pytest.approx(5 * CR)
