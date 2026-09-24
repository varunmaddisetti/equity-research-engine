"""Residual income (excess return) model for banks, NBFCs and insurers.

Why not DCF: for a lender, debt is raw material, not financing, so free cash flow to the firm
is not meaningful. Value = book value today + present value of future profits in excess of
the cost of equity:

  V0   = BV0 + sum_{t=1..N} (ROE_t - Ke) * BV_{t-1} / (1 + Ke)^t  + PV(terminal)
  ROE_t fades linearly from today's ROE (plus scenario shift) to Ke + spread by year N
  BV_t = BV_{t-1} * (1 + ROE_t * (1 - payout))
  terminal = (ROE_N+1 - Ke) * BV_N / (Ke - g), discounted N years, g < Ke

A lender earning exactly its cost of equity is worth book value: P/B = 1.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

ROE_BOUNDS = (-0.10, 0.35)


@dataclass
class RIResult:
    value_per_share: float
    equity_value: float
    book_value: float
    pv_excess: float
    pv_terminal: float
    implied_pb: float
    years: list[dict]


def run_residual_income(bv0: float, roe0: float, ke: float, spread: float, payout: float,
                        g_term: float, shares: float, years: int = 10) -> RIResult:
    if ke <= g_term:
        raise ValueError("cost of equity must exceed terminal growth")
    roe0 = min(max(roe0, ROE_BOUNDS[0]), ROE_BOUNDS[1])
    roe_end = ke + spread
    bv, pv, rows = bv0, 0.0, []
    for t in range(1, years + 1):
        roe = roe0 + (roe_end - roe0) * t / years
        ri = (roe - ke) * bv
        df = (1 + ke) ** -t
        pv += ri * df
        rows.append({"year": t, "roe": roe, "book_value_open": bv, "residual_income": ri,
                     "pv": ri * df})
        bv = bv * (1 + roe * (1 - payout))
    ri_next = (roe_end - ke) * bv
    pv_tv = ri_next / (ke - g_term) * (1 + ke) ** -years
    equity = bv0 + pv + pv_tv
    vps = equity / shares if shares and shares > 0 else float("nan")
    return RIResult(vps, equity, bv0, pv, pv_tv, equity / bv0 if bv0 else float("nan"), rows)


def payout_ratio(m: dict, default: float) -> float:
    dy, pe = m.get("dividend_yield", np.nan), m.get("pe_ttm", np.nan)
    if np.isfinite(dy) and np.isfinite(pe) and pe > 0:
        return float(min(max(dy * pe, 0.0), 0.9))
    return default
