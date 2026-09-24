"""Download results XBRL instances and extract their numeric facts.

Parsing rules (from live NSE files, Sept 2026):
- Elements are matched by LOCAL name, so `in-bse-fin:RevenueFromOperations` (results filings)
  and `in-capmkt:RevenueFromOperations` (Integrated Filing) are the same fact.
- Only contexts WITHOUT a segment/scenario are kept. Dimensional contexts hold breakdowns
  (individual "other expenses" lines, reportable segments, related-party rows) that would
  otherwise overwrite the headline numbers.
- Periods come from each context's dates, never from its id (ids such as OneD / FourD / OneI /
  PY_I are conventions, not guarantees). A 3-month duration is a quarter, 9 months YTD,
  12 months a year; an instant is a balance-sheet date.
- Only facts with a unitRef are numeric. Amounts are INR (full rupees), EPS INR per share.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from ere.db import upsert_df

XBRLI = "http://www.xbrl.org/2003/instance"
FACT_COLS = ["filing_id", "element", "period_start", "period_end", "is_instant", "value", "unit"]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def parse_contexts(root: ET.Element) -> dict[str, tuple[date, date, bool]]:
    """context id -> (start, end, is_instant) for non-dimensional contexts."""
    out: dict[str, tuple[date, date, bool]] = {}
    for ctx in root.iter(f"{{{XBRLI}}}context"):
        if ctx.find(f".//{{{XBRLI}}}segment") is not None:
            continue
        if ctx.find(f".//{{{XBRLI}}}scenario") is not None:
            continue
        period = ctx.find(f"{{{XBRLI}}}period")
        if period is None:
            continue
        instant = _date(period.findtext(f"{{{XBRLI}}}instant"))
        start = _date(period.findtext(f"{{{XBRLI}}}startDate"))
        end = _date(period.findtext(f"{{{XBRLI}}}endDate"))
        if instant:
            out[ctx.get("id")] = (instant, instant, True)
        elif end and start:
            out[ctx.get("id")] = (start, end, False)
    return out


def parse_xbrl(body: bytes, filing_id: str) -> pd.DataFrame:
    root = ET.fromstring(body)
    contexts = parse_contexts(root)
    rows = []
    for el in root:
        ctx = el.get("contextRef")
        unit = el.get("unitRef")
        if ctx is None or unit is None or ctx not in contexts:
            continue
        text = (el.text or "").strip().replace(",", "")
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            continue
        start, end, instant = contexts[ctx]
        rows.append((filing_id, _local(el.tag), start, end, instant, value, unit))
    df = pd.DataFrame(rows, columns=FACT_COLS)
    # The same element can repeat for one period (rare filer error): keep the first.
    return df.drop_duplicates(["element", "period_start", "period_end"]).reset_index(drop=True)


def raw_xbrl_path(raw_dir: Path, filing_id: str, period_end) -> Path:
    return raw_dir / "nse" / "xbrl" / f"{pd.Timestamp(period_end):%Y}" / filing_id


def ingest_xbrl(
    con: duckdb.DuckDBPyConnection,
    raw_dir: Path,
    client=None,
    offline: bool = False,
    symbols: list[str] | None = None,
    retry_errors: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
) -> dict[str, int]:
    """Download and parse every pending filing. Resumable; raw files cached."""
    statuses = ["pending"] + (["error", "missing"] if retry_errors else [])
    q = ("SELECT filing_id, symbol, period_end, xbrl_url FROM filings WHERE status IN ("
         + ",".join("?" * len(statuses)) + ")")
    params: list = list(statuses)
    if symbols:
        q += " AND symbol IN (" + ",".join("?" * len(symbols)) + ")"
        params += symbols
    todo = con.execute(q + " ORDER BY symbol, period_end", params).fetchall()
    stats = {"parsed": 0, "missing": 0, "errors": 0, "not_cached": 0, "facts": 0}
    for filing_id, symbol, period_end, url in todo:
        p = raw_xbrl_path(raw_dir, filing_id, period_end)
        body = p.read_bytes() if p.exists() else None
        if body is None and offline:
            stats["not_cached"] += 1
            continue
        if body is None:
            try:
                body = client.get_bytes(url)
            except Exception as e:
                _set_status(con, filing_id, "error", f"download: {e}"[:500])
                stats["errors"] += 1
                continue
            if body is None:
                _set_status(con, filing_id, "missing", "404")
                stats["missing"] += 1
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
        try:
            facts = parse_xbrl(body, filing_id)
        except Exception as e:
            _set_status(con, filing_id, "error", f"parse: {type(e).__name__}: {e}"[:500])
            stats["errors"] += 1
            continue
        con.execute("DELETE FROM xbrl_facts WHERE filing_id = ?", [filing_id])
        n = upsert_df(con, "xbrl_facts", facts, [], delete_first=False)
        _set_status(con, filing_id, "parsed", f"{n} facts")
        stats["parsed"] += 1
        stats["facts"] += n
        if on_progress:
            on_progress(symbol, filing_id)
    return stats


def _set_status(con, filing_id: str, status: str, message: str | None = None) -> None:
    con.execute("UPDATE filings SET status = ?, message = ? WHERE filing_id = ?",
                [status, message, filing_id])
