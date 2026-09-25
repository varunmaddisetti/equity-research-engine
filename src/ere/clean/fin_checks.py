"""Quality checks for fundamentals, and the golden test against annual reports.

Identity checks use the filer's own numbers, so a failure means either a mapping mistake here
or an error in the filing:
  income_sum     total income ~= revenue + other income                (non-banks, 1%)
  pat_from_pbt   PAT from continuing ops ~= PBT - tax                  (1%)
  balance_sheet  total assets ~= equity and liabilities                (0.5%)
  quarters_sum   FY revenue ~= sum of the four quarters                (2%, all four present)

Golden test: config/golden/golden_fy25.csv holds figures typed by hand from annual reports
(in Rs crore, % or crore shares). `ere check golden` compares them with what the pipeline
extracted, within 0.5%.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from ere.clean.financials import fundamentals, load_mapping

GOLDEN_TOL = 0.005
GOLDEN_ABS_TOL_PCT = 0.05     # for ratio fields expressed in %
CRORE = 1e7


def _rel_gap(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a - b).abs() / b.abs().clip(lower=1.0)


def identity_failures(wide: pd.DataFrame) -> pd.DataFrame:
    rows = []

    def check(name, mask, lhs, rhs, tol):
        ok = mask & lhs.notna() & rhs.notna()
        gap = _rel_gap(lhs, rhs)
        bad = wide[ok & (gap > tol)]
        for i, r in bad.iterrows():
            rows.append((r.symbol, r.period_end, r.period_type, r.basis, name,
                         float(lhs[i]), float(rhs[i]), float(gap[i])))

    col = lambda c: wide[c] if c in wide else pd.Series(np.nan, index=wide.index)  # noqa: E731
    flows = wide.period_type.isin(["Q", "FY"])
    non_bank = col("interest_earned").isna()
    check("income_sum", flows & non_bank, col("total_income"),
          col("revenue") + col("other_income").fillna(0), 0.01)
    check("pat_from_pbt", flows, col("pat_continuing"), col("pbt") - col("tax"), 0.01)
    check("balance_sheet", wide.period_type == "BS", col("total_assets"),
          col("equity_and_liabilities"), 0.005)

    q = wide[wide.period_type == "Q"][["isin", "period_end", "basis", "revenue"]].dropna() \
        if "revenue" in wide else pd.DataFrame(columns=["isin", "period_end", "basis", "revenue"])
    fy = wide[wide.period_type == "FY"]
    for _, r in fy.iterrows():
        if pd.isna(r.get("revenue")):
            continue
        start = r.period_end - pd.DateOffset(years=1)
        qs = q[(q["isin"] == r["isin"]) & (q.period_end > start)
               & (q.period_end <= r.period_end)]
        # Only compare like with like: pre-FY20 quarters are often standalone-only while the
        # year is consolidated.
        if len(qs) == 4 and set(qs["basis"]) == {r["basis"]}:
            s = qs.revenue.sum()
            gap = abs(s - r.revenue) / max(abs(r.revenue), 1.0)
            if gap > 0.02:
                rows.append((r.symbol, r.period_end, "FY", r.basis, "quarters_sum",
                             float(s), float(r.revenue), float(gap)))
    return pd.DataFrame(rows, columns=["symbol", "period_end", "period_type", "basis", "check",
                                       "lhs", "rhs", "rel_gap"])


def coverage(con: duckdb.DuckDBPyConnection, wide: pd.DataFrame) -> pd.DataFrame:
    filings = con.execute(
        """
        SELECT s.symbol, s.valuation_model,
               count(f.filing_id) AS filings,
               count(*) FILTER (WHERE f.status = 'parsed') AS parsed,
               count(*) FILTER (WHERE f.status IN ('error', 'missing')) AS failed,
               min(f.period_end) AS first_period, max(f.period_end) AS last_period
        FROM securities s LEFT JOIN filings f ON f.isin = s.isin
        WHERE s.in_index
        GROUP BY 1, 2
        """
    ).df()
    if wide.empty:
        filings["fy_years"] = 0
        filings["quarters"] = 0
        return filings
    top = wide.copy()
    top["top_line"] = top.get("revenue", np.nan)
    for alt in ("interest_earned", "premium_earned"):
        if alt in top:
            top["top_line"] = top["top_line"].fillna(top[alt])
    has = top[top.top_line.notna() & top.get("pat", pd.Series(np.nan, index=top.index)).notna()]
    fy = has[has.period_type == "FY"].groupby("symbol").size().rename("fy_years")
    qs = has[has.period_type == "Q"].groupby("symbol").size().rename("quarters")
    bs = top[(top.period_type == "BS") & top.get(
        "total_assets", pd.Series(np.nan, index=top.index)).notna()].groupby(
        "symbol").size().rename("balance_sheets")
    out = filings.join(fy, on="symbol").join(qs, on="symbol").join(bs, on="symbol")
    for c in ("fy_years", "quarters", "balance_sheets"):
        out[c] = out[c].fillna(0).astype(int)
    return out.sort_values(["fy_years", "quarters", "symbol"])


def unmapped_elements(con: duckdb.DuckDBPyConnection, mapping_path: Path) -> pd.DataFrame:
    mapped = set(load_mapping(mapping_path)["element"])
    df = con.execute(
        """
        SELECT x.element, count(DISTINCT x.filing_id) AS filings,
               count(DISTINCT f.symbol) AS symbols,
               bool_or(f.is_bank) AS in_bank_filings
        FROM xbrl_facts x JOIN filings f USING (filing_id)
        GROUP BY 1
        """
    ).df()
    total = con.execute("SELECT count(*) FROM filings WHERE status = 'parsed'").fetchone()[0]
    df = df[~df.element.isin(mapped)].copy()
    df["share_of_filings"] = (df.filings / max(total, 1)).round(3)
    return df.sort_values("filings", ascending=False)


# ------------------------------------------------------------------ golden test
GOLDEN_COLS = ["symbol", "basis", "period_end", "field", "expected", "unit", "source", "note"]


def golden_compare(con: duckdb.DuckDBPyConnection, golden_path: Path) -> pd.DataFrame:
    g = pd.read_csv(golden_path, dtype={"expected": str}, comment="#")
    missing = set(GOLDEN_COLS[:6]) - set(g.columns)
    if missing:
        raise ValueError(f"{golden_path.name}: missing columns {sorted(missing)}")
    g["period_end"] = pd.to_datetime(g["period_end"])
    g["expected"] = pd.to_numeric(g["expected"].str.replace(",", ""), errors="coerce")
    isins = dict(con.execute("SELECT symbol, isin FROM securities").fetchall())

    results = []
    for (sym, basis), grp in g.groupby(["symbol", "basis"]):
        isin = isins.get(sym)
        wide = fundamentals(con, ("FY", "BS"), isins=[isin]) if isin else pd.DataFrame()
        for r in grp.itertuples(index=False):
            got, status = np.nan, "no_data"
            if pd.isna(r.expected):
                status = "blank"
            elif not wide.empty:
                pt = "BS" if _is_stock_field(r.field) else "FY"
                row = wide[(wide.period_end == r.period_end) & (wide.period_type == pt)]
                if len(row) and r.field in row and pd.notna(row.iloc[0][r.field]):
                    raw = float(row.iloc[0][r.field])
                    got = raw / CRORE if r.unit in ("cr", "cr_shares") else raw
                    if row.iloc[0]["basis"] != basis:
                        status = f"basis_is_{row.iloc[0]['basis']}"
                    else:
                        tol_ok = (abs(got - r.expected) <= GOLDEN_ABS_TOL_PCT if r.unit == "pct"
                                  else abs(got - r.expected) <= GOLDEN_TOL * max(abs(r.expected),
                                                                                  1e-9))
                        status = "pass" if tol_ok else "FAIL"
            results.append((sym, basis, r.period_end.date(), r.field, r.unit, r.expected,
                            None if pd.isna(got) else round(got, 4), status))
    return pd.DataFrame(results, columns=["symbol", "basis", "period_end", "field", "unit",
                                          "expected", "extracted", "status"])


_STOCK_FIELDS = {
    "total_assets", "current_assets", "ppe", "cwip", "goodwill", "noncurrent_investments",
    "inventories", "trade_receivables", "cash", "bank_balances_other", "current_investments",
    "share_capital", "other_equity", "equity_owners", "minority_interest", "equity_total",
    "borrowings_noncurrent", "borrowings_current", "trade_payables", "current_liabilities",
    "total_liabilities", "equity_and_liabilities", "debt", "cash_like", "advances", "deposits",
    "investments", "bank_borrowings", "capital", "reserves", "loans", "debt_securities",
    "borrowings_other_than_debt_securities", "subordinated_liabilities",
}


def _is_stock_field(field: str) -> bool:
    return field in _STOCK_FIELDS
