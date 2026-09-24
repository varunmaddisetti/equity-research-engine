import time
from datetime import datetime

import httpx
import pytest

from ere.db import connect, init_db
from ere.http import ExchangeClient, StalledRequest
from ere.ingest.xbrl import ingest_xbrl


class Trickle(httpx.SyncByteStream):
    """Sends one byte, then goes quiet past the deadline (the NSE 'tarpit')."""

    def __iter__(self):
        yield b"<"
        time.sleep(0.4)
        yield b"x/>"


def client_for(handler, **kw):
    return ExchangeClient(min_interval_s=0, prime_url=None, max_seconds=0.2,
                          backoff_s=(0.0,), transport=httpx.MockTransport(handler), **kw)


def test_stalled_download_is_abandoned_and_retried_on_fresh_connection():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, stream=Trickle())
        return httpx.Response(200, content=b"<ok/>")

    c = client_for(handler)
    assert c.get_bytes("https://x/y.xml") == b"<ok/>"
    assert len(calls) == 2 and c.reconnects == 1


def test_permanent_stall_raises_after_attempts():
    c = client_for(lambda r: httpx.Response(200, stream=Trickle()), attempts=2)
    t0 = time.monotonic()
    with pytest.raises(StalledRequest):
        c.get_bytes("https://x/y.xml")
    assert time.monotonic() - t0 < 3      # bounded, never an indefinite wait
    assert c.reconnects == 2


def test_404_is_none_and_503_is_retried():
    seq = iter([503, 200])
    c = client_for(lambda r: httpx.Response(next(seq), content=b"ok"))
    assert c.get_bytes("https://x/a") == b"ok"
    assert client_for(lambda r: httpx.Response(404)).get_bytes("https://x/b") is None


def test_min_interval_can_only_increase():
    c = client_for(lambda r: httpx.Response(200))
    c.set_min_interval(1.0)
    c.set_min_interval(0.1)
    assert c._min_interval == 1.0


class AlwaysFails:
    def __init__(self):
        self.n = 0

    def get_bytes(self, url, params=None):
        self.n += 1
        raise StalledRequest("tarpit")


def test_xbrl_run_pauses_then_stops_instead_of_hammering(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    init_db(con)
    for i in range(20):
        con.execute(
            "INSERT INTO filings (filing_id, isin, symbol, source, period_end, basis, is_bank, "
            "is_revision, filed_at, xbrl_url) VALUES (?, 'INE0', 'X', 'nse_results', "
            "'2024-03-31', 'standalone', FALSE, FALSE, ?, ?)",
            [f"F{i:02d}.xml", datetime(2024, 5, 1), f"https://x/F{i:02d}.xml"])
    client, msgs = AlwaysFails(), []
    stats = ingest_xbrl(con, tmp_path / "raw", client=client, cooldown_s=0,
                        on_progress=lambda s, m: msgs.append(m))
    assert stats["cooldowns"] == 1 and stats["stopped"] == 1
    assert client.n == 5 + 3                 # 5 -> pause -> 3 more -> stop
    assert any("pausing" in m for m in msgs)
    assert any(m.startswith("[1/20] downloading") for m in msgs)
    left = con.execute("SELECT count(*) FROM filings WHERE status = 'pending'").fetchone()[0]
    assert left == 12                        # untouched, picked up by the next run
