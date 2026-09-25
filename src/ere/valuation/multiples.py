"""Relative valuation: own-history bands and peer medians, point-in-time.

History: at every month-end over the last `years`, the multiple is computed from that day's
raw close and share count, and the fundamentals that had been FILED by that day. So the band
reflects what the market actually paid for what it actually knew.

Implied value per share today = multiple x today's fundamental (per share):
  pe        -> TTM EPS           (PAT to owners / shares)
  pb        -> book value/share
  ev_ebitda -> (multiple x TTM EBITDA - net debt - minority) / shares
  ev_sales  -> (multiple x TTM revenue - net debt - minority) / shares
"""

from __future__ import annotations

from datetime import date

import duckdb
import numpy as np
import pandas as pd

from ere.clean.financials import fundamentals

MULTIPLE_CAPS = {"pe": 200.0, "pb": 50.0, "ev_ebitda": 100.0, "ev_sales": 50.0}
MIN_POINTS = 12


def _ttm(q: pd.DataFrame, fy: pd.DataFrame, col: str) -> float:
    q = q.sort_values("period_end").tail(4)
    if (len(q) == 4 and col in q and q[col].notna().all()
            and q.period_end.diff().dt.days.dropna().between(80, 100).all()
            and ("basis" not in q or q["basis"].nunique() == 1)):
        return float(q[col].sum())
    if len(fy) and col in fy and pd.notna(fy.iloc[-1][col]):
        return float(fy.iloc[-1][col])
    return np.nan


def snapshot_fundamentals(wide: pd.DataFrame) -> pd.DataFrame:
    """Per isin: TTM PAT / EBITDA / revenue, latest book value, net debt, shares."""
    rows = []
    for isin, w in wide.groupby("isin"):
        q = w[w.period_type == "Q"].sort_values("period_end")
        fy = w[w.period_type == "FY"].sort_values("period_end")
        bs = w[w.period_type == "BS"].sort_values("period_end")
        b = bs.iloc[-1] if len(bs) else pd.Series(dtype=float)
        pat = _ttm(q, fy, "pat_owners")
        if not np.isfinite(pat):
            pat = _ttm(q, fy, "pat")
        eq = b.get("equity_owners", np.nan)
        if pd.isna(eq):
            eq = b.get("capital", np.nan) + b.get("reserves", np.nan)
        both = pd.concat([q, fy]).sort_values("period_end")
        sh = both["shares"].dropna() if "shares" in both else pd.Series(dtype=float)
        debt = b.get("debt", np.nan)
        cash = b.get("cash_like", np.nan)
        rows.append({
            "isin": isin,
            "pat": pat,
            "ebitda": _ttm(q, fy, "ebitda"),
            "revenue": _ttm(q, fy, "revenue"),
            "book": eq,
            "net_debt": (0.0 if pd.isna(debt) else debt) - (0.0 if pd.isna(cash) else cash),
            "minority": 0.0 if pd.isna(b.get("minority_interest", np.nan))
            else b.get("minority_interest"),
            "shares": float(sh.iloc[-1]) if len(sh) else np.nan,
        })
    return pd.DataFrame(rows)


def compute_multiples(price: float, f: dict) -> dict[str, float]:
    shares = f.get("shares", np.nan)
    mcap = price * shares if np.isfinite(price) and np.isfinite(shares) else np.nan
    ev = mcap + f.get("net_debt", 0.0) + f.get("minority", 0.0)
    out = {}
    for name, num, den in (("pe", mcap, f.get("pat")), ("pb", mcap, f.get("book")),
                           ("ev_ebitda", ev, f.get("ebitda")),
                           ("ev_sales", ev, f.get("revenue"))):
        if np.isfinite(num) and den is not None and np.isfinite(den) and den > 0:
            v = num / den
            if 0 < v <= MULTIPLE_CAPS[name]:
                out[name] = v
    return out


def multiple_history(con: duckdb.DuckDBPyConnection, isins: list[str], as_of: date,
                     years: int = 5) -> pd.DataFrame:
    """Monthly point-in-time multiples: columns isin, date, pe, pb, ev_ebitda, ev_sales."""
    ends = pd.date_range(end=pd.Timestamp(as_of), periods=years * 12, freq="ME")
    px = con.execute(
        "SELECT security_id AS isin, date, close FROM prices_adjusted WHERE security_id IN ("
        + ",".join("?" * len(isins)) + ") AND date <= ?", [*isins, as_of]).df()
    if px.empty:
        return pd.DataFrame(columns=["isin", "date", "pe", "pb", "ev_ebitda", "ev_sales"])
    px["date"] = pd.to_datetime(px["date"])
    rows = []
    for d in ends:
        wide = fundamentals(con, ("Q", "FY", "BS"), as_of=d.date(), isins=isins)
        if wide.empty:
            continue
        snap = snapshot_fundamentals(wide).set_index("isin")
        last_px = px[px.date <= d].sort_values("date").groupby("isin").last()
        for isin in snap.index.intersection(last_px.index):
            if (d - last_px.loc[isin, "date"]).days > 10:
                continue  # not trading around that date
            mult = compute_multiples(float(last_px.loc[isin, "close"]), snap.loc[isin].to_dict())
            rows.append({"isin": isin, "date": d, **mult})
    return pd.DataFrame(rows)


def band(series: pd.Series, k: float) -> dict | None:
    s = series.dropna()
    if len(s) < MIN_POINTS:
        return None
    med, sd = float(s.median()), float(s.std(ddof=1))
    return {"low": max(med - k * sd, float(s.min())), "mid": med,
            "high": min(med + k * sd, float(s.max())), "points": int(len(s)),
            "current": float(s.iloc[-1])}


def implied_value(multiple: float, name: str, f: dict) -> float:
    shares = f.get("shares", np.nan)
    if not (np.isfinite(multiple) and np.isfinite(shares) and shares > 0):
        return np.nan
    if name == "pe":
        base = f.get("pat", np.nan)
        return multiple * base / shares if np.isfinite(base) and base > 0 else np.nan
    if name == "pb":
        base = f.get("book", np.nan)
        return multiple * base / shares if np.isfinite(base) and base > 0 else np.nan
    key = "ebitda" if name == "ev_ebitda" else "revenue"
    base = f.get(key, np.nan)
    if not (np.isfinite(base) and base > 0):
        return np.nan
    return (multiple * base - f.get("net_debt", 0.0) - f.get("minority", 0.0)) / shares


def peer_median(values: pd.Series, min_peers: int) -> float | None:
    v = values.dropna()
    v = v[v > 0]
    return float(v.median()) if len(v) >= min_peers else None
