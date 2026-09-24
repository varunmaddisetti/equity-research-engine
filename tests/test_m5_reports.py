import re

import pytest
from fixtures_market import load_market
from typer.testing import CliRunner

from ere.analytics.build import build_analytics
from ere.config import load_universe_config, load_valuation_config
from ere.db import connect, init_db
from ere.paths import CONFIG_DIR
from ere.report.build import build_context, build_reports, fy_label, q_label
from ere.report.charts import TOKENS, line_chart

VAL, UNI = load_valuation_config(), load_universe_config()


@pytest.fixture
def con(tmp_path):
    from ere.valuation.build import build_valuation

    c = connect(tmp_path / "r.duckdb")
    init_db(c)
    load_market(c)
    build_analytics(c, VAL, UNI)
    build_valuation(c, VAL, UNI, CONFIG_DIR / "sotp.yaml", history_years=2)
    yield c
    c.close()


def test_labels():
    import pandas as pd
    assert fy_label(pd.Timestamp("2025-03-31")) == "FY25"
    assert q_label(pd.Timestamp("2025-06-30")) == "Q1 FY26"
    assert q_label(pd.Timestamp("2026-03-31")) == "Q4 FY26"


def test_svg_uses_theme_tokens_not_hex():
    import pandas as pd
    s = pd.Series([1, 2, 3], index=pd.date_range("2025-01-01", periods=3))
    svg = line_chart({"A": s, "B": s * 2}, "t")
    assert "var(--series-1)" in svg and "var(--series-2)" in svg
    for hexv in TOKENS:
        assert hexv not in svg
    assert 'width="100%"' in svg


def test_all_reports_render_with_every_section(con, tmp_path):
    stats = build_reports(con, tmp_path / "site", VAL)
    assert stats == {"reports": 5, "errors": 0}
    html = (tmp_path / "site" / "ALPHA.html").read_text()
    for n in range(1, 11):
        assert "<h2 id=" in html and f">{n}. " in html
    assert "not a recommendation" in html and "SEBI" in html
    assert html.count("<svg") >= 4
    assert "&#34;" not in html.split("</style>")[0]   # CSS must not be HTML-escaped
    bank = (tmp_path / "site" / "BANKX.html").read_text()
    assert "Residual income" in bank and "Net interest income" in bank
    idx = (tmp_path / "site" / "index.html").read_text()
    assert len(re.findall(r'href="\w+\.html"', idx)) == 5


def test_context_values(con):
    ctx = build_context(con, "GAMMA", VAL)
    assert any(f["label"] == "Low liquidity" for f in ctx["flags_on"])
    assert ctx["dcf"] and len(ctx["dcf"]["scenarios"]) == 3
    assert ctx["dcf"]["grid"] and len(ctx["dcf"]["grid"]["rows"]) == 5


def test_cli_report_single(con, tmp_path, monkeypatch):
    import ere.cli as cli

    db = tmp_path / "r.duckdb"
    con.close()
    monkeypatch.setattr(cli, "connect", lambda read_only=False: connect(db, read_only))
    res = CliRunner().invoke(cli.app, ["report", "ALPHA", "--out", str(tmp_path / "out")])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "out" / "ALPHA.html").exists()
