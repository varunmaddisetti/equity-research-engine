# Equity Research Engine

[![ci](https://github.com/varunmaddisetti/equity-research-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/varunmaddisetti/equity-research-engine/actions)

Reproducible, public-data research reports for the **Nifty Smallcap 100**. Every number traces
back to an exchange filing or price file, and every assumption lives in `config/`.

> **Disclaimer.** Educational and analytical use only. This is not investment advice. The author
> is not a SEBI-registered Research Analyst. Reports show valuation *ranges and their assumptions*,
> never ratings or target prices.

## Status

| Milestone | State |
|---|---|
| M0 Skeleton, config, universe, schema, CLI, CI | ✅ done |
| M1 Prices, index closes, delivery %, corporate actions, adjusted prices | ✅ 10.7 years loaded (2,658 sessions); anomaly review in progress |
| M2 Fundamentals from results XBRL (2018 onwards) | ✅ code done, first full download pending |
| M3 Ratios, quality flags, shareholding, beta | next |
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

## Loading fundamentals (M2)

```bash
uv run ere ingest filings        # list every results filing with XBRL (~5 min)
uv run ere ingest xbrl           # download + parse them (~1 hour first time; resumable)
uv run ere build financials      # map XBRL elements to standard fields (seconds)
uv run ere check financials      # coverage, accounting identities, unmapped elements
uv run ere check golden          # compare with figures typed from annual reports
```

`ere ingest financials` runs the first three in one go.

**Sources.** NSE's financial-results filings (XBRL from 2018 to early 2025, `in-bse-fin`
taxonomy) and SEBI Integrated Filing – Financials (from Q4 FY25, `in-capmkt` taxonomy).
Results before 2018 have no machine-readable version, so fundamentals start in 2018.
Every value keeps the date it was filed; revisions are extra rows, never overwrites, so
`fundamentals(as_of=...)` only returns what the market could see on that date.

**Golden test.** `config/golden/golden_fy25.csv` lists FY25 figures for five stocks covering
every valuation path (KAYNES, NATCOPHARM, CDSL, KARURVYSYA, MANAPPURAM). Type the numbers from
each annual report (Rs crore) with the page number, then run `ere check golden`.

**How adjustment works.** ISINs change on face-value splits, so `security_master` chains old
and new ISINs into one security. Factors come from, in order: manual entries in
`config/price_adjustments.yaml`, splits/bonuses on NSE's corporate-action record, splits inferred
where the ISIN changed but nothing is on record (snapped to a face-value ratio such as 1/5), and
demergers on record (factor approximated from the price drop). Every applied event is verified
against the price series, and unexplained moves over 25% are flagged. NSE's bhavcopy
`PREVCLOSE` is the raw previous close, so it cannot be used for this. `adj_close` is a
split/bonus/demerger-adjusted *price* series; dividends are not reinvested.

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
