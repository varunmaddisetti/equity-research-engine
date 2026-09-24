"""Synthetic NSE results filings modelled on live files (Sept 2026).

Company TESTCO (ISIN INE000T01011), FY25 = Apr-2024..Mar-2025, figures in rupees:
  quarters (revenue): Q1 100 cr, Q2 110 cr, Q3 120 cr (results filings, in-bse-fin taxonomy)
  Q4 + FY + balance sheet + cash flow: Integrated Filing (in-capmkt taxonomy), with
  dimensional contexts that must be ignored, and a later REVISION of FY PAT.
  A standalone Q3 filing exists alongside the consolidated one.
Bank TESTBANK (INE000K01011): one integrated banking filing.
"""

from __future__ import annotations

CR = 10_000_000
T_ISIN, K_ISIN = "INE000T01011", "INE000K01011"

LEGACY_NS = ('xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin" '
             'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
             'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
             'xmlns:iso4217="http://www.xbrl.org/2003/iso4217"')
INTEG_NS = ('xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt" '
            'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
            'xmlns:iso4217="http://www.xbrl.org/2003/iso4217"')


def _doc(ns: str, body: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><xbrli:xbrl {ns}>{body}</xbrli:xbrl>'.encode()


def _ctx(cid, start=None, end=None, instant=None, dim=None):
    period = (f"<xbrli:instant>{instant}</xbrli:instant>" if instant else
              f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>")
    scen = (f"<xbrli:scenario><xbrldi:explicitMember dimension=\"{dim[0]}\">{dim[1]}"
            f"</xbrldi:explicitMember></xbrli:scenario>") if dim else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier '
            f'scheme="http://www.nseindia.com/NSESymbol">X</xbrli:identifier></xbrli:entity>'
            f"<xbrli:period>{period}</xbrli:period>{scen}</xbrli:context>")


UNITS = ('<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
         '<xbrli:unit id="INRPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>'
         'iso4217:INR</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>'
         'xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>')


def _facts(prefix, ctx, items, unit="INR"):
    return "".join(f'<{prefix}:{k} contextRef="{ctx}" unitRef="{unit}" decimals="-5">{v}'
                   f"</{prefix}:{k}>" for k, v in items.items())


def _pl(rev_cr, scale=1.0):
    """A consistent P&L in rupees from revenue in crore."""
    rev = rev_cr * CR
    oth = 5 * CR * scale
    fin, dep = 4 * CR * scale, 6 * CR * scale
    exp = rev * 0.8 + fin + dep
    pbt = rev + oth - exp
    tax = pbt * 0.25
    return {
        "RevenueFromOperations": rev, "OtherIncome": oth, "Income": rev + oth,
        "FinanceCosts": fin, "DepreciationDepletionAndAmortisationExpense": dep,
        "Expenses": exp, "ProfitBeforeTax": pbt, "TaxExpense": tax,
        "ProfitLossForPeriodFromContinuingOperations": pbt - tax,
        "ProfitLossForPeriod": pbt - tax,
        "ProfitOrLossAttributableToOwnersOfParent": pbt - tax,
        "PaidUpValueOfEquityShareCapital": 50 * CR,
    }


def legacy_quarter(q_start, q_end, ytd_start, rev_cr, ytd_rev_cr, basis="Consolidated"):
    body = (_ctx("OneD", q_start, q_end) + _ctx("FourD", ytd_start, q_end) + UNITS
            + '<in-bse-fin:Symbol contextRef="OneD">TESTCO</in-bse-fin:Symbol>'
            + f'<in-bse-fin:NatureOfReportStandaloneConsolidated contextRef="OneD">{basis}'
              "</in-bse-fin:NatureOfReportStandaloneConsolidated>"
            + _facts("in-bse-fin", "OneD", _pl(rev_cr))
            + _facts("in-bse-fin", "FourD", _pl(ytd_rev_cr, 3.0))
            + _facts("in-bse-fin", "OneD", {"FaceValueOfEquityShareCapital": 10,
                                            "BasicEarningsLossPerShareFromContinuingOperations":
                                            1.5}, unit="INRPerShare"))
    return _doc(LEGACY_NS, body)


def integrated_annual(pat_override=None):
    fy = _pl(460, 4.0)  # 100+110+120+130
    if pat_override is not None:
        fy["ProfitLossForPeriod"] = pat_override
        fy["ProfitOrLossAttributableToOwnersOfParent"] = pat_override
    bs = {
        "Assets": 900 * CR, "EquityAndLiabilities": 900 * CR, "CashAndCashEquivalents": 40 * CR,
        "BankBalanceOtherThanCashAndCashEquivalents": 10 * CR, "CurrentInvestments": 5 * CR,
        "BorrowingsNoncurrent": 100 * CR, "BorrowingsCurrent": 50 * CR,
        "EquityAttributableToOwnersOfParent": 500 * CR, "Inventories": 80 * CR,
        "TradeReceivablesCurrent": 120 * CR,
    }
    cf = {"CashFlowsFromUsedInOperatingActivities": 70 * CR,
          "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": 30 * CR,
          "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities": 2 * CR}
    body = (_ctx("OneD", "2025-01-01", "2025-03-31") + _ctx("FourD", "2024-04-01", "2025-03-31")
            + _ctx("OneI", instant="2025-03-31") + _ctx("PY_I", instant="2024-03-31")
            + _ctx("OneExpenses1D", "2025-01-01", "2025-03-31",
                   dim=("in-capmkt:DetailsOfOtherExpensesAxis", "in-capmkt:OtherExpenses1Member"))
            + UNITS
            + _facts("in-capmkt", "OneD", _pl(130))
            + _facts("in-capmkt", "FourD", fy)
            + _facts("in-capmkt", "OneI", bs)
            + _facts("in-capmkt", "PY_I", {"Assets": 800 * CR, "EquityAndLiabilities": 800 * CR})
            + _facts("in-capmkt", "FourD", cf)
            # a breakdown line in a dimensional context: must NOT become revenue
            + _facts("in-capmkt", "OneExpenses1D", {"RevenueFromOperations": 1 * CR,
                                                    "SomeUnmappedElement": 3 * CR})
            + _facts("in-capmkt", "OneD", {"SomeUnmappedElement": 7 * CR})
            + _facts("in-capmkt", "OneD", {"FaceValueOfEquityShareCapital": 10},
                     unit="INRPerShare"))
    return _doc(INTEG_NS, body)


def integrated_bank():
    d = {"InterestEarned": 2000 * CR, "InterestExpended": 1200 * CR, "OtherIncome": 300 * CR,
         "OperatingExpenses": 500 * CR, "OperatingProfitBeforeProvisionAndContingencies": 600 * CR,
         "ProvisionsOtherThanTaxAndContingencies": 100 * CR,
         "ProfitLossFromOrdinaryActivitiesBeforeTax": 500 * CR, "TaxExpense": 125 * CR,
         "ProfitLossFromOrdinaryActivitiesAfterTax": 375 * CR, "ProfitLossForThePeriod": 375 * CR,
         "PaidUpValueOfEquityShareCapital": 160 * CR, "PercentageOfGrossNpa": 0.8,
         "PercentageOfNpa": 0.2}
    bs = {"Advances": 80000 * CR, "Deposits": 95000 * CR, "Assets": 120000 * CR,
          "CapitalAndLiabilities": 120000 * CR}
    body = (_ctx("FourD", "2024-04-01", "2025-03-31") + _ctx("OneI", instant="2025-03-31")
            + UNITS + _facts("in-capmkt", "FourD", d) + _facts("in-capmkt", "OneI", bs))
    return _doc(INTEG_NS, body)


A = "https://nsearchives.nseindia.com/corporate/xbrl/"
FILES = {
    A + "INDAS_1_Q1_C.xml": legacy_quarter("2024-04-01", "2024-06-30", "2024-04-01", 100, 100),
    A + "INDAS_2_Q2_C.xml": legacy_quarter("2024-07-01", "2024-09-30", "2024-04-01", 110, 210),
    A + "INDAS_3_Q3_C.xml": legacy_quarter("2024-10-01", "2024-12-31", "2024-04-01", 120, 330),
    A + "INDAS_4_Q3_S.xml": legacy_quarter("2024-10-01", "2024-12-31", "2024-04-01", 90, 250,
                                           basis="Standalone"),
    A + "INTEGRATED_FILING_INDAS_5_FY_C.xml": integrated_annual(),
    A + "INTEGRATED_FILING_INDAS_6_FY_C_REV.xml": integrated_annual(pat_override=999 * CR),
    A + "INTEGRATED_FILING_BANKING_7_FY.xml": integrated_bank(),
}


def _res(symbol, isin, frm, to, cons, filed, url, bank="N"):
    return {"symbol": symbol, "isin": isin, "fromDate": frm, "toDate": to,
            "consolidated": cons, "audited": "Un-Audited", "bank": bank,
            "filingDate": filed, "xbrl": url, "period": "Quarterly"}


RESULTS_Q = {
    "TESTCO": [
        _res("TESTCO", T_ISIN, "01-Apr-2024", "30-Jun-2024", "Consolidated", "05-Aug-2024 18:00",
             A + "INDAS_1_Q1_C.xml"),
        _res("TESTCO", T_ISIN, "01-Jul-2024", "30-Sep-2024", "Consolidated", "05-Nov-2024 18:00",
             A + "INDAS_2_Q2_C.xml"),
        _res("TESTCO", T_ISIN, "01-Oct-2024", "31-Dec-2024", "Consolidated", "05-Feb-2025 18:00",
             A + "INDAS_3_Q3_C.xml"),
        _res("TESTCO", T_ISIN, "01-Oct-2024", "31-Dec-2024", "Non-Consolidated",
             "05-Feb-2025 17:00", A + "INDAS_4_Q3_S.xml"),
        # pre-2018 style row without XBRL: must be skipped
        _res("TESTCO", T_ISIN, "01-Oct-2016", "31-Dec-2016", "Consolidated", "05-Feb-2017 18:00",
             A + "-"),
    ],
}

INTEGRATED = {
    "TESTCO": [
        {"symbol": "TESTCO", "qe_Date": "31-MAR-2025", "consolidated": "Consolidated",
         "audited": "Audited", "type": "Integrated Filing- Financials", "type_Sub": "Original",
         "broadcast_Date": "20-May-2025 19:00:00", "creation_Date": "20-May-2025 19:00:00",
         "xbrl": A + "INTEGRATED_FILING_INDAS_5_FY_C.xml"},
        {"symbol": "TESTCO", "qe_Date": "31-MAR-2025", "consolidated": "Consolidated",
         "audited": "Audited", "type": "Integrated Filing- Financials", "type_Sub": "Revision",
         "broadcast_Date": None, "creation_Date": "10-Sep-2025 17:54:18",
         "xbrl": A + "INTEGRATED_FILING_INDAS_6_FY_C_REV.xml"},
    ],
    "TESTBANK": [
        {"symbol": "TESTBANK", "qe_Date": "31-MAR-2025", "consolidated": "Standalone",
         "audited": "Audited", "type": "Integrated Filing- Financials", "type_Sub": "Original",
         "broadcast_Date": "25-Apr-2025 18:00:00", "creation_Date": "25-Apr-2025 18:00:00",
         "xbrl": A + "INTEGRATED_FILING_BANKING_7_FY.xml"},
    ],
}


class FakeNSE:
    """Serves the index endpoints and XBRL files like ExchangeClient."""

    def __init__(self):
        self.requests: list[str] = []

    def get_json(self, url, params=None):
        params = params or {}
        self.requests.append(f"{url}?{params}")
        sym = params.get("symbol")
        if "integrated" in url:
            return INTEGRATED.get(sym, [])
        if params.get("period") == "Quarterly":
            return RESULTS_Q.get(sym, [])
        return []

    def get_bytes(self, url, params=None):
        self.requests.append(url)
        return FILES.get(url)
