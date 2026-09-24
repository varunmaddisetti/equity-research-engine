"""Command-line entry point: `ere --help`."""

from __future__ import annotations

from datetime import date

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from ere import __version__
from ere.config import load_universe_config, load_valuation_config
from ere.db import connect, init_db, list_tables
from ere.paths import CONFIG_DIR, DB_PATH, PROCESSED_DIR, RAW_DIR
from ere.universe import load_universe, read_constituents, universe_frame

app = typer.Typer(help="Equity Research Engine - Nifty Smallcap 100", no_args_is_help=True)
universe_app = typer.Typer(help="Inspect and refresh the stock universe", no_args_is_help=True)
db_app = typer.Typer(help="Manage the DuckDB warehouse", no_args_is_help=True)
config_app = typer.Typer(help="Validate configuration", no_args_is_help=True)
ingest_app = typer.Typer(help="Download public data", no_args_is_help=True)
build_app = typer.Typer(help="Build derived tables from raw data", no_args_is_help=True)
check_app = typer.Typer(help="Data-quality checks", no_args_is_help=True)
app.add_typer(universe_app, name="universe")
app.add_typer(db_app, name="db")
app.add_typer(config_app, name="config")
app.add_typer(ingest_app, name="ingest")
app.add_typer(build_app, name="build")
app.add_typer(check_app, name="check")

console = Console()


@app.command()
def version() -> None:
    """Print the package version."""
    console.print(__version__)


# ------------------------------------------------------------------ config
@config_app.command("check")
def config_check() -> None:
    """Load and validate every config file."""
    val = load_valuation_config()
    uni = load_universe_config()
    stocks = load_universe()
    coc = val.cost_of_capital
    console.print("[green]valuation.yaml OK[/] ", end="")
    console.print(
        f"rf={coc.risk_free_rate:.2%} ERP={coc.equity_risk_premium:.2%} "
        f"size={coc.size_premium:.2%} -> Ke at beta 1 = {coc.cost_of_equity(1.0):.2%}"
    )
    console.print(f"[green]universe.yaml OK[/] {uni.index_name}: {len(stocks)} stocks")


# ------------------------------------------------------------------ universe
@universe_app.command("show")
def universe_show(
    model: str | None = typer.Option(None, help="Filter by valuation model, e.g. residual_income"),
    industry: str | None = typer.Option(None, help="Case-insensitive industry substring"),
) -> None:
    """List the universe with each stock's valuation path."""
    df = universe_frame()
    if model:
        df = df[df.valuation_model == model]
    if industry:
        df = df[df.industry.str.contains(industry, case=False)]
    t = Table(title=f"{len(df)} securities")
    for col in ("symbol", "name", "industry", "valuation_model", "short_history"):
        t.add_column(col)
    for r in df.itertuples(index=False):
        t.add_row(r.symbol, r.name, r.industry, r.valuation_model, "yes" if r.short_history else "")
    console.print(t)


@universe_app.command("summary")
def universe_summary() -> None:
    """Counts by industry and by valuation model."""
    df = universe_frame()
    console.print(df.groupby("valuation_model").size().rename("stocks").to_frame())
    console.print(df.groupby("industry").size().sort_values(ascending=False).rename("stocks"))


@universe_app.command("refresh")
def universe_refresh(
    write: bool = typer.Option(False, "--write", help="Overwrite the CSV if it changed"),
) -> None:
    """Re-download the constituent list from NSE and show additions/removals."""
    from ere.http import ExchangeClient

    cfg = load_universe_config()
    path = CONFIG_DIR / cfg.constituents_csv
    with ExchangeClient(prime_url=None) as client:
        body = client.get_bytes(cfg.constituents_url)
    if body is None:
        raise typer.Exit("constituent CSV not found at NSE (404)")
    tmp = path.with_suffix(".new.csv")
    tmp.write_bytes(body)
    new = read_constituents(tmp)
    old = read_constituents(path)
    added = sorted(set(new.symbol) - set(old.symbol))
    removed = sorted(set(old.symbol) - set(new.symbol))
    console.print(f"added: {added or 'none'}\nremoved: {removed or 'none'}")
    if write and (added or removed):
        tmp.replace(path)
        console.print(
            f"[yellow]wrote {path.name}. Update snapshot_date and overrides in universe.yaml, "
            "then run `ere db sync-universe`.[/]"
        )
    else:
        tmp.unlink()


# ------------------------------------------------------------------ db
@db_app.command("init")
def db_init() -> None:
    """Create all tables (safe to re-run)."""
    with connect() as con:
        init_db(con)
        console.print(f"[green]initialised[/] {DB_PATH}: {', '.join(list_tables(con))}")


