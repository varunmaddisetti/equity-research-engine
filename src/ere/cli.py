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


def _universe_pairs(con, symbols: str | None) -> list[tuple[str, str]]:
    rows = con.execute(
        "SELECT symbol, isin FROM securities WHERE in_index ORDER BY symbol").fetchall()
    if not rows:
        raise typer.Exit("universe not loaded - run `ere db sync-universe` first")
    if symbols:
        wanted = {s.strip().upper() for s in symbols.split(",")}
        rows = [r for r in rows if r[0] in wanted]
    return rows


@ingest_app.command("filings")
def ingest_filings_cmd(
    symbols: str | None = typer.Option(None, help="Comma-separated, default: whole universe"),
    offline: bool = typer.Option(False, help="Use cached index files only"),
) -> None:
    """List every results filing with an XBRL file (2018 onwards) -> filings."""
    from ere.http import ExchangeClient
    from ere.ingest.filings import ingest_filing_index

    client = None if offline else ExchangeClient(min_interval_s=1.0)
    try:
        with connect() as con:
            init_db(con)
            pairs = _universe_pairs(con, symbols)
            with console.status("fetching filing indexes") as st:
                stats = ingest_filing_index(
                    con, RAW_DIR, pairs, client=client, offline=offline,
                    on_progress=lambda s: st.update(f"filing index: {s}"))
            summary = con.execute(
                "SELECT source, basis, count(*) AS n, min(period_end) AS first_period, "
                "max(period_end) AS last_period "
                "FROM filings GROUP BY 1, 2 ORDER BY 1, 2").df()
    finally:
        if client:
            client.close()
    console.print(stats)
    console.print(summary.to_string(index=False))


@ingest_app.command("xbrl")
def ingest_xbrl_cmd(
    symbols: str | None = typer.Option(None, help="Comma-separated, default: all pending"),
    offline: bool = typer.Option(False, help="Parse cached files only"),
    retry_errors: bool = typer.Option(False, help="Also retry filings that failed before"),
    interval: float = typer.Option(0.5, help="Seconds between downloads"),
) -> None:
    """Download and parse pending XBRL filings -> xbrl_facts. Resumable."""
    from ere.http import ExchangeClient
    from ere.ingest.xbrl import ingest_xbrl

    client = None if offline else ExchangeClient(min_interval_s=interval, prime_url=None)
    sym_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    try:
        with connect() as con:
            init_db(con)
            n = con.execute("SELECT count(*) FROM filings WHERE status = 'pending'").fetchone()[0]
            with console.status(f"{n} filings pending") as st:
                stats = ingest_xbrl(
                    con, RAW_DIR, client=client, offline=offline, symbols=sym_list,
                    retry_errors=retry_errors,
                    on_progress=lambda s, f: st.update(f"parsed {s} {f}"))
    finally:
        if client:
            client.close()
    console.print(stats)


@ingest_app.command("financials")
def ingest_financials(
    symbols: str | None = typer.Option(None, help="Comma-separated, default: whole universe"),
) -> None:
    """All of M2 in one go: filings index, XBRL download/parse, build financials."""
    ingest_filings_cmd(symbols=symbols, offline=False)
    ingest_xbrl_cmd(symbols=symbols, offline=False, retry_errors=False, interval=0.5)
    build_financials_cmd()


@ingest_app.command("shareholding")
def ingest_shareholding(
    symbols: str | None = typer.Option(None, help="Comma-separated, default: whole universe"),
    offline: bool = typer.Option(False, help="Use cached files only"),
) -> None:
    """Promoter / public holding by quarter and promoter pledges -> shareholding."""
    from ere.http import ExchangeClient
    from ere.ingest.shareholding import ingest_shareholding as run

    client = None if offline else ExchangeClient(min_interval_s=1.0)
    try:
        with connect() as con:
            init_db(con)
            pairs = _universe_pairs(con, symbols)
            with console.status("shareholding") as st:
                stats = run(con, RAW_DIR, pairs, client=client, offline=offline,
                            on_progress=lambda s: st.update(f"shareholding: {s}"))
    finally:
        if client:
            client.close()
    console.print(stats)


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


