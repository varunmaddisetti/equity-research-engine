"""Chain ISINs into stable securities.

Why: in India a face-value split (and some other capital changes) issues a NEW ISIN, while a
rename keeps the ISIN but changes the symbol. Neither identifier alone gives a continuous
price history. Rules:

1. One ISIN may carry several symbols over time (rename)       -> same security.
2. ISIN A stops trading and ISIN B starts under the SAME symbol within MAX_GAP_SESSIONS
   trading sessions                                             -> same security (ISIN change).
   The gap limit stops a recycled symbol (old company delisted, new one listed years later)
   from being glued to an unrelated company.

security_id = the most recent ISIN in the chain.
"""

from __future__ import annotations

import duckdb
import pandas as pd

MAX_GAP_SESSIONS = 5


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_security_master(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    # Per (isin, symbol) spell: first/last date traded under that symbol.
    spells = con.execute(
        """
        SELECT isin, symbol, min(date) AS first_date, max(date) AS last_date
        FROM prices_daily GROUP BY isin, symbol
        """
    ).df()
    if spells.empty:
        return pd.DataFrame(columns=["security_id", "isin", "symbol", "first_date", "last_date"])

    sessions = con.execute("SELECT DISTINCT date FROM prices_daily ORDER BY date").df()["date"]
    session_idx = {d: i for i, d in enumerate(sessions)}

    uf = _UnionFind()
    for isin in spells["isin"].unique():
        uf.find(isin)

    # Rule 2: same symbol, different ISIN, back-to-back.
    for _, g in spells.groupby("symbol"):
        if g["isin"].nunique() < 2:
            continue
        g = g.sort_values("first_date")
        rows = list(g.itertuples(index=False))
        for prev, nxt in zip(rows, rows[1:], strict=False):
            if prev.isin == nxt.isin:
                continue
            gap = session_idx[nxt.first_date] - session_idx[prev.last_date]
            if 0 < gap <= MAX_GAP_SESSIONS:
                uf.union(prev.isin, nxt.isin)

    per_isin = (
        spells.sort_values("last_date")
        .groupby("isin")
        .agg(symbol=("symbol", "last"), first_date=("first_date", "min"),
             last_date=("last_date", "max"))
        .reset_index()
    )
    per_isin["root"] = per_isin["isin"].map(uf.find)
    latest = (
        per_isin.sort_values("last_date").groupby("root")["isin"].last().rename("security_id")
    )
    per_isin = per_isin.join(latest, on="root").drop(columns="root")
    return per_isin[["security_id", "isin", "symbol", "first_date", "last_date"]]
