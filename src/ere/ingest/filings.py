"""Index of results filings (with XBRL links) for each stock.

Two NSE endpoints, checked live in Sept 2026:

1. Financial results, 2018 to early 2025 (XBRL exists only from ~2018; older rows say "-"):
   https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Quarterly&symbol=X
   (also period=Annual). Fields used: symbol, isin, fromDate, toDate ("31-Dec-2024"),
   consolidated ("Consolidated" | "Non-Consolidated"), audited, bank ("B" for banks),
   filingDate ("28-Jan-2025 18:28"), xbrl (URL or ".../xbrl/-").

2. SEBI Integrated Filing - Financials, from Q4 FY25 onwards:
   https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=X
       &type=Integrated Filing- Financials
   Fields used: symbol, qe_Date ("30-JUN-2026"), consolidated ("Consolidated" | "Standalone"),
   audited, type_Sub ("Original" | "Revision"), broadcast_Date / creation_Date, xbrl.
   Bank filings have "BANKING" in the XBRL file name.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from ere.db import log_ingest

RESULTS_API = "https://www.nseindia.com/api/corporates-financial-results"
INTEGRATED_API = "https://www.nseindia.com/api/integrated-filing-results"
INTEGRATED_TYPE = "Integrated Filing- Financials"

FILING_COLS = ["filing_id", "isin", "symbol", "source", "period_end", "period_start", "basis",
               "audited", "is_bank", "is_revision", "filed_at", "xbrl_url"]


def _dt(s: str | None, fmts: tuple[str, ...]) -> datetime | None:
    if not s or s.strip() in ("-", ""):
        return None
    for f in fmts:
        try:
            return datetime.strptime(s.strip(), f)
        except ValueError:
            continue
    return None


def _has_xbrl(url: str | None) -> bool:
    return bool(url) and url.lower().endswith(".xml")


def _filing_id(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _basis(s: str | None) -> str:
    return "consolidated" if (s or "").strip().lower() == "consolidated" else "standalone"


def results_records_to_frame(records: list[dict], isin: str) -> pd.DataFrame:
    rows = []
    for r in records or []:
        url = r.get("xbrl")
        end = _dt(r.get("toDate"), ("%d-%b-%Y",))
        filed = _dt(r.get("filingDate"), ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S")) or _dt(
            r.get("broadCastDate"), ("%d-%b-%Y %H:%M:%S",))
        if not _has_xbrl(url) or not end or not filed:
            continue
        rows.append({
            "filing_id": _filing_id(url),
            "isin": isin,
            "symbol": (r.get("symbol") or "").strip(),
            "source": "nse_results",
            "period_end": end.date(),
            "period_start": (_dt(r.get("fromDate"), ("%d-%b-%Y",)) or end).date(),
            "basis": _basis(r.get("consolidated")),
            "audited": (r.get("audited") or "").strip().lower() == "audited",
            "is_bank": (r.get("bank") or "").strip().upper() == "B",
            "is_revision": False,
            "filed_at": filed,
            "xbrl_url": url,
        })
    return pd.DataFrame(rows, columns=FILING_COLS)


def integrated_records_to_frame(records: list[dict], isin: str) -> pd.DataFrame:
    rows = []
    for r in records or []:
        url = r.get("xbrl")
        end = _dt(r.get("qe_Date"), ("%d-%b-%Y",))
        filed = (_dt(r.get("broadcast_Date"), ("%d-%b-%Y %H:%M:%S",))
                 or _dt(r.get("creation_Date"), ("%d-%b-%Y %H:%M:%S",)))
        if not _has_xbrl(url) or not end or not filed:
            continue
        if r.get("type") and r["type"] != INTEGRATED_TYPE:
            continue
        rows.append({
            "filing_id": _filing_id(url),
            "isin": isin,
            "symbol": (r.get("symbol") or "").strip(),
            "source": "nse_integrated",
            "period_end": end.date(),
            "period_start": None,
            "basis": _basis(r.get("consolidated")),
            "audited": (r.get("audited") or "").strip().lower() == "audited",
            "is_bank": "BANKING" in url.upper(),
            "is_revision": (r.get("type_Sub") or "").strip().lower() == "revision",
            "filed_at": filed,
            "xbrl_url": url,
        })
    return pd.DataFrame(rows, columns=FILING_COLS)


def ingest_filing_index(
    con: duckdb.DuckDBPyConnection,
    raw_dir: Path,
    securities: list[tuple[str, str]],
    client=None,
    offline: bool = False,
    on_progress=None,
) -> dict[str, int]:
    """securities: (symbol, current isin). Indexes are small, so they are always refreshed."""
    stats = {"symbols": 0, "filings": 0, "new": 0, "errors": 0}
    known = {r[0] for r in con.execute("SELECT filing_id FROM filings").fetchall()}
    for symbol, isin in securities:
        frames = []
        for name, url, params, parse in (
            ("results_quarterly", RESULTS_API,
             {"index": "equities", "period": "Quarterly", "symbol": symbol},
             results_records_to_frame),
            ("results_annual", RESULTS_API,
             {"index": "equities", "period": "Annual", "symbol": symbol},
             results_records_to_frame),
            ("integrated", INTEGRATED_API,
             {"index": "equities", "symbol": symbol, "type": INTEGRATED_TYPE},
             integrated_records_to_frame),
        ):
            p = raw_dir / "nse" / "filings_index" / f"{symbol}_{name}.json"
            try:
                if offline:
                    if not p.exists():
                        continue
                    records = json.loads(p.read_text())
                else:
                    records = client.get_json(url, params=params) or []
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(records))
                if isinstance(records, dict):  # some endpoints wrap the list
                    records = records.get("data", [])
                frames.append(parse(records, isin))
            except Exception as e:
                log_ingest(con, "filings_index", f"{symbol}:{name}", "error", message=str(e)[:500])
                stats["errors"] += 1
        stats["symbols"] += 1
        if frames:
            df = pd.concat(frames, ignore_index=True).drop_duplicates("filing_id")
            new = df[~df.filing_id.isin(known)]
            if len(new):
                con.register("_f", new)
                cols = ", ".join(FILING_COLS)
                con.execute(f"INSERT INTO filings ({cols}) SELECT {cols} FROM _f")
                con.unregister("_f")
                known |= set(new.filing_id)
            stats["filings"] += len(df)
            stats["new"] += len(new)
            log_ingest(con, "filings_index", symbol, "ok", rows=len(df))
        if on_progress:
            on_progress(symbol)
    return stats
