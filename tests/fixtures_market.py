"""A small synthetic market written straight into the database, for analytics/valuation tests.

Four operating companies in one industry (enough for a 3-peer median) and one bank.
Prices: 3 years of business days, stock log-returns = beta x index returns + noise.
Financials: 12 quarters, 3 fiscal years, 3 balance sheets, filed 45 days after period end.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CR = 1e7
STOCKS = [  # symbol, isin, beta, revenue per quarter (cr), model
    ("ALPHA", "INE00AA01011", 1.5, 100.0, "dcf"),
    ("BETA", "INE00BB01011", 0.8, 200.0, "dcf"),
    ("GAMMA", "INE00CC01011", 1.0, 50.0, "dcf"),
    ("DELTA", "INE00DD01011", 1.2, 80.0, "dcf"),
]
BANK = ("BANKX", "INE00KK01011", 1.1, "residual_income")
END = pd.Timestamp("2026-09-24")


def _dates():
    return pd.bdate_range(END - pd.DateOffset(years=3), END)


def load_market(con, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    d = _dates()
    idx_r = rng.normal(0.0004, 0.012, len(d))
    small = 15000 * np.exp(np.cumsum(idx_r))
    large = 22000 * np.exp(np.cumsum(idx_r * 0.7))
    idx = pd.concat([
        pd.DataFrame({"index_name": "NIFTY SMALLCAP 100", "date": d, "close": small}),
        pd.DataFrame({"index_name": "NIFTY 50", "date": d, "close": large}),
    ])
    idx["source"] = "test"
    con.register("_i", idx)
    con.execute("INSERT INTO index_prices_daily (index_name, date, close, source) "
                "SELECT index_name, date, close, source FROM _i")
    con.unregister("_i")

    secs, px_rows, fin = [], [], []
    for sym, isin, b, rev_q, model in STOCKS + [(BANK[0], BANK[1], BANK[2], 0.0, BANK[3])]:
        secs.append((isin, sym, f"{sym} Ltd", "Banks" if model != "dcf" else "Widgets",
                     model, False))
        r = b * idx_r + rng.normal(0, 0.01, len(d))
        close = 500 * np.exp(np.cumsum(r))
        px_rows.append(pd.DataFrame({
            "security_id": isin, "date": d, "isin": isin, "symbol": sym, "close": close,
            "adj_factor": 1.0, "adj_close": close, "adj_volume": 1e5,
            "traded_value": close * 1e5 * (0.5 if sym == "GAMMA" else 5.0),
            "ret_1d": pd.Series(close).pct_change().values}))
        fin += _financials(sym, isin, rev_q, model)

    s = pd.DataFrame(secs, columns=["isin", "symbol", "name", "industry", "valuation_model",
                                    "short_history"])
    con.register("_s", s)
    con.execute("INSERT INTO securities (isin, symbol, name, industry, valuation_model, "
                "short_history) SELECT * FROM _s")
    con.unregister("_s")
    px = pd.concat(px_rows, ignore_index=True)
    con.register("_p", px)
    con.execute("INSERT INTO prices_adjusted SELECT security_id, date, isin, symbol, close, "
                "adj_factor, adj_close, adj_volume, traded_value, ret_1d FROM _p")
    con.execute("INSERT INTO security_master SELECT DISTINCT isin, isin, symbol, min(date) "
                "OVER (PARTITION BY isin), max(date) OVER (PARTITION BY isin) FROM _p")
    con.unregister("_p")
    f = pd.DataFrame(fin, columns=["isin", "symbol", "period_end", "period_type", "basis",
                                   "field", "value"])
    f["unit"] = "INR"
    f["filing_date"] = f["period_end"] + pd.Timedelta(days=45)
    f["is_restated"] = False
    f["source"] = "test"
    con.register("_f", f)
    con.execute("INSERT INTO financials (isin, symbol, period_end, period_type, basis, field, "
                "value, unit, filing_date, is_restated, source) SELECT isin, symbol, period_end,"
                " period_type, basis, field, value, unit, filing_date, is_restated, source "
                "FROM _f")
    con.unregister("_f")


def _financials(sym, isin, rev_q, model):
    rows = []
    q_ends = pd.date_range(end="2026-06-30", periods=12, freq="QE")
    fy_ends = [pd.Timestamp(f"{y}-03-31") for y in (2024, 2025, 2026)]

    def add(pe, pt, field, v):
        rows.append((isin, sym, pe, pt, "consolidated", field, float(v)))

    if model == "dcf":
        for i, qe in enumerate(q_ends):
            rev = rev_q * CR * (1.03 ** i)
            _pl(add, qe, "Q", rev)
            add(qe, "Q", "paid_up_capital", 10 * CR)
            add(qe, "Q", "face_value", 10)
        for j, fe in enumerate(fy_ends):
            rev = rev_q * 4 * CR * (1.12 ** j)
            _pl(add, fe, "FY", rev)
            add(fe, "FY", "cfo", rev * 0.12)
            add(fe, "FY", "capex_ppe", rev * 0.05)
            add(fe, "FY", "paid_up_capital", 10 * CR)
            add(fe, "FY", "face_value", 10)
            for f, v in (("equity_owners", rev * 0.8), ("equity_total", rev * 0.8),
                         ("borrowings_noncurrent", rev * 0.2), ("borrowings_current", rev * 0.1),
                         ("cash", rev * 0.05), ("trade_receivables", rev * 0.15 * (1 + j * 0.1)),
                         ("inventories", rev * 0.1), ("trade_payables", rev * 0.08),
                         ("total_assets", rev * 1.3)):
                add(fe, "BS", f, v)
    else:
        for j, fe in enumerate(fy_ends):
            g = 1.15 ** j
            for f, v in (("interest_earned", 2000 * CR * g), ("interest_expended", 1200 * CR * g),
                         ("other_income", 200 * CR * g), ("operating_expenses", 450 * CR * g),
                         ("provisions", 100 * CR * g), ("pbt", 450 * CR * g),
                         ("pat", 340 * CR * g), ("paid_up_capital", 100 * CR),
                         ("face_value", 2), ("gross_npa_pct", 2.0), ("net_npa_pct", 0.6),
                         ("gross_npa", 600 * CR), ("net_npa", 180 * CR)):
                add(fe, "FY", f, v)
            for f, v in (("advances", 20000 * CR * g), ("deposits", 25000 * CR * g),
                         ("investments", 6000 * CR * g), ("capital", 100 * CR),
                         ("reserves", 2400 * CR * g)):
                add(fe, "BS", f, v)
    return rows


def _pl(add, pe, pt, rev):
    oth, fin, dep = rev * 0.01, rev * 0.02, rev * 0.04
    pbt = rev * 0.14                     # EBITDA = pbt + fin + dep - oth = 19% of revenue
    tax = pbt * 0.25
    for f, v in (("revenue", rev), ("other_income", oth), ("finance_cost", fin),
                 ("depreciation", dep), ("pbt", pbt), ("tax", tax), ("pat", pbt - tax),
                 ("pat_owners", pbt - tax), ("total_income", rev + oth),
                 ("pat_continuing", pbt - tax)):
        add(pe, pt, f, v)
