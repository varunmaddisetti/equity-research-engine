"""NSE corporate actions -> corp_actions table.

Source: https://www.nseindia.com/api/corporates-corporateActions
        ?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
Returns a JSON list of records like
    {"symbol": "KAYNES", "series": "EQ", "isin": "INE918Z01012", "faceVal": "10",
     "subject": "Bonus 1:1", "exDate": "08-Sep-2023", "recDate": "...", ...}
This endpoint sits on www.nseindia.com and needs the cookie-primed client.

The interesting part is `subject`, free text such as
    "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
    "Bonus 3:2"
    "Interim Dividend - Rs 2.50 Per Share / Special Dividend - Re 1 Per Share"
    "Rights 1:5 @ Premium Rs 100/-"
which parse_subject() turns into typed actions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from ere.db import done_keys, log_ingest, upsert_df

API = "https://www.nseindia.com/api/corporates-corporateActions"

_RS = r"(?:rs|re|inr|₹)\.?\s*-?\s*"
_NUM = r"(\d+(?:\.\d+)?)"
_FROM_TO = re.compile(rf"from\s*{_RS}{_NUM}.*?to\s*{_RS}{_NUM}", re.I | re.S)
_BONUS = re.compile(r"bonus[^0-9]*(\d+)\s*:\s*(\d+)", re.I)
_RIGHTS = re.compile(r"rights[^0-9]*(\d+)\s*:\s*(\d+)", re.I)
_PREMIUM = re.compile(rf"premium\s*(?:of\s*)?{_RS}{_NUM}", re.I)
_DIV_AMT = re.compile(rf"dividend[^/0-9%]*?{_RS}{_NUM}", re.I)
_DIV_PCT = re.compile(rf"dividend[^/0-9]*?{_NUM}\s*%", re.I)


@dataclass(frozen=True)
class Action:
    action: str
    factor: float | None = None
    amount: float | None = None


def parse_subject(subject: str, face_value: float | None = None) -> list[Action]:
    """Turn an NSE corporate-action subject into zero or more typed actions.

    factor = multiplier applied to prices BEFORE the ex-date:
      split Rs10 -> Rs2      : 2/10 = 0.2
      consolidation Rs1->Rs10: 10/1 = 10
      bonus a:b (a new per b): b/(a+b), e.g. 1:1 -> 0.5, 3:2 -> 0.4
    """
    s = " ".join(subject.split())
    low = s.lower()
    out: list[Action] = []

    if re.search(r"split|sub-?\s?division", low):
        m = _FROM_TO.search(s)
        if m and float(m.group(1)) > 0:
            out.append(Action("split", factor=float(m.group(2)) / float(m.group(1))))
        else:
            out.append(Action("split"))  # ratio unknown -> implied factor will be used
    elif "consolidat" in low:
        m = _FROM_TO.search(s)
        if m and float(m.group(1)) > 0:
            out.append(Action("consolidation", factor=float(m.group(2)) / float(m.group(1))))
        else:
            out.append(Action("consolidation"))

    m = _BONUS.search(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            out.append(Action("bonus", factor=b / (a + b)))

    m = _RIGHTS.search(s)
    if m:
        p = _PREMIUM.search(s)
        out.append(Action("rights", amount=float(p.group(1)) if p else None))

    if "dividend" in low:
        amounts = [float(x) for x in _DIV_AMT.findall(s)]
        if not amounts and face_value:
            amounts = [float(x) / 100 * face_value for x in _DIV_PCT.findall(s)]
        out.append(Action("dividend", amount=round(sum(amounts), 4) if amounts else None))

    if re.search(r"demerger|scheme of arrangement", low):
        out.append(Action("demerger"))
    if re.search(r"buy\s?-?back", low):
        out.append(Action("buyback"))
    return out


def _parse_date(s: str | None) -> date | None:
    if not s or s.strip() in ("-", ""):
        return None
    try:
        return pd.to_datetime(s, format="%d-%b-%Y").date()
    except ValueError:
        return None


def records_to_frame(records: list[dict]) -> pd.DataFrame:
    rows = []
    for r in records:
        ex = _parse_date(r.get("exDate"))
        isin = (r.get("isin") or "").strip()
        subject = (r.get("subject") or "").strip()
        if not ex or not isin or not subject:
            continue
        try:
            fv = float(r.get("faceVal") or "nan")
        except ValueError:
            fv = None
        for a in parse_subject(subject, fv if fv == fv else None):
            rows.append({
                "isin": isin,
                "symbol": (r.get("symbol") or "").strip(),
                "ex_date": pd.Timestamp(ex),
                "action": a.action,
                "factor": a.factor,
                "amount": a.amount,
                "subject": subject,
                "source": "nse_api_corp_actions",
            })
    cols = ["isin", "symbol", "ex_date", "action", "factor", "amount", "subject", "source"]
    df = pd.DataFrame(rows, columns=cols)
    return df.drop_duplicates(["isin", "ex_date", "action", "subject"]).reset_index(drop=True)


def quarter_windows(start: date, end: date) -> list[tuple[date, date]]:
    out, s = [], date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    while s <= end:
        nm = s.month + 3
        e = date(s.year + (nm > 12), (nm - 1) % 12 + 1, 1) - timedelta(days=1)
        out.append((max(s, start), min(e, end)))
        s = e + timedelta(days=1)
    return out


def ingest_corp_actions(
    con: duckdb.DuckDBPyConnection,
    raw_dir: Path,
    start: date,
    end: date,
    client=None,
    offline: bool = False,
    today: date | None = None,
) -> dict[str, int]:
    today = today or date.today()
    seen = done_keys(con, "corp_actions")
    stats = {"windows": 0, "rows": 0, "skipped": 0, "errors": 0}
    for ws, we in quarter_windows(start, end):
        key = f"{ws.isoformat()}_{we.isoformat()}"
        p = raw_dir / "nse" / "corp_actions" / f"{key}.json"
        # Windows that end within the last 30 days keep changing; always refresh them.
        stale_ok = we < today - timedelta(days=30)
        if seen.get(key) == "ok" and stale_ok and not offline:
            stats["skipped"] += 1
            continue
        records = None
        if p.exists() and (stale_ok or offline):
            records = json.loads(p.read_text())
        elif offline:
            continue
        else:
            try:
                records = client.get_json(API, params={
                    "index": "equities",
                    "from_date": f"{ws:%d-%m-%Y}",
                    "to_date": f"{we:%d-%m-%Y}",
                })
            except Exception as e:
                log_ingest(con, "corp_actions", key, "error", message=str(e)[:500])
                stats["errors"] += 1
                continue
            records = records or []
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(records))
        df = records_to_frame(records)
        n = upsert_df(con, "corp_actions", df, ["isin", "ex_date", "action", "subject"])
        log_ingest(con, "corp_actions", key, "ok", rows=n, message=f"{len(records)} records")
        stats["windows"] += 1
        stats["rows"] += n
    return stats