@build_app.command("financials")
def build_financials_cmd() -> None:
    """Map XBRL facts to standard fields (rerun after editing config/xbrl_mapping.yaml)."""
    from ere.clean.financials import build_financials

    with connect() as con:
        init_db(con)
        stats = build_financials(con, CONFIG_DIR / "xbrl_mapping.yaml")
    console.print(stats)


@build_app.command("analytics")
def build_analytics_cmd(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD, default: last trading day"),
) -> None:
    """Risk, liquidity, ratios, multiples, shareholding trend, quality flags, peers."""
    from ere.analytics.build import build_analytics

    with connect() as con:
        init_db(con)
        stats = build_analytics(con, load_valuation_config(), load_universe_config(),
                                date.fromisoformat(as_of) if as_of else None)
    console.print(stats)


@build_app.command("valuation")
def build_valuation_cmd(
    history_years: int | None = typer.Option(None, help="Years of history for multiple bands"),
) -> None:
    """DCF / residual income / SOTP / multiples for every stock -> valuations."""
    from ere.valuation.build import build_valuation

    with connect() as con:
        init_db(con)
        with console.status("valuing (the multiple history takes a few minutes)"):
            stats = build_valuation(con, load_valuation_config(), load_universe_config(),
                                    CONFIG_DIR / "sotp.yaml", history_years)
    console.print(stats)


@app.command()
def report(
    symbol: str | None = typer.Argument(None, help="One symbol; omit with --all"),
    all_: bool = typer.Option(False, "--all", help="Every stock plus index.html"),
    out: str = typer.Option("reports", help="Output folder"),
    pdf: bool = typer.Option(False, help="Also write PDFs (needs the [pdf] extra)"),
    repo_url: str = typer.Option("https://github.com/varunmaddisetti/equity-research-engine",
                                 help="Linked from the index page"),
) -> None:
    """Render self-contained HTML research reports."""
    from pathlib import Path

    from ere.report.build import build_reports

    if not symbol and not all_:
        raise typer.Exit("give a SYMBOL or --all")
    out_dir = Path(out) if Path(out).is_absolute() else CONFIG_DIR.parent / out
    with connect(read_only=True) as con, console.status("rendering reports"):
        stats = build_reports(con, out_dir, load_valuation_config(),
                              None if all_ else [symbol.upper()], repo_url)
    console.print(stats)
    if stats["errors"]:
        console.print(f"[yellow]See {out_dir / '_errors.txt'} for the failures.[/]")
    if pdf:
        try:
            from weasyprint import HTML
        except ImportError:
            raise typer.Exit("PDF export needs: uv pip install -e '.[pdf]' (and pango)") from None
        for f in sorted(out_dir.glob("*.html")):
            if f.name != "index.html":
                HTML(filename=str(f)).write_pdf(str(f.with_suffix(".pdf")))
        console.print("PDFs written next to the HTML files.")
    target = out_dir / ("index.html" if all_ else f"{symbol.upper()}.html")
    console.print(f"Open: {target}")


REPO_URL = "https://github.com/varunmaddisetti/equity-research-engine"


@app.command()
def publish(
    site: str = typer.Option("reports", help="Folder with the rendered HTML"),
    push: bool = typer.Option(True, help="--no-push builds the gh-pages commit only"),
) -> None:
    """Publish the rendered reports to the gh-pages branch (served by GitHub Pages)."""
    from pathlib import Path

    from ere.publish import publish_to_gh_pages

    site_dir = Path(site) if Path(site).is_absolute() else CONFIG_DIR.parent / site
    sha = publish_to_gh_pages(CONFIG_DIR.parent, site_dir, push=push)
    console.print(f"gh-pages at {sha[:10]}" + (" (pushed)" if push else " (not pushed)"))
    if push:
        console.print("First time only: GitHub -> Settings -> Pages -> Deploy from a branch -> "
                      "gh-pages / (root). The site appears at "
                      "https://varunmaddisetti.github.io/equity-research-engine/")


