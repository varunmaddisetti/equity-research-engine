"""Synthetic NSE files in the exact published formats, for offline tests.

The market spans 2024-07-01 .. 2024-07-12, so it crosses the legacy -> UDiFF cutover
(8 Jul 2024). 2024-07-05 is treated as a holiday (no files).

Stocks:
  AAA  1:5 face-value split (Rs10 -> Rs2) on 2024-07-09 WITH a new ISIN
  BBB  1:1 bonus on 2024-07-03
  CCC  base price cut 20% on 2024-07-10 with no corporate action (e.g. demerger)
  DDD  genuine 30% crash on 2024-07-11 (no base adjustment)
  EEE  renamed from EEEOLD to EEE on 2024-07-08, same ISIN
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


def _closes() -> dict[date, list[tuple[str, str, float, float]]]:
    """date -> list of (symbol, isin, close, prev_close)."""
    out: dict[date, list] = {}
    prev: dict[str, float] = {}
    for i, d in enumerate(SESSIONS):
        rows = []
        # AAA: ~1000 before split, ~200 after (1% daily drift)
        a = 1000 * 1.01**i
        if d < date(2024, 7, 9):
            rows.append(("AAA", A_OLD, round(a, 2), prev.get("AAA", a)))
        else:
            pc = prev["AAA"] * 0.2 if d == date(2024, 7, 9) else prev["AAA"]
            rows.append(("AAA", A_NEW, round(a * 0.2, 2), pc))
        prev["AAA"] = rows[-1][2]
        # BBB: 500 -> 250 on bonus
        b = 500.0 if d < date(2024, 7, 3) else 250.0
        pcb = prev.get("BBB", b) * (0.5 if d == date(2024, 7, 3) else 1.0)
        rows.append(("BBB", B, b, pcb))
        prev["BBB"] = b
        # CCC: 100 -> 80 with exchange base adjustment
        c = 100.0 if d < date(2024, 7, 10) else 80.0
        pcc = prev.get("CCC", c) * (0.8 if d == date(2024, 7, 10) else 1.0)
        rows.append(("CCC", C, c, pcc))
        prev["CCC"] = c
        # DDD: real crash, prev_close NOT adjusted
        dd = 200.0 if d < date(2024, 7, 11) else 140.0
        rows.append(("DDD", D, dd, prev.get("DDD", dd)))
        prev["DDD"] = dd
        # EEE: rename
        sym = "EEEOLD" if d < date(2024, 7, 8) else "EEE"
        rows.append((sym, E, 50.0, 50.0))
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
    {"symbol": "DDD", "series": "EQ", "isin": D, "faceVal": "10",
     "subject": "Annual General Meeting", "exDate": "02-Jul-2024", "comp": "DDD Ltd"},
]
