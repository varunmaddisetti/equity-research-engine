"""Quarterly shareholding pattern and promoter pledges -> shareholding table.

Sources (checked live, Sept 2026):
- https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=X
  list of {"date": "30-JUN-2026", "pr_and_prgrp": "53.46", "public_val": "46.54",
           "employeeTrusts": "0", "submissionDate": "20-JUL-2026",
           "broadcastDate": "20-JUL-2026 16:28:46", "revisedData": "N", "xbrl": ...}
  Coverage starts around late 2022 for most companies.
- https://www.nseindia.com/api/corporate-pledgedata?index=equities&symbol=X
  {"data": [{"shp": "30-Jun-2026", "percPromoterHolding": " 24.85",
             "percPromoterShares": " 0.00",   <- promoter shares encumbered, % of promoter holding
             "percTotShares": " 0.00",        <- same, % of total shares
             "percSharesPledged": "4.54", ...}]}
FII / DII / MF splits live only inside the (large, dimensional) shareholding XBRL and are left
for a later version.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from ere.db import log_ingest, upsert_df

SHP_API = "https://www.nseindia.com/api/corporate-share-holdings-master"
PLEDGE_API = "https://www.nseindia.com/api/corporate-pledgedata"
COLS = ["isin", "symbol", "quarter_end", "category", "pct", "pledged_pct", "filing_date",
        "source"]


def _num(s) -> float | None:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def _date(s, fmts=("%d-%b-%Y", "%d-%b-%Y %H:%M:%S")):
    if not s:
        return None
    for f in fmts:
        try:
            return datetime.strptime(str(s).strip().title(), f).date()
        except ValueError:
            continue
    return None


def shp_to_frame(records: list[dict], isin: str, symbol: str) -> pd.DataFrame:
    rows = []
    for r in records or []:
        q = _date(r.get("date"))
        if not q:
            continue
        filed = _date(r.get("submissionDate")) or _date(r.get("broadcastDate"))
        for cat, key in (("promoter", "pr_and_prgrp"), ("public", "public_val"),
                         ("employee_trusts", "employeeTrusts")):
            v = _num(r.get(key))
            if v is not None:
                rows.append((isin, symbol, q, cat, v, None, filed, "nse_shp_master"))
    df = pd.DataFrame(rows, columns=COLS)
    # Revised submissions repeat a quarter: keep the latest filing.
    return df.sort_values("filing_date").drop_duplicates(
        ["isin", "quarter_end", "category"], keep="last")


def pledge_to_frame(payload) -> pd.DataFrame:
    records = payload.get("data", []) if isinstance(payload, dict) else (payload or [])
    rows = []
    for r in records:
        q = _date(r.get("shp"))
        if not q:
            continue
        rows.append({"quarter_end": q,
                     "pledged_pct": _num(r.get("percPromoterShares")),
                     "pledged_pct_total": _num(r.get("percTotShares")),
                     "promoter_pct": _num(r.get("percPromoterHolding"))})
    return pd.DataFrame(rows, columns=["quarter_end", "pledged_pct", "pledged_pct_total",
                                       "promoter_pct"])


def ingest_shareholding(
    con: duckdb.DuckDBPyConnection,
    raw_dir: Path,
    securities: list[tuple[str, str]],
    client=None,
    offline: bool = False,
    on_progress=None,
) -> dict[str, int]:
    stats = {"symbols": 0, "rows": 0, "errors": 0}
    for symbol, isin in securities:
        payloads = {}
        for name, url in (("shp", SHP_API), ("pledge", PLEDGE_API)):
            p = raw_dir / "nse" / "shareholding" / f"{symbol}_{name}.json"
            try:
                if offline:
                    payloads[name] = json.loads(p.read_text()) if p.exists() else None
                else:
                    payloads[name] = client.get_json(url, params={"index": "equities",
                                                                  "symbol": symbol})
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(payloads[name]))
            except Exception as e:
                log_ingest(con, "shareholding", f"{symbol}:{name}", "error", message=str(e)[:500])
                stats["errors"] += 1
                payloads[name] = None
        df = shp_to_frame(payloads.get("shp") or [], isin, symbol)
        pledge = pledge_to_frame(payloads.get("pledge") or {})
        if len(df) and len(pledge):
            pl = pledge.drop_duplicates("quarter_end", keep="last").set_index("quarter_end")
            is_prom = df.category == "promoter"
            df.loc[is_prom, "pledged_pct"] = df.loc[is_prom, "quarter_end"].map(pl.pledged_pct)
        n = upsert_df(con, "shareholding", df, ["isin", "quarter_end", "category"])
        log_ingest(con, "shareholding", symbol, "ok", rows=n)
        stats["symbols"] += 1
        stats["rows"] += n
        if on_progress:
            on_progress(symbol)
    return stats