@app.command()
def refresh(
    download: bool = typer.Option(True, help="--no-download rebuilds from local data only"),
    publish_site: bool = typer.Option(False, "--publish", help="Push reports to gh-pages"),
) -> None:
    """The weekly job: new data -> adjusted prices -> fundamentals -> analytics -> valuation
    -> reports (-> publish). Safe to rerun; every download step is incremental."""
    from datetime import timedelta

    from ere.publish import publish_to_gh_pages, run_steps

    recent = (date.today() - timedelta(days=400)).isoformat()
    steps = []
    if download:
        steps += [
            ("prices", lambda: ingest_prices(start="2016-01-01", end=None, delivery=True,
                                             offline=False, force=False, interval=0.4), True),
            ("corporate actions", lambda: ingest_corp_actions_cmd(start=recent, end=None,
                                                                  offline=False), False),
        ]
    steps.append(("adjusted prices", lambda: build_prices(scope="universe"), True))
    if download:
        steps += [
            ("filing index", lambda: ingest_filings_cmd(symbols=None, offline=False), False),
            ("xbrl", lambda: ingest_xbrl_cmd(symbols=None, offline=False, retry_errors=False,
                                             interval=0.5), False),
            ("shareholding", lambda: ingest_shareholding(symbols=None, offline=False), False),
        ]
    steps += [
        ("financials", lambda: build_financials_cmd(), False),
        ("analytics", lambda: build_analytics_cmd(as_of=None), True),
        ("valuation", lambda: build_valuation_cmd(history_years=None), True),
        ("reports", lambda: report(symbol=None, all_=True, out="reports", pdf=False,
                                   repo_url=REPO_URL), True),
    ]
    if publish_site:
        steps.append(("publish", lambda: publish_to_gh_pages(
            CONFIG_DIR.parent, CONFIG_DIR.parent / "reports"), True))
    rep = run_steps(steps)
    log_dir = DB_PATH.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"refresh_{date.today():%Y%m%d}.txt"
    log.write_text(rep.summary() + "\n")
    console.print("\n[bold]Refresh summary[/]\n" + rep.summary())
    console.print(f"log: {log}")
    if not rep.ok:
        raise typer.Exit(code=1)


@app.command()
def show(symbol: str) -> None:
    """Print the analytics snapshot, flags and valuation ranges for one stock."""
    from ere.analytics.build import latest_metrics
    from ere.valuation.build import football_field

    symbol = symbol.upper()
    with connect(read_only=True) as con:
        row = con.execute("SELECT isin, name, valuation_model FROM securities WHERE symbol = ?",
                          [symbol]).fetchone()
        if not row:
            raise typer.Exit(f"{symbol} not in the universe")
        isin, name, model = row
        lm = latest_metrics(con)
        flags = con.execute(
            "SELECT flag, triggered, round(value, 3) AS value, threshold, note FROM quality_flags"
            " WHERE isin = ? AND as_of = (SELECT max(as_of) FROM quality_flags) ORDER BY flag",
            [isin]).df()
        ff = football_field(con, isin) if con.execute(
            "SELECT count(*) FROM valuations WHERE isin = ?", [isin]).fetchone()[0] else None
    console.print(f"[bold]{symbol}[/] {name} - valuation path: {model}")
    if len(lm) and isin in set(lm["isin"]):
        m = lm[lm["isin"] == isin].drop(columns=["isin", "symbol"]).T.dropna()
        m.columns = ["value"]
        console.print(m.to_string())
    if len(flags):
        console.print("\n[bold]Quality flags[/]")
        console.print(flags.to_string(index=False))
    if ff is not None and len(ff):
        console.print("\n[bold]Valuation ranges (Rs per share)[/]")
        console.print(ff.round(1).to_string(index=False))


