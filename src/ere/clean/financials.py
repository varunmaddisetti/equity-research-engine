"""Map raw XBRL facts to standard fields, and read them back point-in-time.

financials (long) is rebuilt from xbrl_facts + filings + config/xbrl_mapping.yaml, so editing
the mapping never needs a re-download: `ere build financials` reruns in seconds.

Point-in-time: every value keeps the filing_date of the filing it came from. A later filing
for the same period (a revision, or next year's comparative) is a new row, flagged
is_restated when the value changed. `fundamentals(as_of=...)` only sees filings made on or
before that date, so a backtest never uses numbers the market had not seen.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

RESTATE_TOL = 0.005


def load_mapping(path: Path) -> pd.DataFrame:
    """-> DataFrame(element, field, rank, section)."""
    raw = yaml.safe_load(path.read_text()) or {}
    rows = []
    for section, fields in raw.items():
        for field, elements in (fields or {}).items():
            for rank, el in enumerate(elements or []):
                rows.append((el, field, rank, section))
    df = pd.DataFrame(rows, columns=["element", "field", "rank", "section"])
    dup = df[df.duplicated("field", keep=False)].groupby("field")["section"].nunique()
    if (dup > 1).any():
        raise ValueError(f"fields defined in two sections: {sorted(dup[dup > 1].index)}")
    return df


def classify_period(start: pd.Series, end: pd.Series, instant: pd.Series) -> pd.Series:
    months = ((end - start).dt.days / 30.44).round()
    out = pd.Series("OTHER", index=start.index, dtype=object)
    out[months.between(2, 4)] = "Q"
    out[months.between(5, 7)] = "H"
    out[months.between(8, 10)] = "9M"
    out[months.between(11, 13)] = "FY"
    out[instant.astype(bool)] = "BS"
    return out


PAID_UP_ELEMENTS = ("PaidUpValueOfEquityShareCapital", "PaidUpEquityCapital", "Capital",
                    "ShareCapital")
SCALE_POWERS = (2, 3, 5, 7)      # x100 (lakh vs crore mix-ups), x1000, x1 lakh, x1 crore
SCALE_BAND = (0.5, 2.0)
MIN_FILINGS_FOR_SCALE = 4


def detect_scale_errors(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Find filings whose rupee amounts are off by a power of ten.

    Real case (IRCON Q4 FY22): the document says "Lakhs" but amounts were tagged at 1/100 of
    their rupee value, so FY revenue read Rs 74 cr instead of Rs 7,380 cr. The detector uses
    paid-up share capital, which never moves by 100x or 1,000x (a bonus at most doubles it,
    splits leave it unchanged), so a genuine collapse in revenue can never trigger it.
    Returns filing_id, isin, symbol, ratio, scale_factor for flagged filings.
    """
    pu = con.execute(
        "SELECT f.filing_id, f.isin, f.symbol, max(x.value) AS paid_up FROM xbrl_facts x "
        "JOIN filings f USING (filing_id) WHERE x.element IN ("
        + ",".join("?" * len(PAID_UP_ELEMENTS)) + ") AND x.unit = 'INR' AND x.value > 0 "
        "GROUP BY 1, 2, 3", list(PAID_UP_ELEMENTS)).df()
    out = []
    for isin, g in pu.groupby("isin"):
        if len(g) < MIN_FILINGS_FOR_SCALE:
            continue
        ref = g["paid_up"].median()
        for r in g.itertuples(index=False):
            ratio = r.paid_up / ref
            if 1 / 20 < ratio < 20:
                continue
            for k in SCALE_POWERS:
                for f in (10.0 ** k, 10.0 ** -k):
                    if SCALE_BAND[0] <= ratio * f <= SCALE_BAND[1]:
                        out.append((r.filing_id, isin, r.symbol, ratio, f))
                        break
                else:
                    continue
                break
    return pd.DataFrame(out, columns=["filing_id", "isin", "symbol", "ratio", "scale_factor"])


