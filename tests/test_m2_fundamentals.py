from datetime import date

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
