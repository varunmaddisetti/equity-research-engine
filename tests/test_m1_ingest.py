from datetime import date

import pandas as pd
import pytest
from fixtures_nse import (
    A_NEW,
    CORP_ACTION_RECORDS,
    HOLIDAY,
    SESSIONS,
    FakeClient,
    _closes,
    delivery_csv,
    indices_csv,
    legacy_bhavcopy,
    market_files,
    udiff_bhavcopy,
)

from ere.db import connect, init_db
from ere.ingest.corp_actions import (
    ingest_corp_actions,
    parse_subject,
    quarter_windows,
    records_to_frame,
)
from ere.ingest.nse_daily import (
    PRICE_COLS,
    bhavcopy_files,
    candidate_sessions,
    parse_bhavcopy,
    parse_delivery,
    parse_indices,
)
from ere.ingest.runner import ingest_daily


# ------------------------------------------------------------------ file formats
def test_urls_switch_format_at_udiff_cutover():
    before, after = bhavcopy_files(date(2024, 7, 5)), bhavcopy_files(date(2024, 7, 8))
    assert before[0].variant == "legacy" and after[0].variant == "udiff"
    assert before[0].url.endswith("/EQUITIES/2024/JUL/cm05JUL2024bhav.csv.zip")
    assert after[0].url.endswith("/content/cm/BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip")
    assert len(before) == 2  # the other format is tried as a fallback


def test_legacy_and_udiff_parse_to_identical_schema():
    rows = _closes()[date(2024, 7, 1)]
    leg = parse_bhavcopy(legacy_bhavcopy(date(2024, 7, 1), rows), date(2024, 7, 1))
    udf = parse_bhavcopy(udiff_bhavcopy(date(2024, 7, 1), rows), date(2024, 7, 1))
    assert list(leg.columns) == list(udf.columns) == PRICE_COLS
    assert set(leg.source) == {"nse_bhav_legacy"} and set(udf.source) == {"nse_bhav_udiff"}
    cols = ["isin", "symbol", "series", "close", "prev_close", "volume", "traded_value"]
    pd.testing.assert_frame_equal(
        leg[cols].sort_values("isin").reset_index(drop=True),
        udf[cols].sort_values("isin").reset_index(drop=True),
    )
    # bond series, non-equity ISIN and the ETF are filtered out
    assert len(leg) == len(rows) == 6


def test_delivery_parser_handles_spaced_headers_and_dashes():
    rows = _closes()[date(2024, 7, 1)]
    df = parse_delivery(delivery_csv(date(2024, 7, 1), rows), date(2024, 7, 1))
    assert len(df) == 6 and df.delivery_pct.eq(40.0).all()


def test_index_names_normalised():
    df = parse_indices(indices_csv(date(2024, 7, 1), 0), date(2024, 7, 1))
    assert set(df.index_name) == {"NIFTY 50", "NIFTY SMALLCAP 100"}
    assert df.set_index("index_name").loc["NIFTY 50", "pe"] == pytest.approx(22.1)


def test_candidate_sessions_skip_weekends_but_keep_special_sessions():
    s = candidate_sessions(date(2025, 1, 31), date(2025, 2, 3))
    assert s == [date(2025, 1, 31), date(2025, 2, 1), date(2025, 2, 3)]  # budget Saturday


# ------------------------------------------------------------------ runner
def _run(con, tmp_path, client, today=date(2024, 8, 1), **kw):
    return ingest_daily(con, tmp_path / "raw", date(2024, 7, 1), date(2024, 7, 12),
                        client=client, today=today, **kw)


def test_runner_downloads_caches_and_resumes(tmp_path):
    client = FakeClient(market_files())
    with connect(tmp_path / "t.duckdb") as con:
        init_db(con)
        stats = _run(con, tmp_path, client)
        assert stats[("bhavcopy", "ok")] == len(SESSIONS)
        assert stats[("bhavcopy", "missing")] == 1  # the holiday
        assert con.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == 6 * 9
        assert con.execute("SELECT count(*) FROM index_prices_daily").fetchone()[0] == 2 * 9
        assert con.execute("SELECT count(*) FROM delivery_daily").fetchone()[0] == 6 * 9
        status = dict(con.execute(
            "SELECT key, status FROM ingest_log WHERE dataset='bhavcopy'").fetchall())
        assert status[HOLIDAY.isoformat()] == "missing"

        # Second run: nothing to fetch, nothing duplicated.
        client.requests.clear()
        stats2 = _run(con, tmp_path, client)
        assert client.requests == []
        assert stats2[("bhavcopy", "skipped")] == 10
        assert con.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == 54