@db_app.command("sync-universe")
def db_sync_universe() -> None:
    """Write the universe into `securities` and record index membership changes."""
    cfg = load_universe_config()
    df = universe_frame()
    today = date.today()
    with connect() as con:
        init_db(con)
        con.register("u", df)
        con.execute("UPDATE securities SET in_index = FALSE")
        con.execute(
            """
            INSERT INTO securities
                (isin, symbol, name, industry, valuation_model, short_history, in_index, updated_at)
            SELECT isin, symbol, name, industry, valuation_model, short_history, TRUE, now()
            FROM u
            ON CONFLICT (isin) DO UPDATE SET
                symbol = excluded.symbol,
                name = excluded.name,
                industry = excluded.industry,
                valuation_model = excluded.valuation_model,
                short_history = excluded.short_history,
                in_index = TRUE,
                updated_at = excluded.updated_at
            """
        )
        # Close memberships for ISINs that left, open ones for ISINs that joined.
        con.execute(
            """
            UPDATE index_membership SET to_date = ?
            WHERE index_name = ? AND to_date IS NULL AND isin NOT IN (SELECT isin FROM u)
            """,
            [today, cfg.index_name],
        )
        con.execute(
            """
            INSERT INTO index_membership (index_name, symbol, isin, from_date, to_date)
            SELECT ?, symbol, isin, ?, NULL FROM u
            WHERE isin NOT IN (
                SELECT isin FROM index_membership WHERE index_name = ? AND to_date IS NULL
            )
            """,
            [cfg.index_name, cfg.snapshot_date, cfg.index_name],
        )
        n = con.execute("SELECT count(*) FROM securities WHERE in_index").fetchone()[0]
    console.print(f"[green]synced[/] {n} securities in index")




@db_app.command("status")
def db_status() -> None:
    """Row counts per table, plus what the ingesters have covered."""
    if not DB_PATH.exists():
        raise typer.Exit("no database yet - run `ere db init`")
    with connect(read_only=True) as con:
        rows = [(t, con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0])
                for t in list_tables(con)]
        console.print(pd.DataFrame(rows, columns=["table", "rows"]).to_string(index=False))
        log = con.execute(
            """
            SELECT dataset, status, count(*) AS n, min(key) AS first, max(key) AS last
            FROM ingest_log GROUP BY dataset, status ORDER BY dataset, status
            """
        ).df()
    if len(log):
        console.print("\n[bold]ingest log[/]")
        console.print(log.to_string(index=False))


# ------------------------------------------------------------------ ingest
def _parse_day(s: str | None, default: date) -> date:
    return date.fromisoformat(s) if s else default


