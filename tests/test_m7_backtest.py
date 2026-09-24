import numpy as np
import pandas as pd
import pytest
from fixtures_market import load_market
from typer.testing import CliRunner

from ere.db import connect, init_db
from ere.research.backtest import (
    add_signals,
    build_panel,
    forward_returns,
    quintile_returns,
    rank_ic,
    summarise,
)


def test_band_discount_uses_only_past_months():
    dates = pd.date_range("2020-01-31", periods=15, freq="ME")
    mult = [10.0] * 12 + [5.0, 20.0, 10.0]
    hist = pd.DataFrame({"isin": "A", "date": dates, "ev_ebitda": mult,
                         "pe": 10.0, "pb": 1.0, "ev_sales": 1.0})
    h = add_signals(hist, {"A": "dcf"}).set_index("date")
    assert h["band_discount"].iloc[:12].isna().all()        # needs 12 past points
    # month 13: past median 10, today 5 -> log(2) cheap
    assert h["band_discount"].iloc[12] == pytest.approx(np.log(2))
    # month 14 must not see its own 20 in the median
    assert h["band_discount"].iloc[13] == pytest.approx(np.log(10 / 20))
    assert h["earnings_yield"].iloc[0] == pytest.approx(0.1)


def test_forward_returns_align_to_future_prices():
    d = pd.bdate_range("2021-01-01", "2021-06-15")  # data ends before 31 May + 1 month
    px = pd.DataFrame({"isin": "A", "date": d, "adj_close": np.arange(len(d)) + 100.0})
    me = pd.DatetimeIndex(["2021-01-29", "2021-05-31"])
    f = forward_returns(px, me, horizons=(1,)).set_index("date")
    jan, feb_end = 100.0 + list(d).index(pd.Timestamp("2021-01-29")), \
        100.0 + list(d).index(pd.Timestamp("2021-02-26"))
    assert f.loc["2021-01-29", "fwd_1m"] == pytest.approx(feb_end / jan - 1)
    assert np.isnan(f.loc["2021-05-31", "fwd_1m"])           # future beyond the data


def _planted_panel(n_stocks=40, months=24, strength=1.0, seed=3):
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.date_range("2022-01-31", periods=months, freq="ME"):
        sig = rng.normal(size=n_stocks)
        ret = strength * 0.01 * sig + rng.normal(0, 0.01, n_stocks)
        for i in range(n_stocks):
            rows.append({"isin": f"S{i}", "date": d, "ebitda_yield": sig[i],
                         "xs_1m": ret[i], "xs_3m": ret[i], "xs_12m": ret[i]})
    return pd.DataFrame(rows)


def test_rank_ic_and_quintiles_detect_planted_signal():
    p = _planted_panel()
    ic = rank_ic(p, "ebitda_yield", "xs_1m")
    assert len(ic) == 24 and ic.mean() > 0.4
    q = quintile_returns(p, "ebitda_yield", "xs_1m")
    assert (q["Q5"] > q["Q1"]).mean() > 0.9


def test_no_signal_gives_ic_near_zero():
    ic = rank_ic(_planted_panel(strength=0.0), "ebitda_yield", "xs_1m")
    assert abs(ic.mean()) < 0.1


def test_summarise_reports_all_horizons():
    stats, series = summarise(_planted_panel())
    st = {(s.signal, s.horizon): s for s in stats}
    assert set(st) == {("ebitda_yield", 1), ("ebitda_yield", 3), ("ebitda_yield", 12)}
    assert st[("ebitda_yield", 1)].q5_minus_q1_monthly > 0
    assert "ebitda_yield_quintiles" in series


def test_min_stocks_guard():
    assert rank_ic(_planted_panel(n_stocks=10), "ebitda_yield", "xs_1m").empty


def test_build_panel_on_synthetic_market(tmp_path):
    c = connect(tmp_path / "b.duckdb")
    init_db(c)
    load_market(c)
    panel = build_panel(c, years=2)
    assert {"earnings_yield", "band_discount", "fwd_1m", "xs_12m"} <= set(panel.columns)
    last = panel["date"].max()
    assert panel.loc[panel["date"] == last, "fwd_1m"].isna().all()
    c.close()


def test_cli_research_backtest(tmp_path, monkeypatch):
    import ere.cli as cli

    db = tmp_path / "b.duckdb"
    with connect(db) as c:
        init_db(c)
        load_market(c)
    monkeypatch.setattr(cli, "connect", lambda read_only=False: connect(db, read_only))
    monkeypatch.setattr(cli, "PROCESSED_DIR", tmp_path / "processed")
    res = CliRunner().invoke(cli.app, ["research", "backtest", "--years", "2",
                                       "--out", str(tmp_path / "site")])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "site" / "research.html").exists()
    assert "Survivorship" in (tmp_path / "site" / "research.html").read_text()