def test_recent_missing_dates_are_retried(tmp_path):
    client = FakeClient(market_files())
    with connect(tmp_path / "t.duckdb") as con:
        init_db(con)
        _run(con, tmp_path, client, today=date(2024, 7, 8))
        client.requests.clear()
        _run(con, tmp_path, client, today=date(2024, 7, 8), datasets=["indices"])
        # the holiday on 5 Jul is within RECHECK_DAYS of "today", so it is asked for again
        assert any("05072024" in u for u in client.requests)


def test_offline_rebuild_from_cache(tmp_path):
    with connect(tmp_path / "a.duckdb") as con:
        init_db(con)
        _run(con, tmp_path, FakeClient(market_files()))
    with connect(tmp_path / "b.duckdb") as con:
        init_db(con)
        _run(con, tmp_path, None, offline=True)
        assert con.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == 54


def test_network_error_is_logged_not_fatal(tmp_path):
    class Boom(FakeClient):
        def get_bytes(self, url, params=None):
            raise ConnectionError("reset by peer")

    with connect(tmp_path / "t.duckdb") as con:
        init_db(con)
        stats = _run(con, tmp_path, Boom({}), datasets=["bhavcopy"])
        assert stats[("bhavcopy", "error")] == 10
        # errors are not "done": a later run retries them
        stats2 = _run(con, tmp_path, FakeClient(market_files()), datasets=["bhavcopy"])
        assert stats2[("bhavcopy", "ok")] == 9


# ------------------------------------------------------------------ corporate actions
@pytest.mark.parametrize("subject, expected", [
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
     [("split", 0.2, None)]),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
     [("split", 0.1, None)]),
    ("Consolidation Of Shares From Rs 1/- Per Share To Rs 10/- Per Share",
     [("consolidation", 10.0, None)]),
    ("Bonus 1:1", [("bonus", 0.5, None)]),
    ("Bonus 3:2", [("bonus", 0.4, None)]),
    ("Dividend - Rs 5 Per Share", [("dividend", None, 5.0)]),
    ("Final Dividend- Rs.1.20 Per Share", [("dividend", None, 1.2)]),
    ("Interim Dividend - Rs 2.50 Per Share / Special Dividend - Re 1 Per Share",
     [("dividend", None, 3.5)]),
    ("Bonus 1:2 / Final Dividend - Rs 3 Per Share",
     [("bonus", 2 / 3, None), ("dividend", None, 3.0)]),
    ("Rights 1:5 @ Premium Rs 100/-", [("rights", None, 100.0)]),
    ("Demerger", [("demerger", None, None)]),
    ("Buy Back", [("buyback", None, None)]),
    ("Annual General Meeting", []),
])
def test_parse_subject(subject, expected):
    got = [(a.action, a.factor, a.amount) for a in parse_subject(subject)]
    assert len(got) == len(expected)
    for (ga, gf, gm), (ea, ef, em) in zip(got, expected, strict=True):
        assert ga == ea
        assert gf == pytest.approx(ef) if ef is not None else gf is None
        assert gm == pytest.approx(em) if em is not None else gm is None


def test_percentage_dividend_uses_face_value():
    [a] = parse_subject("Dividend - 25%", face_value=10)
    assert a.amount == pytest.approx(2.5)


def test_records_to_frame_drops_non_actions():
    df = records_to_frame(CORP_ACTION_RECORDS)
    assert sorted(df.action) == ["bonus", "demerger", "dividend", "split"]
    assert df.set_index("action").loc["split", "isin"] == A_NEW


def test_quarter_windows_cover_range_without_gaps():
    w = quarter_windows(date(2016, 2, 15), date(2016, 12, 31))
    assert w[0] == (date(2016, 2, 15), date(2016, 3, 31))
    assert w[-1] == (date(2016, 10, 1), date(2016, 12, 31))
    assert len(w) == 4


def test_ingest_corp_actions_caches_closed_windows(tmp_path):
    client = FakeClient({}, json_by_params={"01-07-2024": CORP_ACTION_RECORDS})
    with connect(tmp_path / "t.duckdb") as con:
        init_db(con)
        ingest_corp_actions(con, tmp_path / "raw", date(2024, 7, 1), date(2024, 9, 30),
                            client=client, today=date(2025, 1, 1))
        assert con.execute("SELECT count(*) FROM corp_actions").fetchone()[0] == 4
        client.requests.clear()
        ingest_corp_actions(con, tmp_path / "raw", date(2024, 7, 1), date(2024, 9, 30),
                            client=client, today=date(2025, 1, 1))
        assert client.requests == []  # closed quarter, already done


def test_zip_without_csv_extension_is_read():
    """NSE served sec_bhavdata_full_08082022.csv as a zip whose member had no .csv suffix."""
    import io
    import zipfile

    rows = _closes()[date(2024, 7, 1)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sec_bhavdata_full_08082022", delivery_csv(date(2022, 8, 8), rows))
    df = parse_delivery(buf.getvalue(), date(2022, 8, 8))
    assert len(df) == 6
