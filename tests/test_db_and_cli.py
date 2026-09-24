from pathlib import Path

from typer.testing import CliRunner

from ere.cli import app
from ere.db import connect, init_db, list_tables

EXPECTED = {
    "corp_actions", "financials", "index_membership", "index_prices_daily", "ingest_log",
    "macro", "meta", "prices_daily", "securities", "shareholding", "valuations",
}


def test_init_db_is_idempotent(tmp_path: Path):
    with connect(tmp_path / "t.duckdb") as con:
        init_db(con)
        init_db(con)
        assert set(list_tables(con)) == EXPECTED
        assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"


def test_financials_primary_key_keeps_restatements(tmp_path: Path):
    """Same period, two filing dates (original + restated) must both be kept."""
    with connect(tmp_path / "t.duckdb") as con:
        init_db(con)
        sql = (
            "INSERT INTO financials VALUES "
            "('INE000000001','X','2025-03-31','FY','consolidated','revenue',?,'INR','t',?,?,'test')"
        )
        con.execute(sql, [100.0, "2025-05-10", False])
        con.execute(sql, [104.0, "2025-08-01", True])
        assert con.execute("SELECT count(*) FROM financials").fetchone()[0] == 2


runner = CliRunner()


def test_cli_version():
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0 and r.stdout.strip()


def test_cli_config_check():
    r = runner.invoke(app, ["config", "check"])
    assert r.exit_code == 0, r.stdout
    assert "100 stocks" in r.stdout


def test_cli_sync_universe(tmp_path: Path, monkeypatch):
    import ere.cli as cli

    db = tmp_path / "t.duckdb"
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "connect", lambda read_only=False: connect(db, read_only))
    r = runner.invoke(app, ["db", "sync-universe"])
    assert r.exit_code == 0, r.stdout
    with connect(tmp_path / "t.duckdb") as con:
        assert con.execute("SELECT count(*) FROM securities WHERE in_index").fetchone()[0] == 100
        assert con.execute("SELECT count(*) FROM index_membership").fetchone()[0] == 100
    # Re-running must not duplicate memberships.
    runner.invoke(app, ["db", "sync-universe"])
    with connect(tmp_path / "t.duckdb") as con:
        assert con.execute("SELECT count(*) FROM index_membership").fetchone()[0] == 100


def test_cli_unimplemented_ingest_exits_2():
    r = runner.invoke(app, ["ingest", "prices"])
    assert r.exit_code == 2
