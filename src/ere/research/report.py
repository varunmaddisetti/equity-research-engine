"""Render the backtest as reports/research.html (linked from the index)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from ere.report import charts
from ere.report.build import TEMPLATES, _env, num, pct
from ere.research.backtest import LIMITATIONS, SignalStats


def render_research(stats: list[SignalStats], series: dict, panel: pd.DataFrame,
                    out: Path) -> Path:
    rows = [{"signal": s.signal, "horizon": f"{s.horizon}m", "months": s.months,
             "mean_ic": num(s.mean_ic, 3), "ic_t": num(s.ic_t, 2), "hit": pct(s.ic_hit_rate, 0),
             "q51": pct(s.q5_minus_q1_monthly, 2) if s.q5_minus_q1_monthly is not None else "–",
             "q51a": pct(s.q5_minus_q1_ann, 1) if s.q5_minus_q1_ann is not None else "–"}
            for s in stats]
    chart_list = []
    for sig in ("ebitda_yield", "band_discount", "earnings_yield", "book_to_price"):
        q = series.get(f"{sig}_quintiles")
        if q is None or q.empty:
            continue
        growth = (1 + q.fillna(0)).cumprod()
        avg = (1 + q.mean(axis=1).fillna(0)).cumprod()
        chart_list.append({"title": f"{sig}: growth of Rs 1, excess return, monthly rebalance",
                           "svg": charts.line_chart({"Cheapest fifth (Q5)": growth["Q5"],
                                                     "Most expensive fifth (Q1)": growth["Q1"],
                                                     "All stocks": avg},
                                                    f"{sig}: cumulative excess return",
                                                    lambda x, _=None: f"{x:.2f}")})
    dates = pd.to_datetime(panel["date"]) if len(panel) else pd.Series(dtype="datetime64[ns]")
    html = _env().get_template("research.html.j2").render(
        css=(TEMPLATES / "base.css").read_text(), rows=rows, charts=chart_list,
        limitations=LIMITATIONS, n_stocks=panel["isin"].nunique() if len(panel) else 0,
        start=dates.min().date() if len(dates) else "–",
        end=dates.max().date() if len(dates) else "–",
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
