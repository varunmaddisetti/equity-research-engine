from datetime import date

import pandas as pd
import pytest
from fixtures_nse import (
    A_NEW,
    A_OLD,
    CORP_ACTION_RECORDS,
    B,
    C,
    D,
    E,
    FakeClient,
    market_files,
    write_raw_cache,
)
from typer.testing import CliRunner

from ere.clean.adjust_prices import build_adjusted_prices
from ere.clean.security_master import build_security_master
from ere.db import connect, init_db
from ere.ingest.corp_actions import records_to_frame
from ere.ingest.runner import ingest_daily


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.duckdb")
    init_db(c)
    ingest_daily(c, tmp_path / "raw", date(2024, 7, 1), date(2024, 7, 12),
                 datasets=["bhavcopy"], client=FakeClient(market_files()),
                 today=date(2024, 8, 1))
    df = records_to_frame(CORP_ACTION_RECORDS)
    c.register("ca", df)
    c.execute("INSERT INTO corp_actions SELECT * FROM ca")
    yield c
    c.close()


def _adj(con, isin_or_sid):
    return con.execute(
        "SELECT date, close, adj_factor, adj_close, ret_1d, isin FROM prices_adjusted "
        "WHERE security_id = ? ORDER BY date", [isin_or_sid]
    ).df()


def test_security_master_chains_split_isin_and_keeps_rename(con):
    m = build_security_master(con).set_index("isin")
    assert m.loc[A_OLD, "security_id"] == A_NEW == m.loc[A_NEW, "security_id"]
    assert m.loc[E, "security_id"] == E and m.loc[E, "symbol"] == "EEE"
    assert m["security_id"].nunique() == 5


def test_recycled_symbol_is_not_chained(tmp_path):
    with connect(tmp_path / "r.duckdb") as c:
        init_db(c)
        rows = [("INE111X01011", "2018-01-01", "OLDCO"), ("INE111X01011", "2018-01-02", "OLDCO"),
                ("INE222Y01011", "2018-01-03", "BYSTANDER")] + [
            ("INE222Y01011", f"2018-01-{d:02d}", "BYSTANDER") for d in range(4, 20)
        ] + [("INE333Z01011", "2018-01-22", "OLDCO")]
        df = pd.DataFrame(rows, columns=["isin", "date", "symbol"])
        df["date"] = pd.to_datetime(df["date"])
        c.register("r", df)
        c.execute("INSERT INTO prices_daily (isin, date, symbol, series, close, source) "
                  "SELECT isin, date, symbol, 'EQ', 1.0, 't' FROM r")
        m = build_security_master(c).set_index("isin")
        assert m.loc["INE111X01011", "security_id"] != m.loc["INE333Z01011", "security_id"]


def test_split_with_isin_change_is_continuous(con, tmp_path):
    stats = build_adjusted_prices(con, tmp_path / "none.yaml", scope="all")
    a = _adj(con, A_NEW)
    assert len(a) == 9 and set(a["isin"]) == {A_OLD, A_NEW}
    pre = a[a.date < "2024-07-09"]
    assert pre.adj_factor.eq(0.2).all()
    assert a[a.date >= "2024-07-09"].adj_factor.eq(1.0).all()
    # 1% drift every day, no jump on the split
    assert a.ret_1d.dropna().between(0.009, 0.011).all()
    assert stats.events_corp_action >= 2


def test_bonus_adjusted_and_dividend_ignored(con, tmp_path):
    build_adjusted_prices(con, tmp_path / "none.yaml", scope="all")
    b = _adj(con, B)
    assert b[b.date < "2024-07-03"].adj_close.eq(250).all()
    assert b.ret_1d.dropna().abs().max() == pytest.approx(0)


def test_unexplained_base_adjustment_is_used_and_flagged(con, tmp_path):
    build_adjusted_prices(con, tmp_path / "none.yaml", scope="all")
    c = _adj(con, C)
    assert c.ret_1d.dropna().abs().max() == pytest.approx(0)
    kinds = con.execute("SELECT kind FROM price_anomalies WHERE security_id = ?", [C]).fetchall()
    assert kinds == [("unmatched_base_adjustment",)]


def test_real_crash_is_not_adjusted_but_flagged(con, tmp_path):
    build_adjusted_prices(con, tmp_path / "none.yaml", scope="all")
    d = _adj(con, D)
    assert d.adj_factor.eq(1.0).all()
    row = con.execute("SELECT kind, severity, value FROM price_anomalies WHERE security_id = ?",
                      [D]).fetchall()
    assert row == [("large_move", "warn", pytest.approx(-0.3))]


def test_manual_override_wins(con, tmp_path):
    p = tmp_path / "manual.yaml"
    p.write_text("adjustments:\n  - symbol: CCC\n    ex_date: 2024-07-10\n    factor: 0.75\n"
                 "    note: demerger per scheme\n")
    stats = build_adjusted_prices(con, p, scope="all")
    assert stats.events_manual == 1
    ev = con.execute("SELECT factor, source FROM price_events WHERE security_id = ?",
                     [C]).fetchall()
    assert ev == [(0.75, "manual")]


def test_factor_mismatch_detected(con, tmp_path):
    # Pretend the record said 1:2 bonus while the exchange applied 1:1.
    con.execute("UPDATE corp_actions SET factor = 2.0/3 WHERE action = 'bonus'")
    build_adjusted_prices(con, tmp_path / "none.yaml", scope="all")
    kinds = {k for (k,) in con.execute(
        "SELECT kind FROM price_anomalies WHERE security_id = ?", [B]).fetchall()}
    assert "factor_mismatch" in kinds


def test_universe_scope_only_builds_index_members(con, tmp_path):
    con.execute("INSERT INTO securities (isin, symbol, name, valuation_model, short_history) "
                "VALUES (?, 'AAA', 'AAA Ltd', 'dcf', FALSE)", [A_NEW])
    stats = build_adjusted_prices(con, tmp_path / "none.yaml", scope="universe")
    assert stats.securities == 1
    assert con.execute("SELECT DISTINCT security_id FROM prices_adjusted").fetchall() == [(A_NEW,)]


def test_cli_offline_pipeline(tmp_path, monkeypatch):
    import ere.cli as cli

    write_raw_cache(tmp_path / "raw")
    db = tmp_path / "cli.duckdb"
    monkeypatch.setattr(cli, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(cli, "connect", lambda read_only=False: connect(db, read_only))
    r = CliRunner()
    res = r.invoke(cli.app, ["ingest", "prices", "--start", "2024-07-01", "--end", "2024-07-12",
                             "--offline"])
    assert res.exit_code == 0, res.output
    res = r.invoke(cli.app, ["db", "sync-universe"])
    assert res.exit_code == 0, res.output
    res = r.invoke(cli.app, ["build", "prices", "--scope", "all"])
    assert res.exit_code == 0, res.output
    res = r.invoke(cli.app, ["check", "prices"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "processed" / "price_checks.csv").exists()
    res = r.invoke(cli.app, ["db", "status"])
    assert "ingest log" in res.output
