"""Cost of capital, FCFF DCF, sensitivity grid and reverse DCF.

The DCF is deliberately simple and fully transparent, so every number in a report can be
reproduced by hand (the tests do exactly that for a toy case):

  revenue_t   = revenue_{t-1} * (1 + g_t),  g_t fades linearly from g_1 (year 1) to g_T (year N)
  margin_t    = EBITDA margin moving linearly from today's to the target over
                `margin_mean_reversion_years`, flat afterwards
  EBIT_t      = revenue_t * (margin_t - da_pct)
  FCFF_t      = EBIT_t * (1 - tax) + D&A_t - capex_t - nwc_pct * (revenue_t - revenue_{t-1})
  capex_t     = revenue_t * capex_pct_t, capex_pct fading from today's level to a steady
                state of da_pct * (1 + g_T / 0.10) by year N (growth needs net investment)
  TV          = FCFF_N * (1 + g_T) / (WACC - g_T)        (Gordon growth, end-year discounting)
  equity      = EV - net debt - minority interest;   value per share = equity / shares
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from ere.config import CostOfCapital, DCFConfig, Scenario

G1_BOUNDS = (-0.05, 0.35)     # starting growth is clipped: extrapolating 60% growth is silly
TAX_BOUNDS = (0.15, 0.35)
NWC_BOUNDS = (-0.20, 0.50)


@dataclass
class Wacc:
    cost_of_equity: float
    cost_of_debt_pre_tax: float
    cost_of_debt_post_tax: float
    weight_equity: float
    weight_debt: float
    wacc: float
    beta_used: float
    notes: list[str] = field(default_factory=list)


def compute_wacc(m: dict, coc: CostOfCapital) -> Wacc:
    notes = []
    beta = m.get("beta_adj", np.nan)
    if not np.isfinite(beta):
        beta = 1.0
        notes.append("beta unavailable (short price history): 1.0 used")
    ke = coc.cost_of_equity(beta)
    debt = m.get("bs_debt", 0.0) or 0.0
    fin = m.get("ttm_finance_cost", np.nan)
    kd = fin / debt if debt > 0 and np.isfinite(fin) else coc.cost_of_debt_floor
    kd = min(max(kd, coc.cost_of_debt_floor), coc.cost_of_debt_cap)
    kd_post = kd * (1 - coc.marginal_tax_rate)
    e = m.get("market_cap", np.nan)
    if not np.isfinite(e) or e <= 0:
        we, wd = 1.0, 0.0
        notes.append("market cap unavailable: equity weight 100%")
    else:
        we, wd = e / (e + debt), debt / (e + debt)
    return Wacc(ke, kd, kd_post, we, wd, we * ke + wd * kd_post,
                min(max(beta, coc.beta_floor), coc.beta_cap), notes)


@dataclass
class DCFInputs:
    revenue: float            # base-year (TTM) revenue
    ebitda_margin: float      # today's margin
    target_margin: float      # long-run margin (5-year average by default)
    g1: float                 # year-1 growth
    da_pct: float
    capex_pct: float
    nwc_pct: float
    tax_rate: float
    net_debt: float
    minority_interest: float
    shares: float


@dataclass
class DCFResult:
    value_per_share: float
    enterprise_value: float
    equity_value: float
    pv_explicit: float
    pv_terminal: float
    terminal_share: float
    wacc: float
    terminal_growth: float
    years: list[dict]


def run_dcf(inp: DCFInputs, wacc: float, g_term: float, years: int = 10,
            margin_years: int = 5) -> DCFResult:
    if wacc <= g_term:
        raise ValueError(f"WACC {wacc:.4f} must exceed terminal growth {g_term:.4f}")
    rev_prev = inp.revenue
    capex_end = inp.da_pct * (1 + g_term / 0.10)
    rows, pv = [], 0.0
    fcff = 0.0
    for t in range(1, years + 1):
        g = inp.g1 + (g_term - inp.g1) * (t - 1) / max(years - 1, 1)
        rev = rev_prev * (1 + g)
        k = min(t / margin_years, 1.0)
        margin = inp.ebitda_margin + (inp.target_margin - inp.ebitda_margin) * k
        capex_pct = inp.capex_pct + (capex_end - inp.capex_pct) * t / years
        ebitda = rev * margin
        da = rev * inp.da_pct
        ebit = ebitda - da
        capex = rev * capex_pct
        dnwc = inp.nwc_pct * (rev - rev_prev)
        fcff = ebit * (1 - inp.tax_rate) + da - capex - dnwc
        df = (1 + wacc) ** -t
        pv += fcff * df
        rows.append({"year": t, "growth": g, "revenue": rev, "ebitda_margin": margin,
                     "ebit": ebit, "capex": capex, "delta_nwc": dnwc, "fcff": fcff,
                     "discount_factor": df, "pv_fcff": fcff * df})
        rev_prev = rev
    tv = fcff * (1 + g_term) / (wacc - g_term)
    pv_tv = tv * (1 + wacc) ** -years
    ev = pv + pv_tv
    equity = ev - inp.net_debt - inp.minority_interest
    vps = equity / inp.shares if inp.shares and inp.shares > 0 else float("nan")
    return DCFResult(vps, ev, equity, pv, pv_tv, pv_tv / ev if ev else float("nan"),
                     wacc, g_term, rows)


def dcf_inputs_from_metrics(m: dict, fy_margins: list[float], scenario: Scenario,
                            coc: CostOfCapital) -> tuple[DCFInputs | None, list[str]]:
    """Build DCF inputs from the analytics snapshot. Returns (inputs or None, notes)."""
    notes = []
    rev = m.get("ttm_revenue", np.nan)
    ebitda = m.get("ttm_ebitda", np.nan)
    shares = m.get("shares", np.nan)
    if not (np.isfinite(rev) and rev > 0 and np.isfinite(ebitda) and np.isfinite(shares)):
        return None, ["DCF not run: TTM revenue, EBITDA or share count missing"]
    margin = ebitda / rev
    hist = [x for x in fy_margins if np.isfinite(x)]
    target = float(np.mean(hist[-5:])) if hist else margin
    target += scenario.margin_shift
    g0 = m.get("cagr_3y_revenue", m.get("growth_1y_revenue", np.nan))
    if not np.isfinite(g0):
        g0 = 0.10
        notes.append("no revenue growth history: 10% starting growth assumed")
    g1 = min(max(g0 * scenario.growth_multiplier, G1_BOUNDS[0]), G1_BOUNDS[1])
    if g1 != g0 * scenario.growth_multiplier:
        notes.append(f"starting growth clipped to {g1:.0%}")
    da = m.get("depreciation_fy", np.nan)
    rev_fy = rev
    da_pct = da / rev_fy if np.isfinite(da) and da >= 0 else 0.03
    capex = m.get("capex_fy", np.nan)
    capex_pct = capex / rev_fy if np.isfinite(capex) and capex >= 0 else da_pct * 1.2
    nwc = (m.get("bs_trade_receivables", 0.0) + m.get("bs_inventories", 0.0)
           - m.get("bs_trade_payables", 0.0)) / rev
    nwc = min(max(nwc, NWC_BOUNDS[0]), NWC_BOUNDS[1])
    pbt, tax = m.get("ttm_pbt", np.nan), np.nan
    tax_rate = coc.marginal_tax_rate
    if np.isfinite(pbt) and pbt > 0 and np.isfinite(m.get("ttm_pat", np.nan)):
        tax = 1 - m["ttm_pat"] / pbt
        if np.isfinite(tax):
            tax_rate = min(max(tax, TAX_BOUNDS[0]), TAX_BOUNDS[1])
    net_debt = m.get("bs_debt", 0.0) - m.get("bs_cash_like", 0.0)
    return DCFInputs(rev, margin, target, g1, da_pct, capex_pct, nwc, tax_rate, net_debt,
                     m.get("bs_minority_interest", 0.0), shares), notes


def sensitivity_grid(inp: DCFInputs, wacc: float, g_term: float, cfg: DCFConfig
                     ) -> dict:
    n = cfg.sensitivity.grid_size
    half = n // 2
    waccs = [wacc + (i - half) * cfg.sensitivity.wacc_step for i in range(n)]
    gs = [g_term + (j - half) * cfg.sensitivity.growth_step for j in range(n)]
    grid = []
    for w in waccs:
        row = []
        for g in gs:
            if w - g < 0.005:
                row.append(None)
            else:
                row.append(run_dcf(inp, w, g, cfg.explicit_years,
                                   cfg.margin_mean_reversion_years).value_per_share)
        grid.append(row)
    return {"wacc": waccs, "terminal_growth": gs, "value_per_share": grid}


def reverse_dcf(inp: DCFInputs, price: float, wacc: float, g_term: float, cfg: DCFConfig,
                lo: float = -0.30, hi: float = 0.80) -> float | None:
    """Year-1 growth that makes the DCF value equal the market price (bisection)."""
    if not (np.isfinite(price) and price > 0):
        return None

    def f(g1):
        x = DCFInputs(**{**asdict(inp), "g1": g1})
        return run_dcf(x, wacc, g_term, cfg.explicit_years,
                       cfg.margin_mean_reversion_years).value_per_share - price

    flo, fhi = f(lo), f(hi)
    if not (math.isfinite(flo) and math.isfinite(fhi)) or flo * fhi > 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-6 * price:
            break
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2
