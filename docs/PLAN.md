# Project plan (v1)

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Universe | **Nifty Smallcap 100** (snapshot 2026-09-24) | Where public-data research adds the most value (thin analyst coverage). Smallcap 250 is the scale-up once v1 is stable. |
| Repo | **Public**, MIT licence | Portfolio value. Data is gitignored and rebuilt from source. |
| Reports | Static HTML/PDF on **GitHub Pages** | Valuation *ranges and assumptions only* — no ratings, no target prices, disclaimer on every page (SEBI Research Analyst rules). |
| LLM narrative | **Deferred to v1.1**, off by default | Numbers first; LLM text only ever describes computed figures. |
| Timeline | **~12 weeks** at 10–12 hrs/week | Smallcap data is messier than large-cap; M2 gets extra time. |

## What changes because it is smallcap

- **Five valuation paths, not two.** `config/universe.yaml` routes each stock:
  `dcf` (default, 75), `residual_income` (16 banks/NBFCs/HFCs), `insurance` (STARHEALTH),
  `sotp` (CHOLAHLDNG), `ev_sales` (7 loss-making or pre-profit names such as OLAELEC and MEESHO).
  Asset-light financials (CDSL, CAMS, KFINTECH…) stay on DCF: they are fee businesses.
- **Short histories.** 15 stocks listed or restructured in 2024 or later. Reports must
  say so, and the multiples band falls back to peers only.
- **Liquidity and surveillance matter.** Extra flags: median daily traded value below ₹5 cr,
  and presence on NSE ASM/GSM lists.
- **Beta benchmark** = Nifty Smallcap 100 (Nifty 50 beta also shown). An optional 1.5% size premium sits in config.
- **Survivorship bias.** The index rebalances in March and September; `index_membership`
  records entries and exits so the M7 backtest only uses stocks that were in the index at the time.

## Milestones

| # | Weeks | Deliverable | Done when |
|---|---|---|---|
| **M0** | 1 | Skeleton, config, universe, DuckDB schema, CLI, CI | `pytest` green; `ere config check` and `ere db sync-universe` work ✅ |
| **M1** | 2–3 | Prices: NSE bhavcopy (legacy + UDiFF from Jul 2024), index closes, delivery %, corporate actions, ISIN chaining, adjusted prices | 10 years loaded; every `error` anomaly in `ere check prices` for index stocks explained or fixed. ✅ done (0 errors, 8 explained warnings) |
| **M2** | 4–6 | Fundamentals: results XBRL (consolidated + standalone) from both NSE sources, element mapping, revisions kept point-in-time | Golden tests pass for 5 stocks (±0.5%); identity checks clean; unmapped elements reviewed. Code ✅, first full run pending |
| **M3** | 7 | Shareholding, ratios, quality flags, beta, liquidity, peers | Ratios hand-checked for 5 stocks. Code ✅ (synthetic tests), first real run pending |
| **M4** | 8–9 | DCF (scenarios, 5×5 sensitivity, reverse DCF), residual income, multiples, SOTP, EV/Sales | Toy cases match a hand calculation exactly. Code ✅ (toy DCF checked line by line); SOTP inputs to fill |
| **M5** | 10 | Report template + charts (+ optional PDF) | `ere report KAYNES` gives a clean report. Code ✅, checked visually on synthetic data (light, dark, phone) |
| **M6** | 11 | Weekly refresh (launchd on the Mac; NSE blocks cloud IPs), `ere publish` to gh-pages, README | Unattended refresh succeeds. Code ✅, to be installed |
| **M7** | 12+ | Point-in-time backtest: do cheap-looking stocks (earnings / book / EBITDA yield, discount to own multiple history) outperform over 1, 3, 12 months? | Quintile spreads + rank IC, no look-ahead. Code ✅; survivorship-free needs historical index constituents (not yet available) |

