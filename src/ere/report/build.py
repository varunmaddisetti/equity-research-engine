"""Render one self-contained HTML report per stock, plus an index page.

`ere report KAYNES` writes reports/KAYNES.html; `ere report --all` writes every stock and
reports/index.html. Pages are fully self-contained (inline CSS and SVG, a few lines of JS on
the index for sorting) so they work from a laptop folder or GitHub Pages alike.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ere.analytics.build import LENDER_MODELS, SMALL_INDEX, latest_metrics
from ere.clean.financials import fundamentals
from ere.config import ValuationConfig
from ere.paths import CONFIG_DIR
from ere.report import charts
from ere.valuation.build import football_field

TEMPLATES = Path(__file__).parent / "templates"
CRORE = 1e7
DISCLAIMER = ("Educational and analytical use only. Not investment advice, not a recommendation "
              "to buy, sell or hold. The author is not a SEBI-registered Research Analyst. "
              "Valuations are ranges under stated assumptions, not target prices.")
DISCLAIMER_LONG = (
    DISCLAIMER + " Figures are extracted automatically from exchange filings and may contain "
    "errors or omissions; check them against the company's own disclosures before relying on "
    "them. Past price behaviour does not predict future returns.")
MODEL_LABELS = {"dcf": "DCF + multiples", "residual_income": "Residual income (lender)",
                "insurance": "Residual income (insurer)", "sotp": "Sum of the parts",
                "ev_sales": "EV/Sales (pre-profit)"}
METHOD_LABELS = {"dcf": "DCF (bear–bull)", "residual_income": "Residual income (bear–bull)",
                 "sotp": "Sum of the parts", "pe_band": "P/E, own history band",
                 "pe_peers": "P/E, peer median", "pb_band": "P/B, own history band",
                 "pb_peers": "P/B, peer median", "ev_ebitda_band": "EV/EBITDA, own history band",
                 "ev_ebitda_peers": "EV/EBITDA, peer median",
                 "ev_sales_band": "EV/Sales, own history band",
                 "ev_sales_peers": "EV/Sales, peer median"}
FLAG_LABELS = {"promoter_pledge_high": "High promoter pledge",
               "promoter_pledge_rising": "Promoter pledge rising",
               "promoter_holding_drop": "Promoter holding falling",
               "low_cash_conversion": "Low cash conversion",
               "receivable_days_rising": "Receivable days rising",
               "high_leverage": "High leverage", "negative_ebitda": "Negative EBITDA",
               "high_gnpa": "High gross NPA", "low_liquidity": "Low liquidity",
               "short_history": "Short history"}


# ------------------------------------------------------------------ formatting
def _ok(v) -> bool:
    return v is not None and isinstance(v, (int, float, np.floating)) and np.isfinite(v)


def cr(v, d=0) -> str:
    return f"{v / CRORE:,.{d}f}" if _ok(v) else "–"


def pct(v, d=1) -> str:
    return f"{v * 100:.{d}f}%" if _ok(v) else "–"


def pp(v, d=2) -> str:
    return f"{v:.{d}f}%" if _ok(v) else "–"


def num(v, d=0) -> str:
    return f"{v:,.{d}f}" if _ok(v) else "–"


def mult(v) -> str:
    return f"{v:.1f}x" if _ok(v) else "–"


def fy_label(ts: pd.Timestamp) -> str:
    y = ts.year if ts.month <= 3 else ts.year + 1
    return f"FY{y % 100:02d}"


def q_label(ts: pd.Timestamp) -> str:
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(ts.month, 0)
    return f"Q{q} {fy_label(ts)}"


# ------------------------------------------------------------------ sections
def _fy_tables(w: pd.DataFrame, lender: bool, insurer: bool = False):
    fy = w[w.period_type == "FY"].sort_values("period_end").tail(6)
    bs = w[w.period_type == "BS"].sort_values("period_end")
    cols = [fy_label(p) for p in fy.period_end]
    g = lambda r, c: r[c] if c in r and pd.notna(r[c]) else np.nan  # noqa: E731
    if insurer:
        spec = [("Gross premium written", "gross_premium", cr),
                ("Net premium earned", "premium_earned", cr),
                ("Incurred claims", "incurred_claims", cr),
                ("Underwriting profit", "underwriting_profit", cr),
                ("Investment income", "investment_income", cr), ("PAT", "pat", cr),
                ("Combined ratio %", "combined_ratio", pp), ("Solvency ratio", "solvency_ratio",
                                                              lambda v: num(v, 2))]
    elif lender:
        spec = [("Interest earned", "interest_earned", cr), ("Net interest income", "nii", cr),
                ("Operating profit (pre-provision)", "operating_profit_pre_provision", cr),
                ("Provisions", "provisions", cr), ("PAT", "pat", cr),
                ("Gross NPA %", "gross_npa_pct", pp), ("Net NPA %", "net_npa_pct", pp)]
    else:
        spec = [("Revenue", "revenue", cr), ("EBITDA", "ebitda", cr), ("PAT (owners)",
                "pat_owners", cr), ("EPS (Rs)", "eps_basic", lambda v: num(v, 2)),
                ("Operating cash flow", "cfo", cr), ("Capex", "capex", cr)]
    rows = [{"label": lab, "vals": [f(g(r, c)) for _, r in fy.iterrows()]} for lab, c, f in spec]
    if not lender:
        rows.insert(2, {"label": "EBITDA margin", "vals": [
            pct(g(r, "ebitda") / g(r, "revenue")) if _ok(g(r, "revenue")) and g(r, "revenue")
            else "–" for _, r in fy.iterrows()]})
        fcf = [cr(g(r, "cfo") - g(r, "capex")) for _, r in fy.iterrows()]
        rows.append({"label": "Free cash flow", "vals": fcf})

    # ratio trends per FY
    rrows, roe_s, roce_s, margin_s = [], {}, {}, {}
    for _, r in fy.iterrows():
        pe_ = r.period_end
        b1 = bs[bs.period_end == pe_]
        b0 = bs[(bs.period_end <= pe_ - pd.DateOffset(days=300))]
        b1 = b1.iloc[-1] if len(b1) else None
        b0 = b0.iloc[-1] if len(b0) else None

        def eq(b):
            if b is None:
                return np.nan
            v = g(b, "equity_owners")
            return v if _ok(v) else g(b, "capital") + g(b, "reserves")
        eqs = [x for x in (eq(b1), eq(b0)) if _ok(x)]
        pat = g(r, "pat_owners") if _ok(g(r, "pat_owners")) else g(r, "pat")
        roe_s[pe_] = pat / np.mean(eqs) if eqs and _ok(pat) else np.nan
        if not lender:
            caps = [g(b, "equity_total") + (g(b, "debt") if _ok(g(b, "debt")) else 0)
                    for b in (b1, b0) if b is not None and _ok(g(b, "equity_total"))]
            ebit = g(r, "pbt") + (g(r, "finance_cost") if _ok(g(r, "finance_cost")) else 0)
            roce_s[pe_] = ebit / np.mean(caps) if caps and _ok(ebit) else np.nan
            margin_s[pe_] = (g(r, "ebitda") / g(r, "revenue")
                             if _ok(g(r, "revenue")) and g(r, "revenue") else np.nan)
    rrows.append({"label": "ROE", "vals": [pct(roe_s.get(p)) for p in fy.period_end]})
    if not lender:
        rrows.append({"label": "ROCE", "vals": [pct(roce_s.get(p)) for p in fy.period_end]})
        rrows.append({"label": "EBITDA margin", "vals": [pct(margin_s.get(p))
                                                         for p in fy.period_end]})
    fy_idx = {p: fy_label(p) for p in fy.period_end}
    series = {"ROE": pd.Series(roe_s).rename(index=fy_idx)}
    if not lender:
        series["ROCE"] = pd.Series(roce_s).rename(index=fy_idx)
    ratio_chart = (charts.line_chart(series, "Return ratios by fiscal year", charts.pct1_fmt,
                                     categorical=True) if len(fy) >= 2 else None)
    top = "premium_earned" if insurer else ("nii" if lender else "revenue")
    rev_chart = None
    if len(fy) >= 2 and top in fy:
        vals = [g(r, top) / CRORE for _, r in fy.iterrows()]
        label = ("Net premium earned" if insurer else "Net interest income" if lender
                 else "Revenue")
        rev_chart = charts.bar_chart(cols, vals, f"{label} by fiscal year (Rs crore)")
    return ({"cols": cols, "rows": rows if len(fy) else []},
            {"cols": cols, "rows": rrows if len(fy) else []}, rev_chart, ratio_chart)


def _q_table(w: pd.DataFrame, lender: bool):
    q = w[w.period_type == "Q"].sort_values("period_end")
    last8 = q.tail(8)
    if last8.empty:
        return {"cols": [], "rows": []}
    fields = ([("Net interest income", "nii"), ("PAT", "pat")] if lender else
              [("Revenue", "revenue"), ("EBITDA", "ebitda"), ("PAT (owners)", "pat_owners")])
    rows = []
    for lab, c in fields:
        if c not in q:
            continue
        rows.append({"label": lab, "vals": [cr(v) for v in last8[c]]})
        yoy = []
        for _, r in last8.iterrows():
            prev = q[(q.period_end >= r.period_end - pd.DateOffset(days=380))
                     & (q.period_end <= r.period_end - pd.DateOffset(days=350))]
            pv = prev[c].iloc[0] if len(prev) else np.nan
            yoy.append(pct((r[c] - pv) / abs(pv)) if _ok(pv) and pv and _ok(r[c]) else "–")
        rows.append({"label": f"{lab} YoY", "vals": yoy})
    return {"cols": [q_label(p) for p in last8.period_end], "rows": rows}


def _valuation_sections(con, isin: str, m: dict, run_date):
    v = con.execute("SELECT method, scenario, value_per_share, assumptions FROM valuations "
                    "WHERE isin = ? AND run_date = ?", [isin, run_date]).df() if run_date else \
        pd.DataFrame(columns=["method", "scenario", "value_per_share", "assumptions"])
    v["a"] = v.assumptions.map(lambda s: json.loads(s) if s else {})
    out: dict = {"dcf": None, "ri": None, "sotp": None, "wacc": None, "multiples": [],
                 "dcf_skipped": None}
    order = ["bear", "base", "bull"]

    d = v[v.method == "dcf"]
    base = d[d.scenario == "base"]
    if len(base) and base.iloc[0].a.get("skipped"):
        out["dcf_skipped"] = "; ".join(base.iloc[0].a["skipped"])
    elif len(d):
        scen = []
        for s in order:
            r = d[d.scenario == s]
            if r.empty:
                continue
            a = r.iloc[0].a
            inp = a.get("inputs", {})
            scen.append({"name": s, "vals": {
                "value": num(r.iloc[0].value_per_share), "g1": pct(inp.get("g1")),
                "gT": pct(a.get("terminal_growth")), "m0": pct(inp.get("ebitda_margin")),
                "mT": pct(inp.get("target_margin")), "capex": pct(inp.get("capex_pct")),
                "nwc": pct(inp.get("nwc_pct")), "tax": pct(inp.get("tax_rate")),
                "wacc": pct(a.get("wacc", {}).get("wacc")),
                "tv": pct(a.get("pv_terminal_share"))}})
        a0 = base.iloc[0].a if len(base) else {}
        grid = a0.get("sensitivity")
        grid_ctx = None
        if grid:
            n = len(grid["wacc"])
            grid_ctx = {"g": [pct(g) for g in grid["terminal_growth"]], "rows": [
                {"w": pct(w), "cells": [{"v": num(c), "base": i == n // 2 and j == n // 2}
                                        for j, c in enumerate(row)]}
                for i, (w, row) in enumerate(zip(grid["wacc"], grid["value_per_share"],
                                                 strict=False))]}
        out["dcf"] = {
            "scenarios": scen,
            "rows": [("Value per share (Rs)", "value"), ("Year-1 revenue growth", "g1"),
                     ("Terminal growth", "gT"), ("EBITDA margin today", "m0"),
                     ("Long-run EBITDA margin", "mT"), ("Capex % revenue (today)", "capex"),
                     ("Working capital % revenue", "nwc"), ("Tax rate", "tax"),
                     ("WACC", "wacc"), ("Terminal value share of EV", "tv")],
            "reverse": pct(a0.get("reverse_dcf_growth")) if a0.get("reverse_dcf_growth")
            is not None else None,
            "notes": a0.get("notes", []) + a0.get("wacc", {}).get("notes", []),
            "grid": grid_ctx}
        w = a0.get("wacc", {})
        if w:
            out["wacc"] = [("Cost of equity", pct(w.get("cost_of_equity"))),
                           ("Beta used (Blume-adjusted, bounded)", num(w.get("beta_used"), 2)),
                           ("Pre-tax cost of debt", pct(w.get("cost_of_debt_pre_tax"))),
                           ("Equity weight", pct(w.get("weight_equity"))),
                           ("Debt weight", pct(w.get("weight_debt"))),
                           ("WACC", pct(w.get("wacc")))]

    ri = v[v.method == "residual_income"]
    if len(ri) and not ri.iloc[0].a.get("skipped"):
        scen = []
        for s in order:
            r = ri[ri.scenario == s]
            if r.empty:
                continue
            a = r.iloc[0].a
            scen.append({"name": s, "vals": {
                "value": num(r.iloc[0].value_per_share), "roe0": pct(a.get("roe_start")),
                "roeT": pct(a.get("roe_long_run")), "ke": pct(a.get("cost_of_equity")),
                "payout": pct(a.get("payout")), "gT": pct(a.get("terminal_growth")),
                "pb": mult(a.get("implied_pb"))}})
        out["ri"] = {"scenarios": scen, "rows": [
            ("Value per share (Rs)", "value"), ("ROE, year 1", "roe0"),
            ("ROE, long run (Ke + spread)", "roeT"), ("Cost of equity", "ke"),
            ("Payout ratio", "payout"), ("Terminal growth", "gT"),
            ("Implied price / book", "pb")]}
        if out["wacc"] is None:
            a = ri.iloc[0].a
            out["wacc"] = [("Cost of equity", pct(a.get("cost_of_equity"))),
                           ("Beta used (Blume-adjusted, bounded)", num(a.get("beta_used"), 2))]

    so = v[v.method == "sotp"]
    if len(so):
        a = so.iloc[0].a
        out["sotp"] = {"missing": a.get("missing_inputs", []),
                       "parts": [{"name": p["name"], "type": p["type"],
                                  "value": f"Rs {cr(p['value'])} cr"} for p in a.get("parts", [])]}

    cur = {"pe": m.get("pe_ttm"), "pb": m.get("pb"), "ev_ebitda": m.get("ev_ebitda_ttm"),
           "ev_sales": m.get("ev_sales_ttm")}
    for name, label in (("pe", "P/E (TTM)"), ("pb", "P/B"), ("ev_ebitda", "EV/EBITDA (TTM)"),
                        ("ev_sales", "EV/Sales (TTM)")):
        b = v[v.method == f"{name}_band"]
        p = v[v.method == f"{name}_peers"]
        if b.empty and p.empty:
            continue
        band = b.iloc[0].a.get("band", {}) if len(b) else {}
        out["multiples"].append({
            "name": label, "current": mult(cur.get(name)), "low": mult(band.get("low")),
            "mid": mult(band.get("mid")), "high": mult(band.get("high")),
            "peer": mult(p.iloc[0].a.get("multiple")) if len(p) else "–"})
    return out


def build_context(con: duckdb.DuckDBPyConnection, symbol: str, val_cfg: ValuationConfig,
                  lm: pd.DataFrame | None = None) -> dict:
    s = con.execute("SELECT isin, symbol, name, industry, valuation_model, short_history "
                    "FROM securities WHERE symbol = ?", [symbol]).df()
    if s.empty:
        raise ValueError(f"{symbol} is not in the universe")
    s = s.iloc[0].to_dict()
    isin, lender = s["isin"], s["valuation_model"] in LENDER_MODELS
    s["valuation_model_label"] = MODEL_LABELS.get(s["valuation_model"], s["valuation_model"])
    lm = latest_metrics(con) if lm is None else lm
    m = lm[lm["isin"] == isin].iloc[0].to_dict() if len(lm) and isin in set(lm["isin"]) else {}
    as_of = con.execute("SELECT max(as_of) FROM metrics").fetchone()[0]
    run_date = con.execute("SELECT max(run_date) FROM valuations WHERE isin = ?",
                           [isin]).fetchone()[0]

    tiles = [("Price (Rs)", num(m.get("price"), 1)),
             ("Market cap (Rs cr)", cr(m.get("market_cap"))),
             ("P/E (TTM)", mult(m.get("pe_ttm"))), ("P/B", mult(m.get("pb"))),
             ("ROE", pct(m.get("roe")))]
    if lender:
        tiles += [("Gross NPA", pp(m.get("gross_npa_pct"))), ("NIM (approx.)",
                                                              pct(m.get("nim_approx")))]
    else:
        tiles += [("EV/EBITDA", mult(m.get("ev_ebitda_ttm"))), ("ROCE", pct(m.get("roce")))]
    tiles.append(("Dividend yield", pct(m.get("dividend_yield"))))

    flags = con.execute("SELECT flag, triggered, note FROM quality_flags WHERE isin = ? AND "
                        "as_of = ?", [isin, as_of]).df()
    flags_on = [{"label": FLAG_LABELS.get(r.flag, r.flag), "note": r.note}
                for r in flags.itertuples(index=False)
                if pd.notna(r.triggered) and bool(r.triggered)]

    w = (fundamentals(con, ("Q", "FY", "BS"), as_of=as_of, isins=[isin]) if as_of
         else pd.DataFrame())
    insurer = s["valuation_model"] == "insurance"
    fy_t, ratio_t, rev_chart, ratio_chart = (_fy_tables(w, lender, insurer) if len(w) else
                                             ({"rows": []}, {"rows": []}, None, None))
    q_t = _q_table(w, lender) if len(w) else {"rows": []}

    balance = []
    if insurer:
        balance = [("Net worth (Rs cr)", cr(m.get("bs_equity_owners"))),
                   ("Investments (Rs cr)", cr(m.get("bs_investments")))]
    elif lender:
        balance = [("Advances (Rs cr)", cr(m.get("bs_advances"))),
                   ("Deposits (Rs cr)", cr(m.get("bs_deposits"))),
                   ("Net worth (Rs cr)", cr(m.get("bs_equity_owners"))),
                   ("Credit cost (provisions / avg advances)", pct(m.get("credit_cost"), 2)),
                   ("Cost to income", pct(m.get("cost_to_income"))),
                   ("Provision coverage", pct(m.get("provision_coverage"))),
                   ("CET1 ratio", pp(m.get("cet1_ratio")))]
    else:
        balance = [("Equity to owners (Rs cr)", cr(m.get("bs_equity_owners"))),
                   ("Debt (Rs cr)", cr(m.get("bs_debt"))),
                   ("Cash and liquid investments (Rs cr)", cr(m.get("bs_cash_like"))),
                   ("Net debt / TTM EBITDA", mult(m.get("net_debt_to_ebitda"))),
                   ("Interest cover (EBIT / finance cost)", mult(m.get("interest_cover"))),
                   ("Receivable days", num(m.get("receivable_days"))),
                   ("Inventory days", num(m.get("inventory_days"))),
                   ("Payable days", num(m.get("payable_days"))),
                   ("Operating cash flow / PAT (3 years)", mult(m.get("cfo_to_pat_3y")))]

    shp = con.execute("SELECT quarter_end, category, pct, pledged_pct FROM shareholding WHERE "
                      "isin = ? ORDER BY quarter_end DESC", [isin]).df()
    shp_rows = []
    for q, g in list(shp.groupby("quarter_end", sort=False))[:8]:
        d = g.set_index("category")
        shp_rows.append((pd.Timestamp(q).strftime("%b %Y"),
                         pp(d.pct.get("promoter")), pp(d.pct.get("public")),
                         pp(d.pledged_pct.get("promoter"))))

    val = _valuation_sections(con, isin, m, run_date)
    ff = football_field(con, isin, run_date) if run_date else pd.DataFrame()
    football = (charts.football_chart(ff, m.get("price"), METHOD_LABELS) if len(ff) else None)
    ff_table = [{"label": METHOD_LABELS.get(r.method, r.method), "low": num(r.low),
                 "mid": num(r.mid), "high": num(r.high)} for r in ff.itertuples(index=False)]

    px = con.execute("SELECT date, adj_close FROM prices_adjusted WHERE security_id = ? AND "
                     "date >= ? - INTERVAL 3 YEAR AND date <= ? ORDER BY date",
                     [isin, as_of, as_of]).df() if as_of else pd.DataFrame()
    price_chart = None
    if len(px) > 20:
        ps = px.set_index(pd.to_datetime(px.date))["adj_close"]
        ix = con.execute("SELECT date, close FROM index_prices_daily WHERE index_name = ? AND "
                         "date >= ? AND date <= ? ORDER BY date",
                         [SMALL_INDEX, px.date.min(), as_of]).df()
        series = {s["symbol"]: ps / ps.iloc[0] * 100}
        if len(ix):
            isr = ix.set_index(pd.to_datetime(ix.date))["close"]
            series["Nifty Smallcap 100"] = isr / isr.iloc[0] * 100
        price_chart = charts.line_chart(series, "Price vs index, rebased to 100 (3 years)")

    risk = [("Beta vs Nifty Smallcap 100 (raw / adjusted)",
             f"{num(m.get('beta_raw'), 2)} / {num(m.get('beta_adj'), 2)}"),
            ("Beta vs Nifty 50 (raw)", num(m.get("beta_nifty50_raw"), 2)),
            ("Return 1 year", pct(m.get("ret_1y"))),
            ("Return 3 years (a year)", pct(m.get("ret_3y_cagr"))),
            ("Return 5 years (a year)", pct(m.get("ret_5y_cagr"))),
            ("Volatility (1 year, annualised)", pct(m.get("vol_1y"))),
            ("Max drawdown (3 years)", pct(m.get("max_drawdown_3y"))),
            ("52-week range (Rs)", f"{num(m.get('low_52w'))} – {num(m.get('high_52w'))}"),
            ("Median daily traded value, 6 months (Rs cr)",
             num(m.get("median_traded_value_6m_cr"), 1))]

    notes = _data_notes(con, isin, s, m)
    peers = [r[0] for r in con.execute("SELECT peer_symbol FROM peers WHERE isin = ? "
                                       "ORDER BY peer_symbol", [isin]).fetchall()]
    overview_path = CONFIG_DIR / "overviews" / f"{symbol}.md"
    overview = ([p.strip() for p in overview_path.read_text().split("\n\n") if p.strip()]
                if overview_path.exists() else [])
    return {
        "s": s, "as_of": as_of, "tiles": [{"k": k, "v": v} for k, v in tiles],
        "flags_on": flags_on, "overview": overview, "fy_table": fy_t, "q_table": q_t,
        "ratio_table": ratio_t, "balance": balance, "shp_table": {"rows": shp_rows},
        "ff_table": ff_table, "price_fmt": num(m.get("price"), 1), "risk": risk,
        "data_notes": notes, "peers": peers, "band_std": val_cfg.multiples.band_std,
        "band_years": val_cfg.multiples.history_years,
        "charts": {"revenue": rev_chart, "ratios": ratio_chart, "football": football,
                   "price": price_chart},
        "disclaimer": DISCLAIMER, "disclaimer_long": DISCLAIMER_LONG,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), **val,
    }


def _data_notes(con, isin: str, s: dict, m: dict) -> list[str]:
    notes = ["Prices: NSE bhavcopy, adjusted for splits, bonuses and demergers; adjusted close "
             "is a price series (dividends not reinvested)."]
    f = con.execute("SELECT min(period_end), max(period_end), max(filed_at), count(*) FROM "
                    "filings WHERE isin = ? AND status = 'parsed'", [isin]).fetchone()
    if f and f[3]:
        notes.append(f"Fundamentals: {f[3]} XBRL filings covering {f[0]} to {f[1]}; latest filed "
                     f"{str(f[2])[:16]}. Machine-readable results start in 2018.")
    else:
        notes.append("Fundamentals: no parsed XBRL filings for this stock.")
    r = con.execute("SELECT count(*) FROM financials WHERE isin = ? AND is_restated",
                    [isin]).fetchone()[0]
    if r:
        notes.append(f"{r} reported values were later revised; the latest filed value is used.")
    ev = con.execute("SELECT ex_date, source, factor, note FROM price_events WHERE security_id "
                     "= ? AND source IN ('inferred_isin_change', 'demerger_approx', 'manual') "
                     "ORDER BY ex_date", [isin]).fetchall()
    for d, src, fac, note in ev:
        what = {"inferred_isin_change": "split inferred from an ISIN change",
                "demerger_approx": "demerger, factor approximated from the price drop",
                "manual": f"manual adjustment ({note})"}[src]
        notes.append(f"Price history: {what} on {d} (factor {fac:.4f}).")
    if s.get("short_history"):
        notes.append("Listed or restructured recently: fewer years of data than most stocks.")
    if not _ok(m.get("beta_raw")):
        notes.append("Beta could not be estimated (under a year of weekly prices); 1.0 is used.")
    if "ttm_source_quarters" in m and m["ttm_source_quarters"] == 0:
        notes.append("Four consecutive quarters were not available: TTM figures use the latest "
                     "fiscal year.")
    notes.append("FII / DII / mutual fund holdings, auditor changes and contingent liabilities "
                 "are not yet extracted (v1 gap).")
    return notes


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["j2"]))


def render_report(ctx: dict) -> str:
    env = _env()
    return env.get_template("report.html.j2").render(css=(TEMPLATES / "base.css").read_text(),
                                                     **ctx)


def render_index(con, out_rows: list[dict], as_of, repo_url: str,
                 has_research: bool = False) -> str:
    cols = [("Symbol", "str"), ("Company", "str"), ("Industry", "str"), ("Path", "str"),
            ("Price", "num"), ("Mkt cap (Rs cr)", "num"), ("P/E", "num"), ("P/B", "num"),
            ("EV/EBITDA", "num"), ("ROE", "num"), ("1y return", "num"), ("Flags", "num")]
    env = _env()
    return env.get_template("index.html.j2").render(
        css=(TEMPLATES / "base.css").read_text(), cols=[{"label": c, "type": t} for c, t in cols],
        rows=out_rows, n=len(out_rows), as_of=as_of, repo_url=repo_url,
        has_research=has_research,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"), disclaimer=DISCLAIMER)


def index_row(s: dict, m: dict, n_flags: int) -> list[dict]:
    def cell(html, sort):
        return {"html": html, "sort": sort if sort is not None else ""}

    def n(v):
        return v if _ok(v) else None
    return [
        cell(f'<a href="{s["symbol"]}.html">{s["symbol"]}</a>', s["symbol"]),
        cell(s["name"], s["name"]), cell(s["industry"] or "", s["industry"] or ""),
        cell(MODEL_LABELS.get(s["valuation_model"], s["valuation_model"]), s["valuation_model"]),
        cell(num(m.get("price"), 1), n(m.get("price"))),
        cell(cr(m.get("market_cap")), n(m.get("market_cap"))),
        cell(mult(m.get("pe_ttm")), n(m.get("pe_ttm"))), cell(mult(m.get("pb")), n(m.get("pb"))),
        cell(mult(m.get("ev_ebitda_ttm")), n(m.get("ev_ebitda_ttm"))),
        cell(pct(m.get("roe")), n(m.get("roe"))), cell(pct(m.get("ret_1y")), n(m.get("ret_1y"))),
        cell(str(n_flags), n_flags)]


def build_reports(con: duckdb.DuckDBPyConnection, out_dir: Path, val_cfg: ValuationConfig,
                  symbols: list[str] | None = None, repo_url: str = "") -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    uni = con.execute("SELECT isin, symbol, name, industry, valuation_model FROM securities "
                      "WHERE in_index ORDER BY symbol").df()
    todo = uni if not symbols else uni[uni.symbol.isin(symbols)]
    lm = latest_metrics(con)
    as_of = con.execute("SELECT max(as_of) FROM metrics").fetchone()[0]
    flag_counts = dict(con.execute(
        "SELECT isin, count(*) FILTER (WHERE triggered) FROM quality_flags WHERE as_of = ? "
        "GROUP BY 1", [as_of]).fetchall()) if as_of else {}
    stats = {"reports": 0, "errors": 0}
    errors = []
    for r in todo.itertuples(index=False):
        try:
            html = render_report(build_context(con, r.symbol, val_cfg, lm))
            (out_dir / f"{r.symbol}.html").write_text(html, encoding="utf-8")
            stats["reports"] += 1
        except Exception as e:  # one bad stock must not stop the site
            stats["errors"] += 1
            errors.append(f"{r.symbol}: {type(e).__name__}: {e}")
    if not symbols:
        rows = []
        for r in uni.itertuples(index=False):
            m = lm[lm["isin"] == r.isin].iloc[0].to_dict() if len(lm) and r.isin in set(
                lm["isin"]) else {}
            rows.append(index_row(r._asdict(), m, flag_counts.get(r.isin, 0)))
        (out_dir / "index.html").write_text(
            render_index(con, rows, as_of, repo_url, (out_dir / "research.html").exists()),
            encoding="utf-8")
    if errors:
        (out_dir / "_errors.txt").write_text("\n".join(errors), encoding="utf-8")
    return stats


def report_date(con) -> date | None:
    return con.execute("SELECT max(as_of) FROM metrics").fetchone()[0]
