"""Synthetic NSE files in the exact published formats, for offline tests.

The market spans 2024-07-01 .. 2024-07-12, so it crosses the legacy -> UDiFF cutover
(8 Jul 2024). 2024-07-05 is treated as a holiday (no files).

As in real bhavcopies (verified on 42 ex-dates, Sept 2026), PREVCLOSE is the raw previous
close: it is NOT adjusted on ex-dates.

Stocks:
  AAA  1:5 split (Rs10 -> Rs2) on 2024-07-09 with a new ISIN; corporate action on record
  BBB  1:1 bonus on 2024-07-03; corporate action on record
  CCC  demerger on 2024-07-10: price 100 -> 60; "Demerger" corporate action on record
  DDD  genuine 30% crash on 2024-07-11; nothing on record
  EEE  renamed from EEEOLD to EEE on 2024-07-08, same ISIN
  FFF  1:10 split on 2024-07-08 with a new ISIN but NO corporate action on record
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

from ere.ingest.nse_daily import UDIFF_START, bhavcopy_files, delivery_file, indices_file, raw_path

SESSIONS = [date(2024, 7, d) for d in (1, 2, 3, 4, 8, 9, 10, 11, 12)]
HOLIDAY = date(2024, 7, 5)

A_OLD, A_NEW = "INE000A01011", "INE000A01029"
B, C, D, E = "INE000B01011", "INE000C01011", "INE000D01011", "INE000E01011"
F_OLD, F_NEW = "INE000F01011", "INE000F01029"


def _closes() -> dict[date, list[tuple[str, str, float, float]]]:
    """date -> list of (symbol, isin, close, prev_close). prev_close is always raw."""
    out: dict[date, list] = {}
    prev: dict[str, float] = {}

    def row(key: str, sym: str, isin: str, close: float) -> tuple:
        r = (sym, isin, round(close, 2), prev.get(key, round(close, 2)))
        prev[key] = round(close, 2)
        return r

    for i, d in enumerate(SESSIONS):
        a = 1000 * 1.01**i  # 1% daily drift
        rows = [
            row("A", "AAA", A_OLD if d < date(2024, 7, 9) else A_NEW,
                a if d < date(2024, 7, 9) else a * 0.2),
            row("B", "BBB", B, 500.0 if d < date(2024, 7, 3) else 250.0),
            row("C", "CCC", C, 100.0 if d < date(2024, 7, 10) else 60.0),
            row("D", "DDD", D, 200.0 if d < date(2024, 7, 11) else 140.0),
            row("E", "EEEOLD" if d < date(2024, 7, 8) else "EEE", E, 50.0),
            row("F", "FFF", F_OLD if d < date(2024, 7, 8) else F_NEW,
                900.0 if d < date(2024, 7, 8) else 90.0),
        ]
        out[d] = rows
    return out


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def legacy_bhavcopy(d: date, rows) -> bytes:
    hdr = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
           "TOTALTRADES,ISIN,\n")
    ts = d.strftime("%d-%b-%Y").upper()
    lines = [
        f"{s},EQ,{c},{c},{c},{c},{c},{pc:.2f},1000,{c * 1000},{ts},50,{i},\n"
        for s, i, c, pc in rows
    ]
    # Noise that must be filtered out: a bond series and a non-equity ISIN.
    lines.append(f"GOIBOND,GS,100,100,100,100,100,100,10,1000,{ts},1,IN0020240001,\n")
    lines.append(f"AAA,N1,100,100,100,100,100,100,10,1000,{ts},1,INE000A07011,\n")
    return _zip(f"cm{d:%d}{d.strftime('%b').upper()}{d:%Y}bhav.csv", hdr + "".join(lines))


def udiff_bhavcopy(d: date, rows) -> bytes:
    hdr = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
           "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
           "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
           "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n")
    iso = d.isoformat()
    lines = [
        f"{iso},{iso},CM,NSE,STK,1,{i},{s},EQ,,,,,{s} LTD,{c},{c},{c},{c},{c},{pc:.2f},,{c},,,"
        f"1000,{c * 1000},50,F1,1,,,,,\n"
        for s, i, c, pc in rows
    ]
    lines.append(f"{iso},{iso},CM,NSE,ETF,9,INF000001011,SOMEETF,EQ,,,,,ETF,10,10,10,10,10,10,"
                 ",10,,,5,50,1,F1,1,,,,,\n")
    return _zip(f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv", hdr + "".join(lines))


def delivery_csv(d: date, rows) -> bytes:
    hdr = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
           "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, "
           "DELIV_PER\n")
    ds = d.strftime("%d-%b-%Y")
    lines = [f"{s}, EQ, {ds}, {pc}, {c}, {c}, {c}, {c}, {c}, {c}, 1000, 1.00, 50, 400, 40.00\n"
             for s, _, c, pc in rows]
    lines.append(f"GOIBOND, GS, {ds}, 100, 100, 100, 100, 100, 100, 100, 10, 0.01, 1, -, -\n")
    return (hdr + "".join(lines)).encode()


def indices_csv(d: date, i: int) -> bytes:
    hdr = ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
           "Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,"
           "Div Yield\n")
    ds = d.strftime("%d-%m-%Y")
    n50 = 24000 + 10 * i
    sc = 18000 + 20 * i
    return (hdr
            + f"Nifty 50,{ds},{n50},{n50},{n50},{n50},10,.04,1,1,22.1,3.5,1.2\n"
            + f"NIFTY Smallcap 100,{ds},{sc},{sc},{sc},{sc},20,.1,1,1,30.2,3.4,.5\n").encode()


def market_files() -> dict[str, bytes]:
    """url -> body for every file that exists."""
    files: dict[str, bytes] = {}
    for i, (d, rows) in enumerate(_closes().items()):
        bf = bhavcopy_files(d)[0]
        files[bf.url] = udiff_bhavcopy(d, rows) if d >= UDIFF_START else legacy_bhavcopy(d, rows)
        files[delivery_file(d).url] = delivery_csv(d, rows)
        files[indices_file(d).url] = indices_csv(d, i)
    return files


def write_raw_cache(raw_dir) -> None:
    """Populate data/raw exactly as a successful download would."""
    for i, (d, rows) in enumerate(_closes().items()):
        bf = bhavcopy_files(d)[0]
        body = udiff_bhavcopy(d, rows) if d >= UDIFF_START else legacy_bhavcopy(d, rows)
        for f, b in ((bf, body), (delivery_file(d), delivery_csv(d, rows)),
                     (indices_file(d), indices_csv(d, i))):
            p = raw_path(raw_dir, f, d)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b)


class FakeClient:
    """Stands in for ExchangeClient: serves market_files(), None (404) otherwise."""

    def __init__(self, files: dict[str, bytes], json_by_params=None) -> None:
        self.files = files
        self.json_by_params = json_by_params or {}
        self.requests: list[str] = []

    def get_bytes(self, url, params=None):
        self.requests.append(url)
        return self.files.get(url)

    def get_json(self, url, params=None):
        self.requests.append(url)
        return self.json_by_params.get((params or {}).get("from_date"), [])


CORP_ACTION_RECORDS = [
    {"symbol": "AAA", "series": "EQ", "isin": A_NEW, "faceVal": "2",
     "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
     "exDate": "09-Jul-2024", "comp": "AAA Ltd"},
    {"symbol": "BBB", "series": "EQ", "isin": B, "faceVal": "10",
     "subject": "Bonus 1:1", "exDate": "03-Jul-2024", "comp": "BBB Ltd"},
    {"symbol": "BBB", "series": "EQ", "isin": B, "faceVal": "10",
     "subject": "Interim Dividend - Rs 2.50 Per Share", "exDate": "11-Jul-2024",
     "comp": "BBB Ltd"},
    {"symbol": "CCC", "series": "EQ", "isin": C, "faceVal": "10",
     "subject": "Demerger", "exDate": "10-Jul-2024", "comp": "CCC Ltd"},
    {"symbol": "DDD", "series": "EQ", "isin": D, "faceVal": "10",
     "subject": "Annual General Meeting", "exDate": "02-Jul-2024", "comp": "DDD Ltd"},
]