Golden-test stocks for M2 (chosen to cover every path): **KAYNES** (DCF, capex-heavy growth),
**NATCOPHARM** (DCF, pharma), **KARURVYSYA** (bank), **MANAPPURAM** (NBFC), **CDSL** (asset-light financial).

## Risks

| Risk | Mitigation |
|---|---|
| NSE blocks or rate-limits requests | `ere.http.ExchangeClient`: cookie priming, a request every 0.75 s or slower, backoff; archives first; BSE fallback |
| XBRL tags inconsistent across filers | Candidate-tag lists in `xbrl_mapping.yaml`; unmapped-tag log; golden tests |
| Restatements | `financials` keeps every filing_date; analysis uses the latest filing available *as of* a date |
| Index rebalance breaks config | `load_universe` fails loudly on stale overrides; `ere universe refresh` shows the diff |
| Scope creep | No new features before M6 |

## Lessons logged

- **M1:** ISINs are not stable in India. A face-value split issues a new ISIN, so M0's
  "ISINs usually don't change" was wrong. Fixed with `security_master`, which chains ISINs by
  symbol continuity within 5 sessions.
- **M1 first run (Sept 2026):** 2,658 sessions loaded with 0 parse errors across both
  bhavcopy formats; delivery data exists only from 2019-08-23. The first anomaly report
  disproved an assumption: bhavcopy `PREVCLOSE` is *not* adjusted on ex-dates (implied factor
  1.0 on all 42 split/bonus ex-dates). Verification now uses the price series itself, splits
  missing from the corporate-action record are inferred at ISIN changes, and demergers on record
  get an approximate factor.
- **M1 review of the last flags:** (1) NSE often prints a split's ex-date under the old ISIN
  and switches ISIN a session later (KARURVYSYA, CGCL 2016), so inferred splits are searched
  for around the ISIN change. (2) A stock can hit its 20% upper circuit on an ex-date (CGCL 2024,
  +20.0% after a correct 0.25 factor), so the ex-date check tolerates the circuit limit.
  (3) Only FORCEMOT has missing sessions: it is absent from NSE's own files (NSE listing from
  Aug 2019, plus a few days in Feb 2024), so moves across gaps are reported as `gap_move`.
- **M2 source check (Sept 2026, before coding):** XBRL results exist only from ~2018 (older
  filings show no XBRL link), so fundamentals cover ~8 years. From Q4 FY25 results moved to
  SEBI Integrated Filing on a separate NSE endpoint (`integrated-filing-results`) with a new
  taxonomy (`in-capmkt`); element names are the same, so one parser matching local names
  handles both. Dimensional contexts (breakdowns) are skipped; periods come from context dates.
- **M3-M6 (built unattended, Sept 2026):** shareholding endpoints verified live first
  (promoter/public % from the shareholding master; pledges as % of promoter holding from the
  pledge API). FII/DII splits, auditor changes, contingent liabilities and ASM/GSM lists are
  known v1 gaps, shown as "not available" rather than guessed. All four stages are tested on a
  synthetic market; real-data issues are expected on the first run, as in M1.

## Known v1 gaps

| Gap | Effect | Plan |
|---|---|---|
| FII / DII / MF holdings | Shareholding shows promoter and public only | Parse the shareholding XBRL (dimensional) |
| Auditor change, contingent liabilities | Flags shown as "not available" | Annual-report parsing or manual input |
| ASM / GSM surveillance lists | Flag not evaluated | NSE surveillance list download |
| NBFC XBRL element names | Unverified until the first run | Fix from `xbrl_unmapped_elements.csv` |
| SOTP stakes (CHOLAHLDNG) | Report says "needs inputs" | Fill `config/sotp.yaml` from the annual report |
| Bank NIM | Approximated with advances + investments | Needs interest-earning assets from annual reports |
| Historical index constituents | M7 backtest has survivorship bias | Archive NSE rebalance files (March / September) going forward; source past lists |
