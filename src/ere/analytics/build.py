"""Build the analytics snapshot for every stock in the universe: `ere build analytics`.

For each stock, as of the last trading day in the data:
  risk & liquidity (prices), ratios (fundamentals as known on that day), market multiples,
  dividend yield, shareholding trend, quality flags, peer set.
Results go to the long tables `metrics`, `quality_flags` and `peers`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from ere.analytics.quality import evaluate_flags
from ere.analytics.ratios import company_ratios
from ere.analytics.risk import risk_metrics
from ere.clean.financials import fundamentals
from ere.config import UniverseConfig, ValuationConfig

SMALL_INDEX = "NIFTY SMALLCAP 100"
LARGE_INDEX = "NIFTY 50"
LENDER_MODELS = ("residual_income", "insurance")


@dataclass
class AnalyticsStats:
    as_of: date | None = None
    securities: int = 0
    metrics: int = 0
    flags_triggered: int = 0
    no_prices: int = 0
    no_fundamentals: int = 0


def _index_series(con, name: str, as_of) -> pd.Series:
    df = con.execute("SELECT date, close FROM index_prices_daily WHERE index_name = ? "
                     "AND date <= ? ORDER BY date", [name, as_of]).df()
    return df.set_index(pd.to_datetime(df["date"]))["close"] if len(df) else pd.Series(
        dtype=float)


def market_metrics(m: dict, is_lender: bool) -> dict[str, float]:
    out = {}
    price, shares = m.get("price", np.nan), m.get("shares", np.nan)
    mcap = price * shares if np.isfinite(price) and np.isfinite(shares) else np.nan
    out["market_cap"] = mcap
    pat = m.get("ttm_pat_owners", m.get("ttm_pat", np.nan))
    out["pe_ttm"] = mcap / pat if np.isfinite(mcap) and np.isfinite(pat) and pat > 0 else np.nan
    eq = m.get("bs_equity_owners", np.nan)
    out["pb"] = mcap / eq if np.isfinite(mcap) and np.isfinite(eq) and eq > 0 else np.nan
    if not is_lender:
        ev = mcap + m.get("bs_debt", 0.0) - m.get("bs_cash_like", 0.0)
        out["enterprise_value"] = ev
        e, r = m.get("ttm_ebitda", np.nan), m.get("ttm_revenue", np.nan)
        out["ev_ebitda_ttm"] = ev / e if np.isfinite(ev) and np.isfinite(e) and e > 0 else np.nan
        out["ev_sales_ttm"] = ev / r if np.isfinite(ev) and np.isfinite(r) and r > 0 else np.nan
    return {k: v for k, v in out.items() if np.isfinite(v)}


def dividend_yield(con, isin: str, as_of, price: float) -> float:
    """Trailing-12-month dividends per share (split-adjusted to today's shares) / price."""
    df = con.execute(
        """
        SELECT c.ex_date, c.amount, a.adj_factor
        FROM corp_actions c
        JOIN security_master m ON m.isin = c.isin
        LEFT JOIN prices_adjusted a ON a.security_id = m.security_id AND a.date = c.ex_date
        WHERE m.security_id = ? AND c.action = 'dividend' AND c.amount IS NOT NULL
          AND c.ex_date > ? - INTERVAL 365 DAY AND c.ex_date <= ?
        """, [isin, as_of, as_of]).df()
    if df.empty or not np.isfinite(price) or price <= 0:
        return 0.0 if np.isfinite(price) else np.nan
    return float((df.amount * df.adj_factor.fillna(1.0)).sum() / price)


def shareholding_metrics(con, isin: str, as_of) -> dict[str, float]:
    df = con.execute(
        "SELECT quarter_end, category, pct, pledged_pct FROM shareholding WHERE isin = ? "
        "AND quarter_end <= ? ORDER BY quarter_end", [isin, as_of]).df()
    out: dict[str, float] = {}
    prom = df[df.category == "promoter"].copy()
    if prom.empty:
        return out
    prom["quarter_end"] = pd.to_datetime(prom.quarter_end)
    last = prom.iloc[-1]
    out["promoter_pct"] = float(last.pct)
    if pd.notna(last.pledged_pct):
        out["pledged_pct"] = float(last.pledged_pct)
    yr = prom[prom.quarter_end <= last.quarter_end - pd.DateOffset(days=300)]
    if len(yr):
        out["promoter_change_1y_pp"] = float(last.pct - yr.iloc[-1].pct)
        if pd.notna(last.pledged_pct) and pd.notna(yr.iloc[-1].pledged_pct):
            out["pledged_change_1y_pp"] = float(last.pledged_pct - yr.iloc[-1].pledged_pct)
    return out


def peer_rows(uni: pd.DataFrame, cfg: UniverseConfig) -> pd.DataFrame:
    by_sym = uni.set_index("symbol")
    rows = []
    for r in uni.itertuples(index=False):
        if r.symbol in cfg.peer_overrides:
            for p in cfg.peer_overrides[r.symbol]:
                rows.append((r.isin, by_sym["isin"].get(p), p, "override"))
            continue
        same = uni[(uni.industry == r.industry) & (uni.symbol != r.symbol)]
        # Lenders compare with lenders, operating companies with operating companies.
        lender = r.valuation_model in LENDER_MODELS
        same = same[same.valuation_model.isin(LENDER_MODELS) == lender]
        for p in same.itertuples(index=False):
            rows.append((r.isin, p.isin, p.symbol, "industry"))
    return pd.DataFrame(rows, columns=["isin", "peer_isin", "peer_symbol", "source"])


def build_analytics(
    con: duckdb.DuckDBPyConnection,
    val_cfg: ValuationConfig,
    uni_cfg: UniverseConfig,
    as_of: date | None = None,
) -> AnalyticsStats:
    stats = AnalyticsStats()
    if as_of is None:
        row = con.execute("SELECT max(date) FROM prices_adjusted").fetchone()
        if not row or row[0] is None:
            raise ValueError("no adjusted prices - run `ere build prices` first")
        as_of = row[0]
    stats.as_of = as_of
    uni = con.execute("SELECT isin, symbol, industry, valuation_model, short_history "
                      "FROM securities WHERE in_index ORDER BY symbol").df()
    idx_s, idx_l = _index_series(con, SMALL_INDEX, as_of), _index_series(con, LARGE_INDEX, as_of)
    wide_all = fundamentals(con, ("Q", "FY", "BS"), as_of=as_of)
    weeks = val_cfg.cost_of_capital.beta_lookback_weeks

    metric_rows, flag_rows = [], []
    for r in uni.itertuples(index=False):
        is_lender = r.valuation_model in LENDER_MODELS
        px = con.execute("SELECT date, adj_close, traded_value FROM prices_adjusted "
                         "WHERE security_id = ? AND date <= ? ORDER BY date",
                         [r.isin, as_of]).df()
        m: dict[str, float] = {}
        if px.empty:
            stats.no_prices += 1
        else:
            px.index = pd.to_datetime(px["date"])
            m.update(risk_metrics(px["adj_close"], px["traded_value"], idx_s, idx_l, weeks))
        w = wide_all[wide_all["isin"] == r.isin] if len(wide_all) else pd.DataFrame()
        if w.empty:
            stats.no_fundamentals += 1
        m.update(company_ratios(w, is_lender))
        m.update(market_metrics(m, is_lender))
        m["dividend_yield"] = dividend_yield(con, r.isin, as_of, m.get("price", np.nan))
        m.update(shareholding_metrics(con, r.isin, as_of))
        for k, v in m.items():
            if v is not None and np.isfinite(v):
                metric_rows.append((r.isin, r.symbol, as_of, k, float(v)))
        for flag, trig, val, thr, note in evaluate_flags(m, val_cfg.quality_flags, is_lender,
                                                         bool(r.short_history)):
            flag_rows.append((r.isin, r.symbol, as_of, flag, trig,
                              val if np.isfinite(val) else None,
                              thr if np.isfinite(thr) else None, note))
            stats.flags_triggered += int(bool(trig))
        stats.securities += 1

    mdf = pd.DataFrame(metric_rows, columns=["isin", "symbol", "as_of", "metric", "value"])
    fdf = pd.DataFrame(flag_rows, columns=["isin", "symbol", "as_of", "flag", "triggered",
                                           "value", "threshold", "note"])
    con.execute("DELETE FROM metrics WHERE as_of = ?", [as_of])
    con.execute("DELETE FROM quality_flags WHERE as_of = ?", [as_of])
    for table, df in (("metrics", mdf), ("quality_flags", fdf)):
        if len(df):
            con.register("_d", df)
            con.execute(f"INSERT INTO {table} ({', '.join(df.columns)}) "
                        f"SELECT {', '.join(df.columns)} FROM _d")
            con.unregister("_d")
    peers = peer_rows(uni, uni_cfg)
    con.execute("DELETE FROM peers")
    if len(peers):
        con.register("_p", peers)
        con.execute("INSERT INTO peers SELECT * FROM _p")
        con.unregister("_p")
    stats.metrics = len(mdf)
    return stats


def latest_metrics(con, as_of: date | None = None) -> pd.DataFrame:
    """Wide table: one row per stock, one column per metric, for the latest (or given) date."""
    if as_of is None:
        as_of = con.execute("SELECT max(as_of) FROM metrics").fetchone()[0]
    df = con.execute("SELECT isin, symbol, metric, value FROM metrics WHERE as_of = ?",
                     [as_of]).df()
    if df.empty:
        return df
    return df.pivot_table(index=["isin", "symbol"], columns="metric", values="value").reset_index()
