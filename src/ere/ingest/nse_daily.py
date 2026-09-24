"""NSE daily archive files: equity bhavcopy, delivery ("sec_bhavdata_full") and index closes.

Formats (checked against live files, Sept 2026):

- Bhavcopy, legacy (up to 5 Jul 2024), zipped CSV:
    content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip
    SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,
- Bhavcopy, UDiFF (from 8 Jul 2024), zipped CSV:
    content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
    TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,...,OpnPric,HghPric,LwPric,
    ClsPric,LastPric,PrvsClsgPric,...,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,...
- Delivery, plain CSV with spaces after commas:
    products/content/sec_bhavdata_full_{DDMMYYYY}.csv
    SYMBOL, SERIES, DATE1, PREV_CLOSE, ..., DELIV_QTY, DELIV_PER
- Index closes, plain CSV, every NSE index for the day:
    content/indices/ind_close_all_{DDMMYYYY}.csv
    Index Name,Index Date,Open Index Value,...,Closing Index Value,...,P/E,P/B,Div Yield

A 404 on a weekday almost always means a market holiday.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ARCHIVE = "https://nsearchives.nseindia.com"
UDIFF_START = date(2024, 7, 8)
MAIN_BOARD_SERIES = ("EQ", "BE", "BZ")  # BE/BZ = trade-for-trade; smallcaps land there
_SERIES_PRIORITY = {s: i for i, s in enumerate(MAIN_BOARD_SERIES)}

# Weekend sessions (Muhurat trading, budget days, NSE DR drills). A wrong entry costs one
# 404; a missing entry loses one day. Extend as needed.
WEEKEND_SESSIONS = {
    date(2019, 10, 27),
    date(2020, 2, 1),
    date(2020, 11, 14),
    date(2023, 11, 12),
    date(2024, 1, 20),
    date(2024, 3, 2),
    date(2024, 5, 18),
    date(2025, 2, 1),
    date(2026, 2, 1),
}


# ----------------------------------------------------------------------------- calendar
def candidate_sessions(start: date, end: date) -> list[date]:
    """Weekdays plus known weekend sessions. Holidays are discovered as 404s."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 or d in WEEKEND_SESSIONS:
            out.append(d)
        d += timedelta(days=1)
    return out


# ----------------------------------------------------------------------------- files
@dataclass(frozen=True)
class DailyFile:
    dataset: str       # bhavcopy | delivery | indices
    url: str
    filename: str
    variant: str       # legacy | udiff | csv


def bhavcopy_files(d: date) -> list[DailyFile]:
    """Candidate bhavcopy files for a date, preferred format first."""
    mon = d.strftime("%b").upper()
    legacy_name = f"cm{d:%d}{mon}{d:%Y}bhav.csv.zip"
    legacy = DailyFile(
        "bhavcopy",
        f"{ARCHIVE}/content/historical/EQUITIES/{d:%Y}/{mon}/{legacy_name}",
        legacy_name,
        "legacy",
    )
    udiff_name = f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    udiff = DailyFile("bhavcopy", f"{ARCHIVE}/content/cm/{udiff_name}", udiff_name, "udiff")
    return [udiff, legacy] if d >= UDIFF_START else [legacy, udiff]


def delivery_file(d: date) -> DailyFile:
    name = f"sec_bhavdata_full_{d:%d%m%Y}.csv"
    return DailyFile("delivery", f"{ARCHIVE}/products/content/{name}", name, "csv")


def indices_file(d: date) -> DailyFile:
    name = f"ind_close_all_{d:%d%m%Y}.csv"
    return DailyFile("indices", f"{ARCHIVE}/content/indices/{name}", name, "csv")


def raw_path(raw_dir: Path, f: DailyFile, d: date) -> Path:
    return raw_dir / "nse" / f.dataset / f"{d:%Y}" / f.filename


# ----------------------------------------------------------------------------- parsing
def _read_csv_bytes(body: bytes) -> pd.DataFrame:
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            csvs = [n for n in names if n.lower().endswith(".csv")] or names
            if not csvs:
                raise ValueError("zip is empty")
            body = z.read(csvs[0])  # NSE sometimes zips a CSV without the .csv extension
    df = pd.read_csv(io.BytesIO(body), dtype=str, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    df = df.loc[:, [c for c in df.columns if c and not c.startswith("Unnamed")]]
    return df.apply(lambda c: c.str.strip())


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.str.replace(",", "", regex=False).replace({"-": None, "": None}),
                         errors="coerce")


PRICE_COLS = [
    "isin", "date", "symbol", "series", "open", "high", "low", "close", "last",
    "prev_close", "volume", "traded_value", "trades", "source",
]


