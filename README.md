# Equity Research Engine

[![ci](https://github.com/varunmaddisetti/equity-research-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/varunmaddisetti/equity-research-engine/actions)

Reproducible, public-data research reports for the **Nifty Smallcap 100**. Every number traces
back to an exchange filing or price file, every assumption lives in `config/`, and every
report shows its own data gaps.

> **Disclaimer.** Educational and analytical use only. This is not investment advice. The author
> is not a SEBI-registered Research Analyst. Reports show valuation *ranges and their assumptions*,
> never ratings or target prices.

## What it does

```
NSE bhavcopy ──► prices_daily ──► security_master (ISIN chains) ──► prices_adjusted ─┐
NSE corp actions ────────────────────────► split / bonus / demerger factors ─────────┤
NSE results XBRL (2018-) + Integrated Filing ─► xbrl_facts ─► financials (point-in-time)┤
NSE shareholding + pledges ─► shareholding ─────────────────────────────────────────┤
                                                                                     ▼
                         analytics: risk, ratios, multiples, quality flags, peers
                                                                                     ▼
            valuation: DCF (+ sensitivity, reverse DCF) · residual income · SOTP · multiples
                                                                                     ▼
                              one HTML report per stock + index ─► GitHub Pages
```

## Status

| Milestone | State |
|---|---|
| M0 Skeleton, config, universe, schema, CLI, CI | ✅ |
| M1 Prices, corporate actions, adjusted prices | ✅ 10.7 years, 100 stocks, 0 unexplained anomalies |
| M2 Fundamentals from XBRL (2018 onwards) | ✅ code; first full run and golden test pending |
| M3 Analytics: risk, ratios, flags, peers, shareholding | ✅ code; first real run pending |
| M4 Valuation: DCF, residual income, SOTP, multiples | ✅ code; first real run pending |
| M5 HTML reports | ✅ code; first real run pending |
| M6 Weekly refresh + GitHub Pages | ✅ code; to be installed |
| M7 Point-in-time backtest of valuation signals | ✅ code; first real run pending (`ere research backtest`) |

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
uv run ere db sync-universe
uv run ere refresh                # first run: ~2-3 hours of polite downloading, resumable
open reports/index.html
```

`ere refresh` runs every stage below in order. Each download step is incremental, so the
weekly run takes minutes.

## Stages

| Stage | Commands | Notes |
|---|---|---|
| Prices | `ere ingest prices`, `ere ingest corp-actions`, `ere build prices`, `ere check prices` | whole NSE main board; both bhavcopy formats |
| Fundamentals | `ere ingest filings`, `ere ingest xbrl`, `ere build financials`, `ere check financials`, `ere check golden` | editing `config/xbrl_mapping.yaml` only needs `build financials` |
| Shareholding | `ere ingest shareholding` | promoter / public %, promoter pledges |
| Analytics | `ere build analytics`, `ere show SYMBOL` | as of the last trading day (or `--as-of`) |
| Valuation | `ere build valuation` | ~3 min: builds 5-year point-in-time multiple history |
| Reports | `ere report SYMBOL`, `ere report --all` | self-contained HTML, light/dark, phone-friendly |
| Research | `ere research backtest` | do cheap-looking stocks outperform? rank IC, quintile spreads, caveats → `research.html` |
| Publish | `ere publish` | pushes `reports/` to the `gh-pages` branch |
| Weekly job | `./scripts/install_weekly_job.sh` | macOS launchd, Saturdays 09:00 |

Every download keeps the raw file in `data/raw/` (never committed), so any stage can be rebuilt
offline with `--offline`.

## Design choices worth knowing

**Point-in-time everywhere.** Every financial value keeps the date it was filed; revisions are
new rows, never overwrites. `fundamentals(as_of=...)`, the analytics and the multiple bands
only use what had been filed by that date, so backtests cannot look ahead.

**ISINs are not stable identifiers.** A face-value split issues a new ISIN, so `security_master`
chains old and new ISINs into one security. Adjustment factors come from, in order: manual
entries (`config/price_adjustments.yaml`), splits/bonuses on NSE's corporate-action record,
splits inferred where the ISIN changed with nothing on record (snapped to a face-value ratio),
and demergers on record (factor from the price drop). Every event is verified against the price
series. NSE's bhavcopy `PREVCLOSE` is the raw previous close, so it cannot be used for this.

**Two XBRL eras, one parser.** Results filings (2018 to early 2025, `in-bse-fin`) and SEBI
Integrated Filing (from Q4 FY25, `in-capmkt`) share element names; the parser matches local
names, keeps only non-dimensional contexts and reads periods from context dates.

**Five valuation paths** (per stock in `config/universe.yaml`):

| Path | Used for | Methods |
|---|---|---|
| `dcf` | operating companies (default) | 10-year FCFF DCF, bear/base/bull, WACC × g sensitivity, reverse DCF; P/E and EV/EBITDA vs own band and peers |
| `residual_income` | banks, NBFCs, HFCs | excess return on book, bear/base/bull; P/B and P/E |
| `insurance` | general insurers | as residual_income |
| `sotp` | holding companies | listed stakes at market, unlisted at stated value, holdco discount (`config/sotp.yaml`) |
| `ev_sales` | loss-making / pre-profit | EV/Sales vs own band and peers; DCF only once EBITDA is positive |

**Why the weekly job runs on a laptop, not in GitHub Actions.** NSE routinely blocks cloud
data-centre IPs and the raw cache is over a gigabyte, so the Mac does the data work and pushes
only the finished HTML to `gh-pages`.

## Layout

```
config/        universe, valuation assumptions, XBRL mapping, price adjustments, SOTP inputs,
               golden-test figures, optional business overviews
src/ere/
  ingest/      bhavcopy, delivery, indices, corporate actions, filings index, XBRL, shareholding
  clean/       security master, price adjustment, financials mapping, data checks
  analytics/   risk, ratios, quality flags, snapshot build
  valuation/   DCF, residual income, multiples, SOTP, build + football field
  report/      charts (SVG), templates, report build
  research/    M7 point-in-time backtest and its page
  publish.py   refresh pipeline, gh-pages publishing
  cli.py       the `ere` command
scripts/       weekly refresh + launchd installer
tests/         synthetic fixtures in NSE's real file formats; hand-checked valuation cases
docs/PLAN.md   decisions, milestones, risks, lessons logged
```

## Data sources

NSE constituent list, bhavcopy archives, index closes, corporate actions, results XBRL,
Integrated Filing, shareholding and pledge disclosures. Only public files are downloaded, at a
polite rate; raw data is never committed or republished, and scraping sites whose terms forbid
it is out of scope.

## Licence

MIT
