# Equity Research Engine

Reproducible, public-data research reports for the **Nifty Smallcap 100**. Every number traces
back to an exchange filing or price file, and every assumption lives in `config/`.

> **Disclaimer.** Educational and analytical use only. This is not investment advice. The author
> is not a SEBI-registered Research Analyst. Reports show valuation *ranges and their assumptions*,
> never ratings or target prices.

## Status

| Milestone | State |
|---|---|
| M0 Skeleton, config, universe, schema, CLI, CI | ✅ done |
| M1 Prices, index closes, delivery %, corporate actions, adjusted prices | ✅ code done, first full download pending |
| M2 Fundamentals (results XBRL) | next |
| M2–M7 | see [docs/PLAN.md](docs/PLAN.md) |

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
uv run ere config check          # validate config/*.yaml
uv run ere universe summary      # 100 stocks by industry and valuation path
uv run ere db sync-universe      # create data/ere.duckdb and load the universe
uv run ere db status
uv run pytest
```

## Loading prices (M1)

```bash
uv run ere db sync-universe
uv run ere ingest prices --start 2016-01-01     # ~1 hour first time; resumable (Ctrl+C is safe)
uv run ere ingest corp-actions --start 2016-01-01
uv run ere build prices                          # universe only; --scope all for whole market
uv run ere check prices                          # coverage + anomalies -> data/processed/*.csv
```

Reruns only fetch new dates, so a weekly `ere ingest prices` takes seconds.
`--offline` rebuilds the database from the raw files in `data/raw/` without the network.

**How adjustment works.** ISINs change on face-value splits, so `security_master` chains old
and new ISINs into one security. Split and bonus factors come from NSE corporate actions and
are cross-checked against the exchange's own adjusted base price (`prev_close`) on the ex-date.
Base-price adjustments with no matching corporate action (rights, demergers) are applied and
flagged. Demergers you have investigated go in `config/price_adjustments.yaml`.
`adj_close` is a split/bonus-adjusted *price* series; dividends are not reinvested.

## Layout

```
config/              universe.yaml, valuation.yaml, xbrl_mapping.yaml, reference/ (NSE constituent CSV)
src/ere/
  cli.py             `ere` command
  config.py          typed config loaders (pydantic)
  universe.py        constituents + overrides -> Security objects
  db.py              DuckDB schema (point-in-time: every fact has source + filing_date)
  http.py            polite NSE/BSE client (cookie priming, throttling, retries)
  ingest/ clean/ analytics/ valuation/ report/   filled in M1-M5
tests/               unit tests (golden-value tests arrive in M2)
docs/PLAN.md         decisions, milestones, risks
```

## Valuation paths

Set per stock in `config/universe.yaml`:

| Path | Used for | Methods |
|---|---|---|
| `dcf` | operating companies (default) | 3-stage FCFF DCF, EV/EBITDA, P/E, reverse DCF |
| `residual_income` | banks, NBFCs, HFCs | excess-return model, P/B vs ROE |
| `insurance` | general insurers | residual income on book, P/B, P/E |
| `sotp` | holding companies | listed stakes at market less holdco discount |
| `ev_sales` | loss-making / pre-profit | EV/Sales vs peers, reverse DCF (what growth is priced in) |

## Data sources

The NSE constituent list, NSE bhavcopy archives, NSE/BSE corporate announcements (results and
shareholding XBRL, corporate actions), and RBI DBIE for the G-Sec yield. The tool downloads
only public files, at a polite rate. Scraping sites whose terms forbid it is out of scope.

## Licence

MIT