def _finish_prices(out: pd.DataFrame, d: date, source: str) -> pd.DataFrame:
    out = out[out["series"].isin(MAIN_BOARD_SERIES)].copy()
    out = out[out["isin"].str.match(r"^INE[A-Z0-9]{9}$", na=False)]
    out = out.dropna(subset=["close"])
    out["date"] = pd.Timestamp(d)
    out["source"] = source
    out["volume"] = out["volume"].round().astype("Int64")
    out["trades"] = out["trades"].round().astype("Int64")
    # An ISIN listed in two series on one day is rare; keep the higher-priority series.
    out["_p"] = out["series"].map(_SERIES_PRIORITY)
    out = out.sort_values("_p").drop_duplicates("isin").drop(columns="_p")
    return out[PRICE_COLS].reset_index(drop=True)


def parse_bhavcopy_legacy(body: bytes, d: date) -> pd.DataFrame:
    df = _read_csv_bytes(body)
    out = pd.DataFrame({
        "isin": df["ISIN"],
        "symbol": df["SYMBOL"],
        "series": df["SERIES"],
        "open": _num(df["OPEN"]),
        "high": _num(df["HIGH"]),
        "low": _num(df["LOW"]),
        "close": _num(df["CLOSE"]),
        "last": _num(df["LAST"]),
        "prev_close": _num(df["PREVCLOSE"]),
        "volume": _num(df["TOTTRDQTY"]),
        "traded_value": _num(df["TOTTRDVAL"]),
        "trades": _num(df["TOTALTRADES"]) if "TOTALTRADES" in df else pd.NA,
    })
    return _finish_prices(out, d, "nse_bhav_legacy")


def parse_bhavcopy_udiff(body: bytes, d: date) -> pd.DataFrame:
    df = _read_csv_bytes(body)
    if "FinInstrmTp" in df:
        df = df[df["FinInstrmTp"] == "STK"]
    out = pd.DataFrame({
        "isin": df["ISIN"],
        "symbol": df["TckrSymb"],
        "series": df["SctySrs"],
        "open": _num(df["OpnPric"]),
        "high": _num(df["HghPric"]),
        "low": _num(df["LwPric"]),
        "close": _num(df["ClsPric"]),
        "last": _num(df["LastPric"]),
        "prev_close": _num(df["PrvsClsgPric"]),
        "volume": _num(df["TtlTradgVol"]),
        "traded_value": _num(df["TtlTrfVal"]),
        "trades": _num(df["TtlNbOfTxsExctd"]),
    })
    return _finish_prices(out, d, "nse_bhav_udiff")


def parse_bhavcopy(body: bytes, d: date) -> pd.DataFrame:
    """Detect the format from the header rather than trusting the date."""
    head = body
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            head = z.read(z.namelist()[0])[:300]
    if b"TckrSymb" in head[:300]:
        return parse_bhavcopy_udiff(body, d)
    return parse_bhavcopy_legacy(body, d)


def parse_delivery(body: bytes, d: date) -> pd.DataFrame:
    df = _read_csv_bytes(body)
    df = df[df["SERIES"].isin(MAIN_BOARD_SERIES)]
    out = pd.DataFrame({
        "symbol": df["SYMBOL"],
        "series": df["SERIES"],
        "date": pd.Timestamp(d),
        "delivery_qty": _num(df["DELIV_QTY"]).round().astype("Int64"),
        "delivery_pct": _num(df["DELIV_PER"]),
        "source": "nse_sec_bhavdata_full",
    })
    return out.drop_duplicates(["symbol", "series"]).reset_index(drop=True)


def parse_indices(body: bytes, d: date) -> pd.DataFrame:
    df = _read_csv_bytes(body)
    out = pd.DataFrame({
        "index_name": df["Index Name"].str.upper().str.split().str.join(" "),
        "date": pd.Timestamp(d),
        "open": _num(df["Open Index Value"]),
        "high": _num(df["High Index Value"]),
        "low": _num(df["Low Index Value"]),
        "close": _num(df["Closing Index Value"]),
        "pe": _num(df["P/E"]) if "P/E" in df else None,
        "pb": _num(df["P/B"]) if "P/B" in df else None,
        "div_yield": _num(df["Div Yield"]) if "Div Yield" in df else None,
        "source": "nse_ind_close_all",
    })
    out = out.dropna(subset=["close"]).drop_duplicates("index_name")
    return out.reset_index(drop=True)


PARSERS = {"delivery": parse_delivery, "indices": parse_indices, "bhavcopy": parse_bhavcopy}
TABLES = {
    "bhavcopy": ("prices_daily", ["isin", "date"]),
    "delivery": ("delivery_daily", ["symbol", "series", "date"]),
    "indices": ("index_prices_daily", ["index_name", "date"]),
}
