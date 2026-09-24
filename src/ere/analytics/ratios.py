"""Financial ratios from the point-in-time fundamentals table (one company at a time).

Conventions (documented because every analyst defines these slightly differently):
- TTM = sum of the last four reported quarters, only if they are consecutive; otherwise the
  latest full year is used and `ttm_source` says so.
- EBIT = PBT + finance cost (so it includes other income); EBITDA as in add_derived
  (operating: excludes other income and exceptional items).
- ROE = PAT to owners / average owners' equity (opening and closing balance sheet).
- ROCE = EBIT / average (total equity + debt).
- Working-capital days use revenue as the denominator (COGS is not reliably separable
  across Ind AS formats).
- Banks: NIM is approximated as NII / average (advances + investments); the true
  interest-earning-assets figure is not in the results XBRL.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FLOW_FIELDS = ["revenue", "ebitda", "pat", "pat_owners", "finance_cost", "depreciation",
               "other_income", "pbt", "interest_earned", "interest_expended", "nii",
               "provisions", "operating_expenses", "interest_income"]


def _g(row, col):
    if row is None:
        return np.nan
    v = row.get(col, np.nan) if hasattr(row, "get") else np.nan
    return float(v) if v is not None and pd.notna(v) else np.nan


def _div(a, b):
    return a / b if np.isfinite(a) and np.isfinite(b) and b != 0 else np.nan


def _avg(a, b):
    vals = [x for x in (a, b) if np.isfinite(x)]
    return float(np.mean(vals)) if vals else np.nan


def cagr(first: float, last: float, years: float) -> float:
    if not (np.isfinite(first) and np.isfinite(last)) or first <= 0 or last <= 0 or years <= 0:
        return np.nan
    return (last / first) ** (1 / years) - 1


def ttm(q: pd.DataFrame) -> tuple[dict[str, float], pd.Timestamp | None]:
    """Sum of last four consecutive quarters. Returns ({field: value}, last quarter end)."""
    q = q.sort_values("period_end").tail(4)
    if len(q) < 4:
        return {}, None
    gaps = q.period_end.diff().dt.days.dropna()
    if not gaps.between(80, 100).all():
        return {}, None
    out = {}
    for f in FLOW_FIELDS:
        if f in q and q[f].notna().all():
            out[f] = float(q[f].sum())
    return out, q.period_end.iloc[-1]


def company_ratios(w: pd.DataFrame, is_lender: bool = False) -> dict[str, float]:
    """w: fundamentals rows (Q, FY, BS) for one company, as returned by fundamentals()."""
    out: dict[str, float] = {}
    if w is None or w.empty:
        return out
    fy = w[w.period_type == "FY"].sort_values("period_end")
    bs = w[w.period_type == "BS"].sort_values("period_end")
    q = w[w.period_type == "Q"].sort_values("period_end")

    last_fy = fy.iloc[-1] if len(fy) else None
    out["fy_years"] = float(len(fy))
    if last_fy is not None:
        out["last_fy_end_year"] = float(last_fy.period_end.year)

    t, t_end = ttm(q)
    if t:
        out["ttm_source_quarters"] = 1.0
        out["ttm_end_year"] = float(t_end.year)
        out["ttm_end_month"] = float(t_end.month)
    elif last_fy is not None:
        t = {f: _g(last_fy, f) for f in FLOW_FIELDS}
        out["ttm_source_quarters"] = 0.0
    for f, v in t.items():
        if np.isfinite(v):
            out[f"ttm_{f}"] = v

    # balance sheets: latest and one year earlier
    b1 = bs.iloc[-1] if len(bs) else None
    b0 = None
    if b1 is not None:
        prev = bs[bs.period_end <= b1.period_end - pd.DateOffset(days=300)]
        b0 = prev.iloc[-1] if len(prev) else None
        for f in ("total_assets", "equity_owners", "equity_total", "debt", "cash_like",
                  "advances", "deposits", "loans", "trade_receivables", "inventories",
                  "trade_payables", "minority_interest", "investments"):
            v = _g(b1, f)
            if np.isfinite(v):
                out[f"bs_{f}"] = v
        out["bs_year"] = float(b1.period_end.year)

    both = pd.concat([q, fy]).sort_values("period_end")
    sh = both["shares"].dropna() if "shares" in both else pd.Series(dtype=float)
    shares = float(sh.iloc[-1]) if len(sh) else np.nan
    out["shares"] = shares

    # growth from full years
    if len(fy) >= 2:
        for n in (3, 5):
            if len(fy) > n:
                a, b = fy.iloc[-1 - n], fy.iloc[-1]
                for f in ("revenue", "ebitda", "pat_owners", "eps_basic", "nii",
                          "interest_income"):
                    v = cagr(_g(a, f), _g(b, f), n)
                    if np.isfinite(v):
                        out[f"cagr_{n}y_{f}"] = v
        a, b = fy.iloc[-2], fy.iloc[-1]
        for f in ("revenue", "pat_owners", "nii"):
            v = _div(_g(b, f) - _g(a, f), abs(_g(a, f)))
            if np.isfinite(v):
                out[f"growth_1y_{f}"] = v

    rev, ebitda = t.get("revenue", np.nan), t.get("ebitda", np.nan)
    pat_o = t.get("pat_owners", t.get("pat", np.nan))
    if not np.isfinite(pat_o):
        pat_o = t.get("pat", np.nan)
    out["ebitda_margin"] = _div(ebitda, rev)
    out["pat_margin"] = _div(pat_o, rev)

    eq_avg = _avg(_g(b1, "equity_owners"), _g(b0, "equity_owners"))
    if is_lender and not np.isfinite(eq_avg):
        eq_avg = _avg(_g(b1, "capital") + _g(b1, "reserves"),
                      _g(b0, "capital") + _g(b0, "reserves"))
        if np.isfinite(_g(b1, "capital") + _g(b1, "reserves")):
            out["bs_equity_owners"] = _g(b1, "capital") + _g(b1, "reserves")
    pat_fy = _g(last_fy, "pat_owners") if last_fy is not None else np.nan
    if not np.isfinite(pat_fy):
        pat_fy = _g(last_fy, "pat")
    out["roe"] = _div(pat_fy, eq_avg)

    if not is_lender:
        ebit = t.get("pbt", np.nan) + t.get("finance_cost", 0.0)
        cap = _avg(_g(b1, "equity_total") + _nz(_g(b1, "debt")),
                   _g(b0, "equity_total") + _nz(_g(b0, "debt")))
        out["ebit_ttm"] = ebit
        out["roce"] = _div(ebit, cap)
        net_debt = _g(b1, "debt") - _nz(_g(b1, "cash_like"))
        out["net_debt"] = net_debt
        out["net_debt_to_ebitda"] = _div(net_debt, ebitda)
        out["interest_cover"] = _div(ebit, t.get("finance_cost", np.nan))
        if last_fy is not None and np.isfinite(_g(last_fy, "revenue")):
            r = _g(last_fy, "revenue")
            out["receivable_days"] = _div(_g(b1, "trade_receivables") * 365, r)
            out["inventory_days"] = _div(_g(b1, "inventories") * 365, r)
            out["payable_days"] = _div(_g(b1, "trade_payables") * 365, r)
            out["asset_turnover"] = _div(r, _avg(_g(b1, "total_assets"),
                                                 _g(b0, "total_assets")))
            if b0 is not None and len(fy) >= 2:
                r0 = _g(fy.iloc[-2], "revenue")
                d0 = _div(_g(b0, "trade_receivables") * 365, r0)
                out["receivable_days_prev"] = d0
        tail3 = fy.tail(3)
        if len(tail3) == 3:
            cfo = tail3["cfo"].sum() if "cfo" in tail3 and tail3["cfo"].notna().all() else np.nan
            pat3 = tail3["pat"].sum() if "pat" in tail3 and tail3["pat"].notna().all() else np.nan
            capex = (tail3["capex"].sum() if "capex" in tail3 and tail3["capex"].notna().all()
                     else np.nan)
            out["cfo_to_pat_3y"] = _div(cfo, pat3)
            out["fcf_to_pat_3y"] = _div(cfo - capex, pat3)
            out["fcf_3y"] = cfo - capex if np.isfinite(cfo - capex) else np.nan
        if last_fy is not None:
            out["fcf_fy"] = _g(last_fy, "cfo") - _g(last_fy, "capex")
            out["capex_fy"] = _g(last_fy, "capex")
            out["depreciation_fy"] = _g(last_fy, "depreciation")
    else:
        nii = t.get("nii", np.nan)
        earning = _avg(_g(b1, "advances") + _nz(_g(b1, "investments")),
                       _g(b0, "advances") + _nz(_g(b0, "investments")))
        if not np.isfinite(nii) and np.isfinite(t.get("interest_income", np.nan)):
            nii = t.get("interest_income", np.nan) - t.get("finance_cost", np.nan)
            earning = _avg(_g(b1, "loans"), _g(b0, "loans"))
        out["nii_ttm"] = nii
        out["nim_approx"] = _div(nii, earning)
        income = nii + _nz(t.get("other_income", np.nan))
        out["cost_to_income"] = _div(t.get("operating_expenses", np.nan), income)
        out["credit_cost"] = _div(t.get("provisions", np.nan),
                                  _avg(_g(b1, "advances"), _g(b0, "advances")))
        if last_fy is not None:
            for f in ("gross_npa_pct", "net_npa_pct", "return_on_assets", "cet1_ratio"):
                v = _g(last_fy, f)
                if np.isfinite(v):
                    out[f] = v
            g, n = _g(last_fy, "gross_npa"), _g(last_fy, "net_npa")
            out["provision_coverage"] = 1 - _div(n, g) if np.isfinite(_div(n, g)) else np.nan
        for f in ("advances", "deposits", "loans"):
            v = _div(_g(b1, f) - _g(b0, f), _g(b0, f))
            if np.isfinite(v):
                out[f"growth_1y_{f}"] = v
    return {k: v for k, v in out.items() if v is not None and np.isfinite(v)}


def _nz(x: float) -> float:
    return x if np.isfinite(x) else 0.0