def build_financials(con: duckdb.DuckDBPyConnection, mapping_path: Path) -> dict[str, int]:
    fixes = detect_scale_errors(con)
    con.execute("UPDATE filings SET scale_factor = 1.0")
    for r in fixes.itertuples(index=False):
        con.execute("UPDATE filings SET scale_factor = ? WHERE filing_id = ?",
                    [r.scale_factor, r.filing_id])
    mapping = load_mapping(mapping_path)
    con.register("_map", mapping)
    facts = con.execute(
        """
        SELECT f.isin, f.symbol, f.basis, f.source, f.filed_at, f.filing_id,
               x.element, x.period_start, x.period_end, x.is_instant,
               CASE WHEN x.unit = 'INR' THEN x.value * coalesce(f.scale_factor, 1.0)
                    ELSE x.value END AS value,
               x.unit, m.field, m.rank
        FROM xbrl_facts x
        JOIN filings f USING (filing_id)
        JOIN _map m USING (element)
        """
    ).df()
    con.unregister("_map")
    con.execute("DELETE FROM financials")
    if facts.empty:
        return {"rows": 0, "restated": 0}

    facts["period_type"] = classify_period(facts.period_start, facts.period_end,
                                           facts.is_instant)
    facts = facts[facts.period_type != "OTHER"]
    facts["filing_date"] = facts["filed_at"].dt.normalize()
    # Within one filing, the best-ranked candidate element wins for each field.
    facts = facts.sort_values(["rank", "filed_at"], ascending=[True, False])
    key = ["isin", "period_end", "period_type", "basis", "field", "filing_date"]
    facts = facts.drop_duplicates(key)

    # Restated = differs from the previously filed value for the same period.
    facts = facts.sort_values(key)
    grp = facts.groupby(["isin", "period_end", "period_type", "basis", "field"])["value"]
    prev = grp.shift(1)
    facts["is_restated"] = prev.notna() & (
        (facts["value"] - prev).abs() > RESTATE_TOL * prev.abs().clip(lower=1.0))

    out = facts.rename(columns={"element": "xbrl_tag"})[
        ["isin", "symbol", "period_end", "period_type", "basis", "field", "value", "unit",
         "xbrl_tag", "filing_date", "filing_id", "is_restated", "source"]]
    con.register("_fin", out)
    cols = ", ".join(out.columns)
    con.execute(f"INSERT INTO financials ({cols}) SELECT {cols} FROM _fin")
    con.unregister("_fin")
    return {"rows": len(out), "restated": int(out.is_restated.sum()),
            "scale_fixed_filings": len(fixes)}


def fundamentals(
    con: duckdb.DuckDBPyConnection,
    period_types: tuple[str, ...] = ("Q", "FY", "BS"),
    as_of: date | None = None,
    isins: list[str] | None = None,
) -> pd.DataFrame:
    """Wide table: one row per (isin, period_end, period_type) with the latest value known as
    of `as_of` for each field. Consolidated figures are used when that period has any;
    otherwise standalone (many smallcaps have no subsidiaries). Adds derived fields."""
    # All period types are read so face value / paid-up capital can be carried across them;
    # the requested types are filtered at the end.
    q = """
        SELECT isin, symbol, period_end, period_type, basis, field, value, filing_date
        FROM financials
        WHERE TRUE
    """
    params: list = []
    if as_of is not None:
        q += " AND filing_date <= ?"
        params.append(as_of)
    if isins:
        q += " AND isin IN (" + ",".join("?" * len(isins)) + ")"
        params += isins
    df = con.execute(q, params).df()
    if df.empty:
        return df
    df = df.sort_values("filing_date").drop_duplicates(
        ["isin", "period_end", "period_type", "basis", "field"], keep="last")
    has_cons = (df[df.basis == "consolidated"]
                .groupby(["isin", "period_end", "period_type"]).size().rename("n_cons"))
    df = df.join(has_cons, on=["isin", "period_end", "period_type"])
    df = df[(df.basis == "consolidated") | df.n_cons.isna()]
    wide = df.pivot_table(index=["isin", "period_end", "period_type"], columns="field",
                          values="value", aggfunc="last")
    meta = df.groupby(["isin", "period_end", "period_type"]).agg(
        symbol=("symbol", "last"), basis=("basis", "last"), last_filed=("filing_date", "max"))
    wide = meta.join(wide).reset_index()
    # Face value and paid-up capital are often tagged only in the quarter context of a filing,
    # so carry them across period types for the same company (changes only on a split).
    wide = wide.sort_values(["isin", "period_end", "period_type"])
    for c in ("face_value", "paid_up_capital"):
        if c in wide:
            wide[c] = wide.groupby("isin")[c].transform(lambda s: s.ffill().bfill())
    wide = wide[wide.period_type.isin(period_types)]
    return add_derived(wide.reset_index(drop=True))


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df else pd.Series(np.nan, index=df.index)


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """EBITDA, share count, debt, cash, capex, NII. NaN when an input is missing."""
    z = lambda name: _col(df, name).fillna(0.0)  # noqa: E731  optional add-backs
    # Operating EBITDA: strip other income and exceptional items back out of PBT.
    df["ebitda"] = (_col(df, "pbt") - z("exceptional_items") + _col(df, "finance_cost")
                    + _col(df, "depreciation") - z("other_income"))
    df["shares"] = _col(df, "paid_up_capital") / _col(df, "face_value")
    # Bank results XBRL has no face-value tag: fall back to PAT / EPS (same period, so a
    # quarterly PAT pairs with quarterly EPS). Only sensible when both are positive.
    pat = _col(df, "pat_owners").fillna(_col(df, "pat"))
    eps = _col(df, "eps_basic")
    from_eps = (pat / eps).where((pat > 0) & (eps > 0))
    df["shares"] = df["shares"].fillna(from_eps)
    df["debt"] = _col(df, "borrowings_noncurrent").fillna(0) + _col(df, "borrowings_current")
    df.loc[_col(df, "borrowings_noncurrent").isna() & _col(df, "borrowings_current").isna(),
           "debt"] = np.nan
    df["cash_like"] = _col(df, "cash") + z("bank_balances_other") + z("current_investments")
    df["capex"] = _col(df, "capex_ppe").abs() + z("capex_intangibles").abs()
    df["nii"] = _col(df, "interest_earned") - _col(df, "interest_expended")
    return df
