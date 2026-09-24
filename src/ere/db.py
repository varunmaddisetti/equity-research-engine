"""DuckDB warehouse: schema creation and connection helper.

Design rules
- Every fact row carries `source` and, where it applies, `filing_date`/`ingested_at`
  so data can be queried point-in-time (no look-ahead in backtests).
- Financials are stored LONG (one row per field) so new XBRL tags never need a migration.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from ere.paths import DB_PATH

SCHEMA_VERSION = 1

DDL = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR
    )
    """,
    # Keyed by ISIN: symbols change on renames (e.g. Suven -> COHANCE), ISINs usually don't.
    """
    CREATE TABLE IF NOT EXISTS securities (
        isin            VARCHAR PRIMARY KEY,
        symbol          VARCHAR NOT NULL,
        name            VARCHAR NOT NULL,
        industry        VARCHAR,
        valuation_model VARCHAR NOT NULL,
        short_history   BOOLEAN NOT NULL,
        in_index        BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
    # Index membership history -> avoids survivorship bias in the M7 backtest.
    """
    CREATE TABLE IF NOT EXISTS index_membership (
        index_name  VARCHAR NOT NULL,
        symbol      VARCHAR NOT NULL,
        isin        VARCHAR NOT NULL,
        from_date   DATE NOT NULL,
        to_date     DATE,
        PRIMARY KEY (index_name, isin, from_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices_daily (
        isin          VARCHAR NOT NULL,
        symbol        VARCHAR NOT NULL,
        date          DATE NOT NULL,
        open          DOUBLE,
        high          DOUBLE,
        low           DOUBLE,
        close         DOUBLE NOT NULL,
        prev_close    DOUBLE,
        volume        BIGINT,
        traded_value  DOUBLE,
        delivery_pct  DOUBLE,
        adj_factor    DOUBLE NOT NULL DEFAULT 1.0,
        adj_close     DOUBLE,
        source        VARCHAR NOT NULL,
        PRIMARY KEY (isin, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_prices_daily (
        index_name  VARCHAR NOT NULL,
        date        DATE NOT NULL,
        close       DOUBLE NOT NULL,
        source      VARCHAR NOT NULL,
        PRIMARY KEY (index_name, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS corp_actions (
        isin      VARCHAR NOT NULL,
        symbol    VARCHAR NOT NULL,
        ex_date   DATE NOT NULL,
        action    VARCHAR NOT NULL,   -- split | bonus | dividend | rights | demerger
        ratio_old DOUBLE,
        ratio_new DOUBLE,
        amount    DOUBLE,             -- per-share cash for dividends
        purpose   VARCHAR,            -- raw text from the exchange
        source    VARCHAR NOT NULL,
        PRIMARY KEY (isin, ex_date, action)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financials (
        isin         VARCHAR NOT NULL,
        symbol       VARCHAR NOT NULL,
        period_end   DATE NOT NULL,
        period_type  VARCHAR NOT NULL,   -- Q | H | FY
        basis        VARCHAR NOT NULL,   -- consolidated | standalone
        field        VARCHAR NOT NULL,   -- standard field name from xbrl_mapping.yaml
        value        DOUBLE,
        unit         VARCHAR NOT NULL DEFAULT 'INR',
        xbrl_tag     VARCHAR,
        filing_date  DATE NOT NULL,      -- when the market could first see it
        is_restated  BOOLEAN NOT NULL DEFAULT FALSE,
        source       VARCHAR NOT NULL,
        PRIMARY KEY (isin, period_end, period_type, basis, field, filing_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shareholding (
        isin         VARCHAR NOT NULL,
        symbol       VARCHAR NOT NULL,
        quarter_end  DATE NOT NULL,
        category     VARCHAR NOT NULL,   -- promoter | fii | dii | mf | public | other
        pct          DOUBLE NOT NULL,
        pledged_pct  DOUBLE,             -- % of promoter holding pledged
        filing_date  DATE,
        source       VARCHAR NOT NULL,
        PRIMARY KEY (isin, quarter_end, category)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS macro (
        series  VARCHAR NOT NULL,
        date    DATE NOT NULL,
        value   DOUBLE NOT NULL,
        source  VARCHAR NOT NULL,
        PRIMARY KEY (series, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS valuations (
        isin             VARCHAR NOT NULL,
        symbol           VARCHAR NOT NULL,
        run_date         DATE NOT NULL,
        method           VARCHAR NOT NULL,   -- dcf | residual_income | pe | ev_ebitda | pb | ...
        scenario         VARCHAR NOT NULL,   -- bear | base | bull | low | mid | high
        value_per_share  DOUBLE,
        assumptions      JSON,
        PRIMARY KEY (isin, run_date, method, scenario)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_log (
        run_id       VARCHAR NOT NULL,
        dataset      VARCHAR NOT NULL,
        key          VARCHAR NOT NULL,   -- e.g. a date or an ISIN
        status       VARCHAR NOT NULL,   -- ok | missing | error
        rows         INTEGER,
        message      VARCHAR,
        ingested_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
]


def connect(db_path: Path = DB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in DDL:
        con.execute(stmt)
    con.execute(
        "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)]
    )


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    return sorted(r[0] for r in con.execute("SHOW TABLES").fetchall())
