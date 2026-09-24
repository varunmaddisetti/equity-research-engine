"""Resumable download-parse-load loop for NSE daily files.

- Raw files are cached untouched under data/raw/nse/<dataset>/<year>/ and never re-downloaded.
- ingest_log remembers every date tried, so a rerun only fetches what is new.
- A 404 is logged as 'missing' (usually a holiday). Recent misses (< RECHECK_DAYS old) are
  retried on the next run because NSE may simply not have published yet.
- --offline rebuilds the database from the raw cache without touching the network.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ere.db import done_keys, log_ingest, upsert_df
from ere.ingest.nse_daily import (
    PARSERS,
    TABLES,
    DailyFile,
    bhavcopy_files,
    candidate_sessions,
    delivery_file,
    indices_file,
    raw_path,
)

RECHECK_DAYS = 5
DATASETS = ("bhavcopy", "indices", "delivery")


def _candidates(dataset: str, d: date) -> list[DailyFile]:
    if dataset == "bhavcopy":
        return bhavcopy_files(d)
    if dataset == "delivery":
        return [delivery_file(d)]
    if dataset == "indices":
        return [indices_file(d)]
    raise ValueError(dataset)


def _should_skip(status: str | None, d: date, today: date) -> bool:
    if status == "ok":
        return True
    return status == "missing" and d < today - timedelta(days=RECHECK_DAYS)


def ingest_daily(
    con: duckdb.DuckDBPyConnection,
    raw_dir: Path,
    start: date,
    end: date,
    datasets: Iterable[str] = DATASETS,
    client=None,
    offline: bool = False,
    force: bool = False,
    on_progress: Callable[[str, date, str], None] | None = None,
    today: date | None = None,
) -> Counter:
    """Returns a Counter of (dataset, outcome) -> count."""
    if not offline and client is None:
        raise ValueError("client is required unless offline=True")
    today = today or date.today()
    stats: Counter = Counter()
    sessions = candidate_sessions(start, end)
    for dataset in datasets:
        seen = {} if force else done_keys(con, dataset)
        table, keys = TABLES[dataset]
        for d in sessions:
            key = d.isoformat()
            if not force and not offline and _should_skip(seen.get(key), d, today):
                stats[(dataset, "skipped")] += 1
                continue
            outcome = _ingest_one(con, raw_dir, dataset, d, client, offline, table, keys)
            stats[(dataset, outcome)] += 1
            if on_progress:
                on_progress(dataset, d, outcome)
    return stats


def _ingest_one(con, raw_dir, dataset, d, client, offline, table, keys) -> str:
    key = d.isoformat()
    body, used = None, None
    for f in _candidates(dataset, d):
        p = raw_path(raw_dir, f, d)
        if p.exists():
            body, used = p.read_bytes(), f
            break
    if body is None and offline:
        return "not_cached"
    if body is None:
        try:
            for f in _candidates(dataset, d):
                fetched = client.get_bytes(f.url)
                if fetched:
                    body, used = fetched, f
                    p = raw_path(raw_dir, f, d)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(body)
                    break
        except Exception as e:  # network trouble: log and move on, rerun will retry
            log_ingest(con, dataset, key, "error", message=f"{type(e).__name__}: {e}"[:500])
            return "error"
    if body is None:
        log_ingest(con, dataset, key, "missing", message="404 (holiday or not yet published)")
        return "missing"
    try:
        df = PARSERS[dataset](body, d)
    except Exception as e:
        log_ingest(con, dataset, key, "error",
                   message=f"parse {used.filename}: {type(e).__name__}: {e}"[:500])
        return "error"
    # Each file is one full day, so replace the day wholesale (cheap thanks to zone maps,
    # unlike a key-by-key upsert against millions of rows).
    con.execute(f'DELETE FROM "{table}" WHERE date = ?', [d])
    n = upsert_df(con, table, df, keys, delete_first=False)
    log_ingest(con, dataset, key, "ok", rows=n, message=used.variant)
    return "ok"
