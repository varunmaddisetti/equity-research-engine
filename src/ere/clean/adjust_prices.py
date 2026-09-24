"""Split/bonus-adjusted price history with built-in cross-checks.

Three sources of adjustment factors, in priority order:

1. manual       - config/price_adjustments.yaml (demergers etc. where you know the ratio)
2. corp_action  - split / consolidation / bonus parsed from NSE corporate actions
3. implied      - the exchange's own base price. On an ex-date NSE publishes prev_close already
                  adjusted, so  implied = prev_close(t) / close(t-1).  Anything more than
                  IMPLIED_THRESHOLD away from 1 with no matching corporate action is used as an
                  event and flagged. This catches rights issues (TERP), demergers and missing
                  corporate-action records. Small implied moves (dividends, tick rounding) are
                  ignored, so adj_close is a *price* series, not total return.

adj_factor(t) = product of event factors with ex_date > t;  adj_close = close * adj_factor.

Anomalies written to price_anomalies:
  factor_mismatch            corp-action factor disagrees with implied factor by > 2%
  unmatched_base_adjustment  exchange adjusted the base price but no corp action explains it
  large_move                 |adjusted daily return| > 25% (most smallcaps have 20% circuits,
                             so this usually means a missed adjustment)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from ere.clean.security_master import build_security_master

IMPLIED_THRESHOLD = 0.05
MISMATCH_TOL = 0.02
LARGE_MOVE_WARN = 0.25
LARGE_MOVE_ERROR = 0.50
CAPITAL_ACTIONS = ("split", "consolidation", "bonus")
EXPLAINING_ACTIONS = ("split", "consolidation", "bonus", "rights", "demerger")


@dataclass
class BuildStats:
    securities: int = 0
    rows: int = 0
    events_corp_action: int = 0
    events_implied: int = 0
    events_manual: int = 0
    anomalies_warn: int = 0
    anomalies_error: int = 0


def load_manual_adjustments(path: Path) -> pd.DataFrame:
    cols = ["isin", "symbol", "ex_date", "factor", "note"]
    if not path.exists():
        return pd.DataFrame(columns=cols)
    raw = yaml.safe_load(path.read_text()) or {}
    rows = raw.get("adjustments") or []
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["ex_date"] = pd.to_datetime(df["ex_date"])
        df["factor"] = df["factor"].astype(float)
    return df


def _assign_security(
    df: pd.DataFrame, master: pd.DataFrame, date_col: str
) -> pd.Series:
    """Map rows with isin/symbol/date to security_id: ISIN first, then symbol+date."""
    by_isin = master.set_index("isin")["security_id"]
    sid = df["isin"].map(by_isin) if "isin" in df else pd.Series(np.nan, index=df.index)
    missing = sid.isna() & df["symbol"].notna()
    if missing.any():
        for i in df.index[missing]:
            sym, d = df.at[i, "symbol"], df.at[i, date_col]
            m = master[(master.symbol == sym) & (master.first_date <= d + pd.Timedelta(days=15))
                       & (master.last_date >= d - pd.Timedelta(days=15))]
            if len(m):
                sid.at[i] = m["security_id"].iloc[0]
    return sid


def _align_to_sessions(dates: pd.Series, sessions: np.ndarray) -> pd.Series:
    """Move each date to the first session on/after it (ex-date on a suspension day)."""
    idx = np.searchsorted(sessions, dates.values.astype("datetime64[ns]"))
    out = pd.Series(pd.NaT, index=dates.index, dtype="datetime64[ns]")
    ok = idx < len(sessions)
    out[ok] = sessions[idx[ok]]
    return out


def compute_adjustments(
    prices: pd.DataFrame,
    corp_actions: pd.DataFrame,
    manual: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Pure function (easy to test).

    prices:       security_id, isin, symbol, date, close, prev_close, volume, traded_value
    corp_actions: security_id, ex_date, action, factor
    manual:       security_id, ex_date, factor, note
    Returns (prices_adjusted, price_events, price_anomalies).
    """
    prices = prices.sort_values(["security_id", "date"]).reset_index(drop=True)
    prices["implied"] = prices["prev_close"] / prices.groupby("security_id")["close"].shift(1)

    events, anomalies, adjusted = [], [], []
    ca_by_sid = {k: g for k, g in corp_actions.groupby("security_id")}
    man_by_sid = {k: g for k, g in manual.groupby("security_id")}

    for sid, px in prices.groupby("security_id", sort=False):
        px = px.reset_index(drop=True)
        sessions = px["date"].values.astype("datetime64[ns]")
        first, last = px["date"].iloc[0], px["date"].iloc[-1]
        implied = dict(zip(px["date"], px["implied"], strict=False))

        # --- candidate events from corporate actions
        ca = ca_by_sid.get(sid)
        ca_factor: dict[pd.Timestamp, float] = {}
        explained: dict[pd.Timestamp, list[str]] = {}
        if ca is not None:
            ca = ca[(ca.ex_date > first) & (ca.ex_date <= last)].copy()
            ca["d"] = _align_to_sessions(ca["ex_date"], sessions)
            for r in ca.dropna(subset=["d"]).itertuples(index=False):
                if r.action in EXPLAINING_ACTIONS:
                    explained.setdefault(r.d, []).append(r.action)
                if r.action in CAPITAL_ACTIONS and pd.notna(r.factor):
                    ca_factor[r.d] = ca_factor.get(r.d, 1.0) * float(r.factor)

        man = man_by_sid.get(sid)
        man_factor: dict[pd.Timestamp, tuple[float, str]] = {}
        if man is not None:
            man = man.copy()
            man["d"] = _align_to_sessions(man["ex_date"], sessions)
            for r in man.dropna(subset=["d"]).itertuples(index=False):
                man_factor[r.d] = (float(r.factor), r.note or "")

        ev: dict[pd.Timestamp, float] = {}
        for d, (f, note) in man_factor.items():
            ev[d] = f
            events.append((sid, d, f, "manual", note))
        for d, f in ca_factor.items():
            if d in ev:
                continue
            ev[d] = f
            imp = implied.get(d)
            note = "+".join(explained.get(d, []))
            events.append((sid, d, f, "corp_action", note))
            if pd.notna(imp) and abs(imp / f - 1) > MISMATCH_TOL:
                anomalies.append((sid, d, "factor_mismatch", "warn", imp,
                                  f"corp action {f:.4f} vs exchange implied {imp:.4f}"))
        for d, imp in implied.items():
            if d in ev or pd.isna(imp) or abs(imp - 1) <= IMPLIED_THRESHOLD:
                continue
            ev[d] = float(imp)
            why = explained.get(d)
            events.append((sid, d, float(imp), "implied_prev_close",
                           "+".join(why) if why else "unexplained"))
            if not why:
                anomalies.append((sid, d, "unmatched_base_adjustment", "warn", imp,
                                  "exchange adjusted base price; no corporate action found"))

        # --- adjustment factor: product of factors for events strictly after each date
        f_at = px["date"].map(ev).fillna(1.0).to_numpy()
        rev_cum = np.cumprod(f_at[::-1])[::-1]
        adj = np.append(rev_cum[1:], 1.0)
        out = px[["security_id", "date", "isin", "symbol", "close", "traded_value"]].copy()
        out["adj_factor"] = adj
        out["adj_close"] = px["close"].to_numpy() * adj
        out["adj_volume"] = px["volume"].astype(float).to_numpy() / adj
        out["ret_1d"] = out["adj_close"].pct_change()
        adjusted.append(out)

        big = out[out["ret_1d"].abs() > LARGE_MOVE_WARN]
        for r in big.itertuples(index=False):
            sev = "error" if abs(r.ret_1d) > LARGE_MOVE_ERROR else "warn"
            anomalies.append((sid, r.date, "large_move", sev, r.ret_1d,
                              "check for a missing split/bonus/demerger"))

    cols_adj = ["security_id", "date", "isin", "symbol", "close", "adj_factor", "adj_close",
                "adj_volume", "traded_value", "ret_1d"]
    prices_adjusted = (pd.concat(adjusted, ignore_index=True)[cols_adj] if adjusted
                       else pd.DataFrame(columns=cols_adj))
    price_events = pd.DataFrame(events, columns=["security_id", "ex_date", "factor", "source",
                                                 "note"])
    price_anomalies = pd.DataFrame(anomalies, columns=["security_id", "date", "kind",
                                                       "severity", "value", "note"])
    return prices_adjusted, price_events, price_anomalies


