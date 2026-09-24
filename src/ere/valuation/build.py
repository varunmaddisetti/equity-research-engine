"""Run every valuation method for every stock: `ere build valuation`.

Writes one row per (method, scenario) to `valuations`, with the full assumptions as JSON so
the report (and a reviewer) can see exactly how each number was produced.

Methods by valuation path (config/universe.yaml):
  dcf             DCF bear/base/bull (+ sensitivity grid, reverse DCF) | P/E, EV/EBITDA bands
                  and peer medians
  residual_income RI bear/base/bull | P/B and P/E bands, peer P/B
  insurance       same as residual_income
  sotp            SOTP (config/sotp.yaml) | P/B band
  ev_sales        EV/Sales band and peers | reverse DCF when EBITDA is positive
Every row is a value per share or a range edge. No method produces a target price or a view.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from ere.analytics.build import latest_metrics
from ere.clean.financials import fundamentals
from ere.config import UniverseConfig, ValuationConfig
from ere.valuation.dcf import (
    compute_wacc,
    dcf_inputs_from_metrics,
    reverse_dcf,
    run_dcf,
    sensitivity_grid,
)
from ere.valuation.multiples import (
    band,
    implied_value,
    multiple_history,
    peer_median,
    snapshot_fundamentals,
)
from ere.valuation.residual_income import payout_ratio, run_residual_income
from ere.valuation.sotp import load_sotp, run_sotp

METHODS_BY_MODEL = {
    "dcf": ("pe", "ev_ebitda"),
    "residual_income": ("pb", "pe"),
    "insurance": ("pb", "pe"),
    "sotp": ("pb",),
    "ev_sales": ("ev_sales",),
}
PEER_METRIC = {"pe": "pe_ttm", "pb": "pb", "ev_ebitda": "ev_ebitda_ttm",
               "ev_sales": "ev_sales_ttm"}
RI_ROE_SHIFT = {"bear": -0.02, "base": 0.0, "bull": 0.02}


@dataclass
class ValuationStats:
    run_date: date | None = None
    securities: int = 0
    rows: int = 0
    dcf_run: int = 0
    ri_run: int = 0
    bands: int = 0
    skipped_notes: int = 0


def _json(obj) -> str:
    def conv(o):
        if isinstance(o, (np.floating, float)):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (pd.Timestamp, date)):
            return o.isoformat()
        return str(o)
    return json.dumps(obj, default=conv, allow_nan=False)


def _clean(o):
    """Replace non-finite floats with None, recursively (JSON-safe)."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def build_valuation(
    con: duckdb.DuckDBPyConnection,
    val_cfg: ValuationConfig,
    uni_cfg: UniverseConfig,
    sotp_path: Path,
    history_years: int | None = None,
) -> ValuationStats:
    stats = ValuationStats()
    run_date = con.execute("SELECT max(as_of) FROM metrics").fetchone()[0]
    if run_date is None:
        raise ValueError("no analytics yet - run `ere build analytics` first")
    stats.run_date = run_date
    lm = latest_metrics(con, run_date).set_index("isin")
    uni = con.execute("SELECT isin, symbol, valuation_model FROM securities WHERE in_index "
                      "ORDER BY symbol").df()
    peers = con.execute("SELECT isin, peer_isin FROM peers WHERE peer_isin IS NOT NULL").df()
    coc, dcf_cfg = val_cfg.cost_of_capital, val_cfg.dcf
    k = val_cfg.multiples.band_std
    years_hist = history_years or val_cfg.multiples.history_years

    wide = fundamentals(con, ("Q", "FY", "BS"), as_of=run_date)
    snap = snapshot_fundamentals(wide).set_index("isin") if len(wide) else pd.DataFrame()
    hist = multiple_history(con, list(uni["isin"]), run_date, years_hist)
    sotp_specs = load_sotp(sotp_path)

    rows = []

    def add(isin, sym, method, scenario, value, assumptions):
        rows.append((isin, sym, run_date, method, scenario,
                     float(value) if value is not None and np.isfinite(value) else None,
                     _json(_clean(assumptions))))

    for u in uni.itertuples(index=False):
        m = lm.loc[u.isin].to_dict() if u.isin in lm.index else {}
        m = {k2: v for k2, v in m.items() if isinstance(v, (int, float)) and np.isfinite(v)}
        f = snap.loc[u.isin].to_dict() if len(snap) and u.isin in snap.index else {}
        price = m.get("price", np.nan)
        model = u.valuation_model
        stats.securities += 1
        w = compute_wacc(m, coc)
        common = {"price": price, "cost_of_equity": w.cost_of_equity, "beta_used": w.beta_used}

        # ---------------------------------------------------------------- DCF
        if model in ("dcf", "ev_sales"):
            fy = wide[(wide["isin"] == u.isin) & (wide.period_type == "FY")].sort_values(
                "period_end") if len(wide) else pd.DataFrame()
            margins = list((fy["ebitda"] / fy["revenue"]).values) if (
                len(fy) and "ebitda" in fy and "revenue" in fy) else []
            ebitda_ok = m.get("ttm_ebitda", -1) > 0
            for scen_name, scen in dcf_cfg.scenarios.items():
                g_term = min(scen.terminal_growth, w.wacc - 0.01)
                inp, notes = dcf_inputs_from_metrics(m, margins, scen, coc)
                if inp is None or (model == "ev_sales" and not ebitda_ok):
                    if scen_name == "base":
                        add(u.isin, u.symbol, "dcf", "base", None, {
                            **common, "skipped": notes or ["EBITDA not positive: DCF not "
                                                           "meaningful; see EV/Sales"]})
                        stats.skipped_notes += 1
                    continue
                res = run_dcf(inp, w.wacc, g_term, dcf_cfg.explicit_years,
                              dcf_cfg.margin_mean_reversion_years)
                a = {**common, "scenario": scen.model_dump(), "wacc": asdict(w),
                     "terminal_growth": g_term, "inputs": asdict(inp), "notes": notes,
                     "enterprise_value": res.enterprise_value,
                     "equity_value": res.equity_value, "pv_terminal_share": res.terminal_share,
                     "years": res.years}
                if scen_name == "base":
                    a["sensitivity"] = sensitivity_grid(inp, w.wacc, g_term, dcf_cfg)
                    a["reverse_dcf_growth"] = reverse_dcf(inp, price, w.wacc, g_term, dcf_cfg)
                add(u.isin, u.symbol, "dcf", scen_name, res.value_per_share, a)
                stats.dcf_run += 1

        # ---------------------------------------------------------------- residual income
        if model in ("residual_income", "insurance"):
            bv, roe, shares = m.get("bs_equity_owners"), m.get("roe"), m.get("shares")
            if bv and roe is not None and shares:
                ke = w.cost_of_equity
                payout = payout_ratio(m, val_cfg.residual_income.payout_ratio_default)
                for scen_name, scen in dcf_cfg.scenarios.items():
                    g_term = min(scen.terminal_growth, ke - 0.01)
                    res = run_residual_income(
                        bv, roe + RI_ROE_SHIFT[scen_name], ke,
                        val_cfg.residual_income.roe_fade_to_spread_over_ke, payout, g_term,
                        shares, val_cfg.residual_income.explicit_years)
                    add(u.isin, u.symbol, "residual_income", scen_name, res.value_per_share, {
                        **common, "book_value": bv, "roe_start": roe + RI_ROE_SHIFT[scen_name],
                        "roe_long_run": ke + val_cfg.residual_income.roe_fade_to_spread_over_ke,
                        "payout": payout, "terminal_growth": g_term,
                        "implied_pb": res.implied_pb, "years": res.years})
                    stats.ri_run += 1
            else:
                add(u.isin, u.symbol, "residual_income", "base", None,
                    {**common, "skipped": ["book value, ROE or share count missing"]})
                stats.skipped_notes += 1

        # ---------------------------------------------------------------- SOTP
        if model == "sotp":
            spec = sotp_specs.get(u.symbol, {})
            v, detail = run_sotp(con, spec, m.get("shares", np.nan), m.get("net_debt", 0.0),
                                 val_cfg.sotp.holdco_discount, run_date)
            add(u.isin, u.symbol, "sotp", "base", v, {**common, **detail})

        # ---------------------------------------------------------------- multiples
        h = hist[hist["isin"] == u.isin] if len(hist) else pd.DataFrame()
        peer_isins = list(peers[peers["isin"] == u.isin].peer_isin)
        for name in METHODS_BY_MODEL.get(model, ()):
            b = band(h[name], k) if name in h else None
            if b:
                for edge in ("low", "mid", "high"):
                    add(u.isin, u.symbol, f"{name}_band", edge, implied_value(b[edge], name, f),
                        {**common, "multiple": b[edge], "band": b, "years": years_hist,
                         "fundamental": f})
                stats.bands += 1
            pm = peer_median(lm.reindex(peer_isins)[PEER_METRIC[name]]
                             if PEER_METRIC[name] in lm else pd.Series(dtype=float),
                             val_cfg.multiples.min_peers)
            if pm:
                add(u.isin, u.symbol, f"{name}_peers", "mid", implied_value(pm, name, f),
                    {**common, "multiple": pm, "peers": len(peer_isins), "fundamental": f})

    df = pd.DataFrame(rows, columns=["isin", "symbol", "run_date", "method", "scenario",
                                     "value_per_share", "assumptions"])
    con.execute("DELETE FROM valuations WHERE run_date = ?", [run_date])
    if len(df):
        con.register("_v", df)
        con.execute("INSERT INTO valuations SELECT * FROM _v")
        con.unregister("_v")
    stats.rows = len(df)
    return stats


def football_field(con: duckdb.DuckDBPyConnection, isin: str, run_date: date | None = None
                   ) -> pd.DataFrame:
    """One row per method: low / mid / high value per share (NaN edges dropped)."""
    if run_date is None:
        run_date = con.execute("SELECT max(run_date) FROM valuations WHERE isin = ?",
                               [isin]).fetchone()[0]
    v = con.execute("SELECT method, scenario, value_per_share FROM valuations "
                    "WHERE isin = ? AND run_date = ?", [isin, run_date]).df()
    order = {"bear": "low", "low": "low", "base": "mid", "mid": "mid", "bull": "high",
             "high": "high"}
    v["edge"] = v.scenario.map(order)
    v = v.dropna(subset=["value_per_share", "edge"])
    if v.empty:
        return pd.DataFrame(columns=["method", "low", "mid", "high"])
    ff = v.pivot_table(index="method", columns="edge", values="value_per_share",
                       aggfunc="first").reset_index()
    for c in ("low", "mid", "high"):
        if c not in ff:
            ff[c] = np.nan
    ff["low"] = ff["low"].fillna(ff["mid"])
    ff["high"] = ff["high"].fillna(ff["mid"])
    return ff[["method", "low", "mid", "high"]]
