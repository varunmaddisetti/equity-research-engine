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
from ere.paths import CONFIG_DIR, DB_PATH
from ere.universe import load_universe, read_constituents, universe_frame

app = typer.Typer(help="Equity Research Engine - Nifty Smallcap 100", no_args_is_help=True)
universe_app = typer.Typer(help="Inspect and refresh the stock universe", no_args_is_help=True)
db_app = typer.Typer(help="Manage the DuckDB warehouse", no_args_is_help=True)
config_app = typer.Typer(help="Validate configuration", no_args_is_help=True)
ingest_app = typer.Typer(help="Download public data (M1-M3)", no_args_is_help=True)
app.add_typer(universe_app, name="universe")
app.add_typer(db_app, name="db")
app.add_typer(config_app, name="config")
app.add_typer(ingest_app, name="ingest")

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
    """Row counts per table."""
    if not DB_PATH.exists():
        raise typer.Exit("no database yet - run `ere db init`")
    with connect(read_only=True) as con:
        rows = [(t, con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0])
                for t in list_tables(con)]
    console.print(pd.DataFrame(rows, columns=["table", "rows"]).to_string(index=False))


# ------------------------------------------------------------------ ingest (later milestones)
def _not_yet(milestone: str) -> None:
    console.print(f"[yellow]Not implemented yet - planned for {milestone}. See docs/PLAN.md.[/]")
    raise typer.Exit(code=2)


@ingest_app.command("prices")
def ingest_prices(
    start: str = typer.Option("2016-01-01", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD, default today"),
) -> None:
    """NSE bhavcopy (legacy + UDiFF formats) -> prices_daily."""
    _not_yet("M1")


@ingest_app.command("corp-actions")
def ingest_corp_actions() -> None:
    """Splits, bonuses, dividends -> corp_actions, then price adjustment."""
    _not_yet("M1")


@ingest_app.command("financials")
def ingest_financials() -> None:
    """Results XBRL (consolidated + standalone) -> financials."""
    _not_yet("M2")


@ingest_app.command("shareholding")
def ingest_shareholding() -> None:
    """Quarterly shareholding pattern XBRL -> shareholding."""
    _not_yet("M3")


if __name__ == "__main__":
    app()