def build_adjusted_prices(
    con: duckdb.DuckDBPyConnection,
    manual_path: Path,
    scope: str = "universe",
) -> BuildStats:
    """Rebuild security_master, price_events, prices_adjusted and price_anomalies."""
    master = build_security_master(con)
    con.execute("DELETE FROM security_master")
    if master.empty:
        return BuildStats()
    con.register("_m", master)
    con.execute("INSERT INTO security_master SELECT * FROM _m")
    con.unregister("_m")

    if scope == "universe":
        uni = con.execute("SELECT isin, symbol FROM securities WHERE in_index").df()
        uni["date"] = pd.Timestamp(date.today())
        sids = set(_assign_security(uni, master, "date").dropna())
        if not sids:
            raise ValueError("no universe securities have price data; run `ere db sync-universe` "
                             "and `ere ingest prices` first")
    elif scope == "all":
        sids = set(master["security_id"])
    else:
        raise ValueError("scope must be 'universe' or 'all'")

    wanted = master[master.security_id.isin(sids)][["isin", "security_id"]]
    con.register("_w", wanted)
    prices = con.execute(
        """
        SELECT w.security_id, p.isin, p.symbol, p.date, p.close, p.prev_close,
               p.volume, p.traded_value
        FROM prices_daily p JOIN _w w USING (isin)
        """
    ).df()
    con.unregister("_w")

    ca = con.execute("SELECT isin, symbol, ex_date, action, factor FROM corp_actions").df()
    ca["security_id"] = _assign_security(ca, master, "ex_date")
    ca = ca.dropna(subset=["security_id"])
    ca = ca[ca.security_id.isin(sids)]

    manual = load_manual_adjustments(manual_path)
    manual["security_id"] = (_assign_security(manual, master, "ex_date") if len(manual)
                             else pd.Series(dtype=str))
    manual = manual.dropna(subset=["security_id"])

    adj, events, anomalies = compute_adjustments(prices, ca, manual)

    for table, df in (("prices_adjusted", adj), ("price_events", events),
                      ("price_anomalies", anomalies)):
        con.execute(f"DELETE FROM {table}")
        if len(df):
            con.register("_d", df)
            cols = ", ".join(df.columns)
            con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _d")
            con.unregister("_d")

    src = events["source"].value_counts() if len(events) else pd.Series(dtype=int)
    sev = anomalies["severity"].value_counts() if len(anomalies) else pd.Series(dtype=int)
    return BuildStats(
        securities=len(sids),
        rows=len(adj),
        events_corp_action=int(src.get("corp_action", 0)),
        events_implied=int(src.get("implied_prev_close", 0)),
        events_manual=int(src.get("manual", 0)),
        anomalies_warn=int(sev.get("warn", 0)),
        anomalies_error=int(sev.get("error", 0)),
    )