@check_app.command("financials")
def check_financials() -> None:
    """Coverage, accounting identities and unmapped elements -> data/processed/*.csv"""
    from ere.clean.fin_checks import coverage, identity_failures, unmapped_elements
    from ere.clean.financials import fundamentals

    with connect(read_only=True) as con:
        wide = fundamentals(con)
        cov = coverage(con, wide)
        fails = identity_failures(wide) if len(wide) else pd.DataFrame()
        unmapped = unmapped_elements(con, CONFIG_DIR / "xbrl_mapping.yaml")
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    cov.to_csv(PROCESSED_DIR / "fin_coverage.csv", index=False)
    fails.to_csv(PROCESSED_DIR / "fin_identity_failures.csv", index=False)
    unmapped.to_csv(PROCESSED_DIR / "xbrl_unmapped_elements.csv", index=False)
    if len(wide):
        wide.to_csv(PROCESSED_DIR / "fundamentals_wide.csv", index=False)

    console.print("[bold]Weakest coverage[/] (fy_years / quarters with top line and PAT)")
    console.print(cov.head(15).to_string(index=False))
    if len(fails):
        console.print("\n[bold]Identity check failures[/]")
        console.print(fails.groupby("check").size().rename("n").to_string())
    console.print("\n[bold]Most frequent unmapped elements[/]")
    console.print(unmapped.head(15).to_string(index=False))
    console.print(
        f"\n{len(cov)} stocks | with >= 5 FY: {(cov.fy_years >= 5).sum()} | "
        f"no filings: {(cov.filings == 0).sum()} | failed downloads/parses: {cov.failed.sum()} | "
        f"identity failures: {len(fails)}")
    console.print(f"Full tables in {PROCESSED_DIR}: fin_coverage.csv, fin_identity_failures.csv, "
                  "xbrl_unmapped_elements.csv, fundamentals_wide.csv")


@check_app.command("golden")
def check_golden(
    path: str = typer.Option("config/golden/golden_fy25.csv", help="Hand-typed figures"),
) -> None:
    """Compare extracted figures with numbers typed from annual reports (0.5% tolerance)."""
    from pathlib import Path

    from ere.clean.fin_checks import golden_compare

    p = Path(path)
    if not p.is_absolute():
        p = CONFIG_DIR.parent / p
    with connect(read_only=True) as con:
        res = golden_compare(con, p)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    res.to_csv(PROCESSED_DIR / "golden_results.csv", index=False)
    filled = res[res.status != "blank"]
    if filled.empty:
        console.print("No figures filled in yet - add 'expected' values to the golden CSV.")
        return
    console.print(filled.to_string(index=False))
    counts = filled.status.value_counts()
    console.print(f"\npass: {counts.get('pass', 0)} / {len(filled)} filled "
                  f"| FAIL: {counts.get('FAIL', 0)} | other: "
                  f"{len(filled) - counts.get('pass', 0) - counts.get('FAIL', 0)}")


@check_app.command("prices")
def check_prices(
    symbol: str | None = typer.Option(None, help="Show every anomaly for one symbol"),
) -> None:
    """Coverage and anomalies for the universe. Writes data/processed/price_checks.csv."""
    with connect(read_only=True) as con:
        coverage = con.execute(
            """
            WITH cal AS (SELECT DISTINCT date FROM prices_daily)
            SELECT s.symbol, s.short_history,
                   min(a.date) AS first_date, max(a.date) AS last_date, count(a.date) AS days,
                   (SELECT count(*) FROM cal WHERE cal.date BETWEEN min(a.date) AND max(a.date))
                     - count(a.date) AS missing_sessions,
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
    if len(detail):
        kinds = detail.groupby(["kind", "severity"]).size().rename("n").reset_index()
        console.print(kinds.to_string(index=False) + "\n")
    no_data = coverage[coverage.days == 0]
    console.print(coverage.head(25).to_string(index=False))
    console.print(
        f"\n{len(coverage)} stocks | no price data: {len(no_data)} | "
        f"with errors: {(coverage.errors > 0).sum()} | "
        f"with warnings: {(coverage.warns > 0).sum()} | "
        f"with missing sessions: {(coverage.missing_sessions > 0).sum()}"
    )
    console.print(f"Full tables: {PROCESSED_DIR / 'price_checks.csv'} and price_anomalies.csv")


if __name__ == "__main__":
    app()
