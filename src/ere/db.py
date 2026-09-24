"""DuckDB warehouse: schema creation and connection helper.

Design rules
- Raw facts (prices_daily, corp_actions, ...) are stored exactly as the exchange published
  them. Derived tables (security_master, prices_adjusted, ...) are rebuilt from scratch by
  `ere build prices` and can always be dropped.
- Every fact row carries `source` and, where it applies, `filing_date`, so data can be
  queried point-in-time (no look-ahead in backtests).
- Financials are stored LONG (one row per field) so new XBRL tags never need a migration.
- ISINs are NOT stable identifiers in India: a face-value split issues a new ISIN.
  `security_master` chains old and new ISINs into one `security_id` (the latest ISIN).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from ere.paths import DB_PATH

SCHEMA_VERSION = 2

# Tables whose definition changed in v2. No v1 release ever wrote rows to them (ingest was
# not implemented in v1), so dropping them on upgrade loses nothing.
_CHANGED_IN_V2 = ["prices_daily", "index_prices_daily", "corp_actions"]

DDL = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR
    )
    """,
    # Current constituents. Keyed by the *current* ISIN.
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
    # ------------------------------------------------------------------ raw facts
    # Whole NSE main board (series EQ/BE/BZ), not just the index: needed later for
    # survivorship-free backtests and for peers outside the index.
    """
    CREATE TABLE IF NOT EXISTS prices_daily (
        isin          VARCHAR NOT NULL,
        date          DATE NOT NULL,
        symbol        VARCHAR NOT NULL,
        series        VARCHAR NOT NULL,
        open          DOUBLE,
        high          DOUBLE,
        low           DOUBLE,
        close         DOUBLE NOT NULL,
        last          DOUBLE,
        prev_close    DOUBLE,           -- exchange's base price; adjusted on ex-dates
        volume        BIGINT,
        traded_value  DOUBLE,           -- rupees
        trades        BIGINT,
        source        VARCHAR NOT NULL, -- nse_bhav_legacy | nse_bhav_udiff
        PRIMARY KEY (isin, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_daily (
        symbol        VARCHAR NOT NULL,
        series        VARCHAR NOT NULL,
        date          DATE NOT NULL,
        delivery_qty  BIGINT,
        delivery_pct  DOUBLE,
        source        VARCHAR NOT NULL,
        PRIMARY KEY (symbol, series, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_prices_daily (
        index_name  VARCHAR NOT NULL,   -- upper-cased, e.g. 'NIFTY SMALLCAP 100'
        date        DATE NOT NULL,
        open        DOUBLE,
        high        DOUBLE,
        low         DOUBLE,
        close       DOUBLE NOT NULL,
        pe          DOUBLE,
        pb          DOUBLE,
        div_yield   DOUBLE,
        source      VARCHAR NOT NULL,
        PRIMARY KEY (index_name, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS corp_actions (
        isin        VARCHAR NOT NULL,
        symbol      VARCHAR NOT NULL,
        ex_date     DATE NOT NULL,
        action      VARCHAR NOT NULL,  -- split | consolidation | bonus | dividend | rights
                                       -- | demerger | buyback | other
        factor      DOUBLE,            -- price multiplier for dates before ex_date (split/bonus)
        amount      DOUBLE,            -- per-share cash (dividends) or rights premium
        subject     VARCHAR NOT NULL,  -- raw text from the exchange
        source      VARCHAR NOT NULL,
        PRIMARY KEY (isin, ex_date, action, subject)
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
    CREATE TABLE IF NOT EXISTS macro (
        series  VARCHAR NOT NULL,
        date    DATE NOT NULL,
        value   DOUBLE NOT NULL,
        source  VARCHAR NOT NULL,
        PRIMARY KEY (series, date)
    )
    """,
    # ------------------------------------------------------------------ derived
    """
    CREATE TABLE IF NOT EXISTS security_master (
        security_id  VARCHAR NOT NULL,   -- latest ISIN in the chain
        isin         VARCHAR NOT NULL PRIMARY KEY,
        symbol       VARCHAR NOT NULL,   -- last symbol seen for this ISIN
        first_date   DATE NOT NULL,
        last_date    DATE NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_events (
        security_id  VARCHAR NOT NULL,
        ex_date      DATE NOT NULL,
        factor       DOUBLE NOT NULL,
        source       VARCHAR NOT NULL,   -- corp_action | implied_prev_close | manual
        note         VARCHAR,
        PRIMARY KEY (security_id, ex_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices_adjusted (
        security_id   VARCHAR NOT NULL,
        date          DATE NOT NULL,
        isin          VARCHAR NOT NULL,
        symbol        VARCHAR NOT NULL,
        close         DOUBLE NOT NULL,
        adj_factor    DOUBLE NOT NULL,
        adj_close     DOUBLE NOT NULL,
        adj_volume    DOUBLE,
        traded_value  DOUBLE,
        ret_1d        DOUBLE,
        PRIMARY KEY (security_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_anomalies (
        security_id  VARCHAR NOT NULL,
        date         DATE NOT NULL,
        kind         VARCHAR NOT NULL,   -- large_move | factor_mismatch | unmatched_base_adjustment
        severity     VARCHAR NOT NULL,   -- warn | error
        value        DOUBLE,
        note         VARCHAR
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
        dataset      VARCHAR NOT NULL,   -- bhavcopy | delivery | indices | corp_actions
        key          VARCHAR NOT NULL,   -- e.g. an ISO date or a date range
        status       VARCHAR NOT NULL,   -- ok | missing | error
        rows         INTEGER,
        message      VARCHAR,
        ingested_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
        PRIMARY KEY (dataset, key)
    )
    """,
]


