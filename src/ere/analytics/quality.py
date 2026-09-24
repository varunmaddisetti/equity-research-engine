"""Quality / red-flag checks. Each flag is True, False, or None (not enough data).

Thresholds come from config/valuation.yaml -> quality_flags, plus a few defaults below that
are not yet in the config. Flags describe; they never produce a buy/sell view.
"""

from __future__ import annotations

import numpy as np

from ere.config import QualityFlagsConfig

NET_DEBT_EBITDA_MAX = 3.0
GNPA_PCT_MAX = 5.0
PLEDGE_RISE_PP = 1.0


def _v(m: dict, k: str) -> float:
    x = m.get(k, np.nan)
    return float(x) if x is not None and np.isfinite(x) else np.nan


def evaluate_flags(m: dict, cfg: QualityFlagsConfig, is_lender: bool, short_history: bool
                   ) -> list[tuple[str, bool | None, float, float, str]]:
    """Returns (flag, triggered, value, threshold, note)."""
    out = []

    def add(flag, value, threshold, test, note, applies=True):
        if not applies:
            return
        if not np.isfinite(value):
            out.append((flag, None, np.nan, threshold, "not enough data"))
        else:
            out.append((flag, bool(test(value, threshold)), value, threshold, note))

    add("promoter_pledge_high", _v(m, "pledged_pct"), cfg.promoter_pledge_pct_max,
        lambda v, t: v > t, "% of promoter shares encumbered")
    add("promoter_pledge_rising", _v(m, "pledged_change_1y_pp"), PLEDGE_RISE_PP,
        lambda v, t: v > t, "change in encumbered % over a year (pp)")
    add("promoter_holding_drop", -_v(m, "promoter_change_1y_pp"), cfg.promoter_holding_drop_pp_1y,
        lambda v, t: v > t, "fall in promoter holding over a year (pp)")
    add("low_cash_conversion", _v(m, "cfo_to_pat_3y"), cfg.cfo_to_pat_3y_min,
        lambda v, t: v < t, "3-year operating cash flow / PAT", applies=not is_lender)
    rd, rd0 = _v(m, "receivable_days"), _v(m, "receivable_days_prev")
    add("receivable_days_rising", rd / rd0 - 1 if rd0 and np.isfinite(rd0) else np.nan,
        cfg.receivable_days_yoy_increase_max, lambda v, t: v > t,
        "year-on-year change in receivable days", applies=not is_lender)
    add("high_leverage", _v(m, "net_debt_to_ebitda"), NET_DEBT_EBITDA_MAX,
        lambda v, t: v > t, "net debt / TTM EBITDA", applies=not is_lender)
    add("negative_ebitda", _v(m, "ttm_ebitda"), 0.0, lambda v, t: v < t, "TTM EBITDA",
        applies=not is_lender)
    add("high_gnpa", _v(m, "gross_npa_pct"), GNPA_PCT_MAX, lambda v, t: v > t,
        "gross NPA % of advances", applies=is_lender)
    add("low_liquidity", _v(m, "median_traded_value_6m_cr"),
        cfg.min_median_daily_traded_value_cr, lambda v, t: v < t,
        "median daily traded value, last 6 months (Rs cr)")
    out.append(("short_history", bool(short_history), np.nan, np.nan,
                "listed or restructured recently; fewer years of data"))
    for flag in ("auditor_change", "contingent_liabilities_high", "asm_gsm_surveillance"):
        out.append((flag, None, np.nan, np.nan, "not available from XBRL in v1"))
    return out
