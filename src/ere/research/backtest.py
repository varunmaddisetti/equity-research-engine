"""M7: do valuation signals predict returns? A point-in-time backtest.

Question: at each month-end, if stocks are ranked by how cheap they look (using only prices and
results that were public on that date), do the cheaper ones outperform over the next 1, 3 and
12 months?

Signals (higher = cheaper):
  earnings_yield   TTM PAT to owners / market cap              (1 / P/E)
  book_to_price    book value / market cap                     (1 / P/B)
  ebitda_yield     TTM EBITDA / enterprise value               (1 / EV/EBITDA)
  band_discount    log(own trailing-36-month median multiple / current multiple), using each
                   stock's primary multiple (EV/EBITDA for operating companies, P/B for lenders
                   and holdcos, EV/Sales for pre-profit): cheap relative to its own history

Evaluation:
  rank IC          Spearman correlation between signal and forward return, each month
  quintiles        equal-weight portfolios by signal quintile; Q5 (cheapest) minus Q1
  Forward returns are measured from the month-end close to the close h months later, so the
  signal never sees its own outcome. Returns are also shown in excess of the Nifty Smallcap 100.

Honest limitations, printed with every result:
  * Survivorship bias: the universe is TODAY's index members. Stocks that fell out of the index
    (often after doing badly) are missing, which flatters every strategy.
  * Short sample: XBRL fundamentals start in 2018, so roughly 6-7 years of monthly signals;
    12-month IC observations overlap, so their t-statistics overstate significance.
  * Price returns only (dividends excluded); no transaction costs or liquidity limits.
This is research into a hypothesis, not a trading recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from ere.valuation.multiples import multiple_history

HORIZONS = (1, 3, 12)
PRIMARY_MULTIPLE = {"dcf": "ev_ebitda", "residual_income": "pb", "insurance": "pb",
                    "sotp": "pb", "ev_sales": "ev_sales"}
SIGNALS = ("earnings_yield", "book_to_price", "ebitda_yield", "band_discount")
BAND_MONTHS = 36
MIN_BAND_POINTS = 12
MIN_STOCKS = 20
LIMITATIONS = [
    "Survivorship bias: universe = current index members; past exits are missing.",
    "Fundamentals start in 2018: short sample; 12-month ICs overlap, t-stats overstated.",
    "Price returns only; no dividends, transaction costs or liquidity limits.",
]


@dataclass
class SignalStats:
    signal: str
    horizon: int
    months: int
    mean_ic: float
    ic_t: float
    ic_hit_rate: float
    q5_minus_q1_monthly: float | None   # only for the 1-month horizon
    q5_minus_q1_ann: float | None


def add_signals(hist: pd.DataFrame, models: dict[str, str]) -> pd.DataFrame:
    """hist: isin, date, pe, pb, ev_ebitda, ev_sales (point-in-time monthly multiples)."""
    h = hist.sort_values(["isin", "date"]).copy()
    for inv, col in (("earnings_yield", "pe"), ("book_to_price", "pb"),
                     ("ebitda_yield", "ev_ebitda")):
        h[inv] = 1.0 / h[col] if col in h else np.nan
    h["band_discount"] = np.nan
    for isin, g in h.groupby("isin"):
        col = PRIMARY_MULTIPLE.get(models.get(isin, "dcf"), "ev_ebitda")
        if col not in g:
            continue
        x = g[col]
        # median of the PREVIOUS months only (shift(1)): no look-ahead
        med = x.shift(1).rolling(BAND_MONTHS, min_periods=MIN_BAND_POINTS).median()
        h.loc[g.index, "band_discount"] = np.log(med / x)
    return h


def forward_returns(px: pd.DataFrame, dates: pd.DatetimeIndex, horizons=HORIZONS
                    ) -> pd.DataFrame:
    """px: isin, date, adj_close (daily). Returns isin, date, fwd_{h}m for each month-end in
    dates, using the last close on/before each date and on/before date + h months."""
    px = px.sort_values("date")
    wide = px.pivot_table(index="date", columns="isin", values="adj_close")
    wide.index = pd.to_datetime(wide.index)
    last_day = wide.index.max()
    out = []
    for d in dates:
        base = wide.loc[:d].tail(1)
        if base.empty or (d - base.index[0]).days > 10:
            continue
        rec = pd.DataFrame({"isin": wide.columns, "date": d})
        for hmo in horizons:
            end = d + pd.DateOffset(months=hmo)
            if end > last_day:
                rec[f"fwd_{hmo}m"] = np.nan
                continue
            fut = wide.loc[:end].tail(1)
            rec[f"fwd_{hmo}m"] = (fut.values[0] / base.values[0]) - 1
        out.append(rec)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(
        columns=["isin", "date", *[f"fwd_{h}m" for h in horizons]])


def index_forward(idx: pd.Series, dates, horizons=HORIZONS) -> pd.DataFrame:
    rows = []
    for d in dates:
        base = idx.loc[:d].tail(1)
        if base.empty:
            continue
        r = {"date": d}
        for hmo in horizons:
            end = d + pd.DateOffset(months=hmo)
            r[f"idx_{hmo}m"] = (idx.loc[:end].iloc[-1] / base.iloc[0] - 1
                                if end <= idx.index.max() else np.nan)
        rows.append(r)
    return pd.DataFrame(rows)


def rank_ic(panel: pd.DataFrame, signal: str, ret: str, min_stocks: int = MIN_STOCKS
            ) -> pd.Series:
    """Monthly Spearman rank correlation between signal and forward return."""
    out = {}
    for d, g in panel.groupby("date"):
        g = g[[signal, ret]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(g) >= min_stocks:
            out[d] = g[signal].rank().corr(g[ret].rank())
    return pd.Series(out, dtype=float).sort_index()


def quintile_returns(panel: pd.DataFrame, signal: str, ret: str, min_stocks: int = MIN_STOCKS
                     ) -> pd.DataFrame:
    """Equal-weight mean forward return by signal quintile (1 = most expensive, 5 = cheapest)."""
    rows = []
    for d, g in panel.groupby("date"):
        g = g[[signal, ret]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(g) < min_stocks:
            continue
        q = pd.qcut(g[signal].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
        m = g.groupby(q, observed=True)[ret].mean()
        rows.append({"date": d, **{f"Q{int(k)}": v for k, v in m.items()}})
    return pd.DataFrame(rows).set_index("date").sort_index() if rows else pd.DataFrame()


def summarise(panel: pd.DataFrame, excess: bool = True) -> tuple[list[SignalStats], dict]:
    stats, series = [], {}
    for sig in SIGNALS:
        if sig not in panel:
            continue
        for hmo in HORIZONS:
            col = f"xs_{hmo}m" if excess else f"fwd_{hmo}m"
            if col not in panel:
                continue
            ic = rank_ic(panel, sig, col)
            if ic.empty:
                continue
            t = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))) if len(ic) > 2 else np.nan
            q51 = q51a = None
            if hmo == 1:
                q = quintile_returns(panel, sig, col)
                if len(q) and {"Q1", "Q5"} <= set(q.columns):
                    spread = (q["Q5"] - q["Q1"]).dropna()
                    q51 = float(spread.mean())
                    q51a = float((1 + spread).prod() ** (12 / len(spread)) - 1)
                    series[f"{sig}_quintiles"] = q
            series[f"{sig}_ic_{hmo}m"] = ic
            stats.append(SignalStats(sig, hmo, len(ic), float(ic.mean()), float(t),
                                     float((ic > 0).mean()), q51, q51a))
    return stats, series


def build_panel(con: duckdb.DuckDBPyConnection, as_of: date | None = None, years: int = 8
                ) -> pd.DataFrame:
    if as_of is None:
        as_of = con.execute("SELECT max(date) FROM prices_adjusted").fetchone()[0]
    uni = con.execute("SELECT isin, valuation_model FROM securities WHERE in_index").df()
    models = dict(zip(uni["isin"], uni["valuation_model"], strict=False))
    hist = multiple_history(con, list(uni["isin"]), as_of, years)
    if hist.empty:
        return hist
    panel = add_signals(hist, models)
    dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    px = con.execute("SELECT security_id AS isin, date, adj_close FROM prices_adjusted "
                     "WHERE date <= ?", [as_of]).df()
    fwd = forward_returns(px, dates)
    panel = panel.merge(fwd, on=["isin", "date"], how="left")
    ix = con.execute("SELECT date, close FROM index_prices_daily WHERE index_name = "
                     "'NIFTY SMALLCAP 100' AND date <= ? ORDER BY date", [as_of]).df()
    if len(ix):
        s = ix.set_index(pd.to_datetime(ix["date"]))["close"]
        panel = panel.merge(index_forward(s, dates), on="date", how="left")
        for hmo in HORIZONS:
            panel[f"xs_{hmo}m"] = panel[f"fwd_{hmo}m"] - panel[f"idx_{hmo}m"]
    else:
        for hmo in HORIZONS:
            panel[f"xs_{hmo}m"] = panel[f"fwd_{hmo}m"]
    return panel