def connect(db_path: Path = DB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


def _current_version(con: duckdb.DuckDBPyConnection) -> int | None:
    has_meta = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'meta'"
    ).fetchone()[0]
    if not has_meta:
        return None
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else None


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    version = _current_version(con)
    if version is not None and version < 2:
        for t in _CHANGED_IN_V2 + ["ingest_log"]:
            con.execute(f'DROP TABLE IF EXISTS "{t}"')
    for stmt in DDL:
        con.execute(stmt)
    con.execute(
        "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)]
    )


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    return sorted(r[0] for r in con.execute("SHOW TABLES").fetchall())


def upsert_df(
    con: duckdb.DuckDBPyConnection,
    table: str,
    df,
    key_cols: list[str],
    delete_first: bool = True,
) -> int:
    """Delete rows matching df's keys (unless the caller already did), then insert df."""
    if df is None or len(df) == 0:
        return 0
    con.register("_upsert_src", df)
    try:
        if delete_first:
            on = " AND ".join(f't."{k}" = s."{k}"' for k in key_cols)
            con.execute(
                f'DELETE FROM "{table}" t WHERE EXISTS (SELECT 1 FROM _upsert_src s WHERE {on})'
            )
        cols = ", ".join(f'"{c}"' for c in df.columns)
        con.execute(f'INSERT INTO "{table}" ({cols}) SELECT {cols} FROM _upsert_src')
    finally:
        con.unregister("_upsert_src")
    return len(df)


def log_ingest(
    con: duckdb.DuckDBPyConnection,
    dataset: str,
    key: str,
    status: str,
    rows: int | None = None,
    message: str | None = None,
) -> None:
    con.execute(
        "INSERT OR REPLACE INTO ingest_log (dataset, key, status, rows, message, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, now())",
        [dataset, key, status, rows, message],
    )


def done_keys(con: duckdb.DuckDBPyConnection, dataset: str) -> dict[str, str]:
    """key -> status for everything already attempted for a dataset."""
    rows = con.execute(
        "SELECT key, status FROM ingest_log WHERE dataset = ?", [dataset]
    ).fetchall()
    return dict(rows)
