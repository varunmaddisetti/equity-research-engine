"""Split/bonus/demerger-adjusted price history with built-in cross-checks.

Lesson from the first real run (Sept 2026): NSE bhavcopy PREVCLOSE is the RAW previous close.
It is not adjusted on ex-dates (implied factor was exactly 1.0 on all 42 split/bonus ex-dates
checked), so it cannot be used to detect corporate actions. Instead the price series itself
is used to verify and fill gaps.

Adjustment events, in priority order:

1. manual               config/price_adjustments.yaml -> adjustments
2. corp_action          split / consolidation / bonus parsed from NSE corporate actions
3. inferred_isin_change the ISIN changed (a split issues a new ISIN) with no split on record
                        and the price jumped: the jump is snapped to the nearest simple ratio
                        between face values (1/2, 1/5, 1/10, 2/5, ...) when within SNAP_TOL
4. demerger_approx      a "Demerger" corporate action within a few sessions of a large drop:
                        factor = close(ex) / close(prev). Approximate (includes that day's
                        market move); put the exact factor in the manual list if you know it

adj_factor(t) = product of event factors with ex_date > t;  adj_close = close * adj_factor.
adj_close is a *price* series: dividends are not reinvested.

Anomalies (price_anomalies):
  event_not_in_prices  a corporate action was applied but the adjusted return on its ex-date
                       is still > 20%: the factor or the date on record is wrong   (error)
  inferred_split       split inferred at an ISIN change; review once               (warn)
  isin_change_gap      ISIN changed with a big jump that is not a simple ratio     (error)
  demerger_approx      demerger factor approximated from prices                     (warn)
  large_move           |adjusted daily return| > 25% on a non-event day; most smallcaps have
                       20% circuit limits, so check for a missed action (warn; > 50%: error).
                       Moves you have checked and accept go in reviewed_moves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from ere.clean.security_master import build_security_master

LARGE_MOVE_WARN = 0.25
LARGE_MOVE_ERROR = 0.50
EVENT_CHECK_TOL = 0.20
MIN_JUMP = 1.25              # an ISIN-change jump smaller than 25% is not treated as a split
SNAP_TOL = 0.08              # snap to a simple ratio if within 8% (absorbs the day's move)
DEMERGER_MIN_DROP = 0.15
DEMERGER_WINDOW = (-1, 3)    # sessions around the recorded ex-date to look for the drop
CAPITAL_ACTIONS = ("split", "consolidation", "bonus")

# An ISIN changes on a FACE-VALUE change (bonuses keep the ISIN), so only ratios between
# the usual face values Rs 10 / 5 / 2 / 1 qualify: splits 10->5, 10->2, 10->1, 5->2, 5->1, 2->1
# and the reverse consolidations. A dense set of ratios would snap almost anything.
_FACE_VALUES = (10, 5, 2, 1)
NICE_RATIOS = sorted({
    float(Fraction(new, old)) for old in _FACE_VALUES for new in _FACE_VALUES if new != old
})


@dataclass
class BuildStats:
    securities: int = 0
    rows: int = 0
    events_corp_action: int = 0
    events_inferred: int = 0
    events_demerger: int = 0
    events_manual: int = 0
    anomalies_warn: int = 0
    anomalies_error: int = 0


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_manual_adjustments(path: Path) -> pd.DataFrame:
    cols = ["isin", "symbol", "ex_date", "factor", "note"]
    df = pd.DataFrame(_load_yaml(path).get("adjustments") or [], columns=cols)
    if not df.empty:
        df["ex_date"] = pd.to_datetime(df["ex_date"])
        df["factor"] = df["factor"].astype(float)
    return df


def load_reviewed_moves(path: Path) -> pd.DataFrame:
    cols = ["isin", "symbol", "date", "note"]
    df = pd.DataFrame(_load_yaml(path).get("reviewed_moves") or [], columns=cols)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def snap_ratio(r: float) -> float | None:
    """Nearest simple ratio to r (in log space) if within SNAP_TOL, else None."""
    if not r or r <= 0 or not math.isfinite(r):
        return None
    best = min(NICE_RATIOS, key=lambda x: abs(math.log(r / x)))
    return best if abs(math.log(r / best)) <= math.log(1 + SNAP_TOL) else None


def _assign_security(df: pd.DataFrame, master: pd.DataFrame, date_col: str) -> pd.Series:
    """Map rows with isin/symbol/date to security_id: ISIN first, then symbol+date."""
    by_isin = master.set_index("isin")["security_id"]
    sid = df["isin"].map(by_isin) if "isin" in df else pd.Series(np.nan, index=df.index)
    sid = sid.astype(object)
    missing = sid.isna() & df["symbol"].notna()
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


def _near_event(dates: list, ev: dict, i: int, span: int = 3) -> bool:
    lo, hi = max(0, i - span), min(len(dates), i + span + 1)
    return any(dates[j] in ev for j in range(lo, hi))


def compute_adjustments(
    prices: pd.DataFrame,
    corp_actions: pd.DataFrame,
    manual: pd.DataFrame,
    reviewed: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Pure function (easy to test).

    prices:       security_id, isin, symbol, date, close, volume, traded_value
    corp_actions: security_id, ex_date, action, factor
    manual:       security_id, ex_date, factor, note
    reviewed:     security_id, date            (large moves already checked by a human)
    Returns (prices_adjusted, price_events, price_anomalies).
    """
    prices = prices.sort_values(["security_id", "date"]).reset_index(drop=True)
    events, anomalies, adjusted = [], [], []
    ca_by_sid = {k: g for k, g in corp_actions.groupby("security_id")}
    man_by_sid = {k: g for k, g in manual.groupby("security_id")}
    reviewed_keys = (set(zip(reviewed["security_id"], reviewed["date"], strict=False))
                     if reviewed is not None and len(reviewed) else set())

    for sid, px in prices.groupby("security_id", sort=False):
        px = px.reset_index(drop=True)
        dates = list(px["date"])
        pos = {d: i for i, d in enumerate(dates)}
        sessions = px["date"].values.astype("datetime64[ns]")
        close = px["close"].to_numpy(dtype=float)
        raw_ratio = np.r_[np.nan, close[1:] / close[:-1]]
        first, last = dates[0], dates[-1]

        ev: dict[pd.Timestamp, tuple[float, str, str]] = {}

        # 1. manual
        man = man_by_sid.get(sid)
        if man is not None:
            man = man.assign(d=_align_to_sessions(man["ex_date"], sessions))
            for r in man.dropna(subset=["d"]).itertuples(index=False):
                ev[r.d] = (float(r.factor), "manual", r.note or "")

        # 2. corporate actions on record
        ca = ca_by_sid.get(sid)
        demerger_dates: list[pd.Timestamp] = []
        if ca is not None:
            ca = ca[(ca.ex_date > first) & (ca.ex_date <= last)]
            ca = ca.assign(d=_align_to_sessions(ca["ex_date"], sessions)).dropna(subset=["d"])
            cap: dict[pd.Timestamp, list] = {}
            for r in ca.itertuples(index=False):
                if r.action in CAPITAL_ACTIONS and pd.notna(r.factor):
                    cap.setdefault(r.d, []).append((r.action, float(r.factor)))
                elif r.action == "demerger":
                    demerger_dates.append(r.d)
            for d, items in cap.items():
                if d not in ev:
                    f = float(np.prod([x[1] for x in items]))
                    ev[d] = (f, "corp_action", "+".join(x[0] for x in items))

        # 3. splits inferred at ISIN changes
        isins = px["isin"].to_numpy()
        for i in np.flatnonzero(isins[1:] != isins[:-1]) + 1:
            r = raw_ratio[i]
            if not np.isfinite(r) or abs(math.log(r)) < math.log(MIN_JUMP):
                continue
            if _near_event(dates, ev, i):
                continue
            f = snap_ratio(r)
            d = dates[i]
            if f is not None:
                ev[d] = (f, "inferred_isin_change", f"ISIN {isins[i - 1]} -> {isins[i]}")
                anomalies.append((sid, d, "inferred_split", "warn", f,
                                  f"no split on record; price ratio {r:.4f} snapped to {f:.4f}"))
            else:
                anomalies.append((sid, d, "isin_change_gap", "error", r,
                                  "ISIN changed with a jump that is not a simple ratio"))

        # 4. demergers on record -> factor from the price drop
        for dd in demerger_dates:
            i0 = pos[dd]
            window = range(max(1, i0 + DEMERGER_WINDOW[0]),
                           min(len(dates), i0 + DEMERGER_WINDOW[1] + 1))
            cands = [i for i in window if dates[i] not in ev
                     and raw_ratio[i] < 1 - DEMERGER_MIN_DROP]
            if not cands:
                continue
            i = min(cands, key=lambda j: raw_ratio[j])
            f = float(raw_ratio[i])
            ev[dates[i]] = (f, "demerger_approx", "demerger on record; factor from prices")
            anomalies.append((sid, dates[i], "demerger_approx", "warn", f,
                              "approximate factor; set the exact one in price_adjustments.yaml"))

        for d, (f, source, note) in ev.items():
            events.append((sid, d, f, source, note))

        # adjustment factor: product of factors for events strictly after each date
        f_at = np.array([ev[d][0] if d in ev else 1.0 for d in dates])
        rev_cum = np.cumprod(f_at[::-1])[::-1]
        adj = np.append(rev_cum[1:], 1.0)
        out = px[["security_id", "date", "isin", "symbol", "close", "traded_value"]].copy()
        out["adj_factor"] = adj
        out["adj_close"] = close * adj
        out["adj_volume"] = px["volume"].astype(float).to_numpy() / adj
        out["ret_1d"] = out["adj_close"].pct_change()
        adjusted.append(out)

        # verification
        for i, (d, ret) in enumerate(zip(dates, out["ret_1d"], strict=False)):
            if i == 0 or not np.isfinite(ret):
                continue
            if d in ev:
                if ev[d][1] == "corp_action" and abs(ret) > EVENT_CHECK_TOL:
                    anomalies.append((sid, d, "event_not_in_prices", "error", ret,
                                      f"{ev[d][2]} factor {ev[d][0]:.4f} applied but adjusted "
                                      f"return is {ret:+.1%} (raw {raw_ratio[i] - 1:+.1%})"))
                continue
            if abs(ret) > LARGE_MOVE_WARN and (sid, d) not in reviewed_keys:
                sev = "error" if abs(ret) > LARGE_MOVE_ERROR else "warn"
                anomalies.append((sid, d, "large_move", sev, ret,
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
    """Rebuild security_master, price_events, prices_adjusted and price_anomalies.

    manual_path holds both `adjustments` (manual factors) and `reviewed_moves`.
    """
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
        SELECT w.security_id, p.isin, p.symbol, p.date, p.close, p.volume, p.traded_value
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

    reviewed = load_reviewed_moves(manual_path)
    reviewed["security_id"] = (_assign_security(reviewed, master, "date") if len(reviewed)
                               else pd.Series(dtype=str))
    reviewed = reviewed.dropna(subset=["security_id"])

    adj, events, anomalies = compute_adjustments(prices, ca, manual, reviewed)

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
        events_inferred=int(src.get("inferred_isin_change", 0)),
        events_demerger=int(src.get("demerger_approx", 0)),
        events_manual=int(src.get("manual", 0)),
        anomalies_warn=int(sev.get("warn", 0)),
        anomalies_error=int(sev.get("error", 0)),
    )
