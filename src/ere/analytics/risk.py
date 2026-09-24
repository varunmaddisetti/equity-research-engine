"""Price-based risk and liquidity metrics from adjusted prices.

All functions take a price Series indexed by date (adjusted close) and only use data up to
`as_of`, so the same code serves reports (as_of = today) and backtests.

Beta: weekly (Friday-close) log returns over `weeks` against an index, OLS slope.
Blume adjustment (0.67 * raw + 0.33) pulls estimates toward 1, which is standard practice
because raw betas mean-revert. Smallcaps are benchmarked to the Nifty Smallcap 100; the
Nifty 50 beta is kept for reference.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def weekly_returns(px: pd.Series) -> pd.Series:
    w = px.resample("W-FRI").last().dropna()
    return np.log(w).diff().dropna()


def beta(stock: pd.Series, index: pd.Series, weeks: int = 104, min_weeks: int = 52
         ) -> tuple[float, int]:
    """Returns (raw beta, weeks used). NaN if fewer than min_weeks overlapping weeks."""
    rs, ri = weekly_returns(stock), weekly_returns(index)
    df = pd.concat([rs, ri], axis=1, join="inner").dropna().tail(weeks)
    if len(df) < min_weeks or df.iloc[:, 1].var() == 0:
        return float("nan"), len(df)
    cov = np.cov(df.iloc[:, 0], df.iloc[:, 1], ddof=1)
    return float(cov[0, 1] / cov[1, 1]), len(df)


def blume(b: float) -> float:
    return 0.67 * b + 0.33 if np.isfinite(b) else float("nan")


def annual_vol(px: pd.Series, days: int = TRADING_DAYS) -> float:
    r = np.log(px).diff().dropna().tail(days)
    return float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(r) > 20 else float("nan")


def max_drawdown(px: pd.Series) -> float:
    if px.empty:
        return float("nan")
    return float((px / px.cummax() - 1).min())


def trailing_return(px: pd.Series, years: float) -> float:
    """Annualised total price return over `years` (simple for <= 1y)."""
    if px.empty:
        return float("nan")
    end_date = px.index[-1]
    start = px[px.index <= end_date - pd.DateOffset(days=int(365.25 * years))]
    if start.empty:
        return float("nan")
    total = px.iloc[-1] / start.iloc[-1]
    return float(total - 1) if years <= 1 else float(total ** (1 / years) - 1)


def risk_metrics(
    px: pd.Series,
    traded_value: pd.Series,
    idx_small: pd.Series,
    idx_large: pd.Series,
    weeks: int = 104,
) -> dict[str, float]:
    px = px.dropna()
    out: dict[str, float] = {}
    if px.empty:
        return out
    last = px.index[-1]
    b_small, n_small = beta(px, idx_small[idx_small.index <= last], weeks)
    b_large, _ = beta(px, idx_large[idx_large.index <= last], weeks)
    yr = px[px.index > last - pd.DateOffset(years=1)]
    out.update({
        "price": float(px.iloc[-1]),
        "beta_raw": b_small,
        "beta_adj": blume(b_small),
        "beta_weeks": float(n_small),
        "beta_nifty50_raw": b_large,
        "ret_1y": trailing_return(px, 1),
        "ret_3y_cagr": trailing_return(px, 3),
        "ret_5y_cagr": trailing_return(px, 5),
        "vol_1y": annual_vol(px),
        "max_drawdown_3y": max_drawdown(px[px.index > last - pd.DateOffset(years=3)]),
        "high_52w": float(yr.max()),
        "low_52w": float(yr.min()),
    })
    rng = out["high_52w"] - out["low_52w"]
    out["pos_52w"] = (out["price"] - out["low_52w"]) / rng if rng > 0 else float("nan")
    tv = traded_value.dropna()
    tv6 = tv[tv.index > last - pd.DateOffset(months=6)]
    out["median_traded_value_6m_cr"] = float(tv6.median() / 1e7) if len(tv6) else float("nan")
    return out