@ingest_app.command("prices")
def ingest_prices(
    start: str = typer.Option("2016-01-01", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD, default today"),
    delivery: bool = typer.Option(True, help="Also fetch delivery % (sec_bhavdata_full)"),
    offline: bool = typer.Option(False, help="Rebuild from data/raw only, no network"),
    force: bool = typer.Option(False, help="Re-process dates already marked done"),
    interval: float = typer.Option(0.4, help="Seconds between requests (be polite)"),
) -> None:
    """NSE bhavcopy (legacy + UDiFF), index closes and delivery % -> DuckDB.

    Resumable: stop with Ctrl+C any time and rerun; finished dates are skipped.
    """
    from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

    from ere.http import ExchangeClient
    from ere.ingest.nse_daily import candidate_sessions
    from ere.ingest.runner import ingest_daily

    s, e = _parse_day(start, date(2016, 1, 1)), _parse_day(end, date.today())
    datasets = ["bhavcopy", "indices"] + (["delivery"] if delivery else [])
    total = len(candidate_sessions(s, e)) * len(datasets)
    client = None if offline else ExchangeClient(min_interval_s=interval, prime_url=None)
    try:
        with connect() as con, Progress(
            TextColumn("{task.description}"), BarColumn(),
            TextColumn("{task.completed}/{task.total}"), TimeRemainingColumn(),
            console=console,
        ) as prog:
            init_db(con)
            task = prog.add_task("starting", total=total)

            def tick(dataset: str, d: date, outcome: str) -> None:
                prog.update(task, advance=1, description=f"{dataset} {d} {outcome}")

            stats = ingest_daily(con, RAW_DIR, s, e, datasets, client=client,
                                 offline=offline, force=force, on_progress=tick)
            prog.update(task, completed=total, description="done")
    finally:
        if client:
            client.close()
    df = pd.Series(stats).rename("count").rename_axis(["dataset", "outcome"]).reset_index()
    console.print(df.pivot(index="dataset", columns="outcome", values="count").fillna(0)
                  .astype(int))
    if stats.get(("bhavcopy", "error")):
        console.print("[yellow]Some dates failed. Rerun the same command to retry them; "
                      "details in `ere db status` / ingest_log.[/]")


@ingest_app.command("corp-actions")
def ingest_corp_actions_cmd(
    start: str = typer.Option("2016-01-01", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD, default today + 90 days"),
    offline: bool = typer.Option(False, help="Rebuild from data/raw only"),
) -> None:
    """Splits, bonuses, dividends, rights, demergers (whole market) -> corp_actions."""
    from datetime import timedelta

    from ere.http import ExchangeClient
    from ere.ingest.corp_actions import ingest_corp_actions

    s = _parse_day(start, date(2016, 1, 1))
    e = _parse_day(end, date.today() + timedelta(days=90))
    client = None if offline else ExchangeClient(min_interval_s=1.0)
    try:
        with connect() as con:
            init_db(con)
            stats = ingest_corp_actions(con, RAW_DIR, s, e, client=client, offline=offline)
            by = con.execute(
                "SELECT action, count(*) n FROM corp_actions GROUP BY action ORDER BY n DESC"
            ).df()
    finally:
        if client:
            client.close()
    console.print(stats)
    console.print(by.to_string(index=False))


@ingest_app.command("financials")
def ingest_financials() -> None:
    """Results XBRL (consolidated + standalone) -> financials."""
    _not_yet("M2")


@ingest_app.command("shareholding")
def ingest_shareholding() -> None:
    """Quarterly shareholding pattern XBRL -> shareholding."""
    _not_yet("M3")


def _not_yet(milestone: str) -> None:
    console.print(f"[yellow]Not implemented yet - planned for {milestone}. See docs/PLAN.md.[/]")
    raise typer.Exit(code=2)


# ------------------------------------------------------------------ build / check
@build_app.command("prices")
def build_prices(
    scope: str = typer.Option("universe", help="universe (fast) or all (whole NSE main board)"),
) -> None:
    """Chain ISINs, apply split/bonus factors, compute adjusted prices and anomalies."""
    from ere.clean.adjust_prices import build_adjusted_prices

    with connect() as con:
        init_db(con)
        stats = build_adjusted_prices(con, CONFIG_DIR / "price_adjustments.yaml", scope=scope)
    console.print(stats)
    if stats.anomalies_warn or stats.anomalies_error:
        console.print("Run `ere check prices` to review anomalies.")


@check_app.command("prices")
def check_prices(
    symbol: str | None = typer.Option(None, help="Show every anomaly for one symbol"),
) -> None:
    """Coverage and anomalies for the universe. Writes data/processed/price_checks.csv."""
    with connect(read_only=True) as con:
        coverage = con.execute(
            """
            SELECT s.symbol, s.short_history,
                   min(a.date) AS first_date, max(a.date) AS last_date, count(a.date) AS days,
                   count(DISTINCT a.isin) AS isins,
                   (SELECT count(*) FROM price_events e WHERE e.security_id = s.isin) AS events,
                   (SELECT count(*) FROM price_anomalies x
                     WHERE x.security_id = s.isin AND x.severity = 'error') AS errors,
                   (SELECT count(*) FROM price_anomalies x
                     WHERE x.security_id = s.isin AND x.severity = 'warn') AS warns
            FROM securities s LEFT JOIN prices_adjusted a ON a.security_id = s.isin
            WHERE s.in_index
            GROUP BY s.symbol, s.isin, s.short_history
            ORDER BY errors DESC, warns DESC, s.symbol
            """
        ).df()
        detail = con.execute(
            """
            SELECT m.symbol, x.date, x.kind, x.severity, round(x.value, 4) AS value, x.note
            FROM price_anomalies x
            JOIN (SELECT DISTINCT security_id, last_value(symbol) OVER (
                    PARTITION BY security_id ORDER BY last_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS symbol
                  FROM security_master) m USING (security_id)
            ORDER BY m.symbol, x.date
            """
        ).df()
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(PROCESSED_DIR / "price_checks.csv", index=False)
    detail.to_csv(PROCESSED_DIR / "price_anomalies.csv", index=False)

    if symbol:
        console.print(detail[detail.symbol == symbol].to_string(index=False) or "no anomalies")
        return
    no_data = coverage[coverage.days == 0]
    console.print(coverage.head(25).to_string(index=False))
    console.print(
        f"\n{len(coverage)} stocks | no price data: {len(no_data)} | "
        f"with errors: {(coverage.errors > 0).sum()} | with warnings: {(coverage.warns > 0).sum()}"
    )
    console.print(f"Full tables: {PROCESSED_DIR / 'price_checks.csv'} and price_anomalies.csv")


if __name__ == "__main__":
    app()
