"""Sum-of-the-parts for holding companies (config/sotp.yaml).

value per share = [ sum(listed stakes at market) + sum(unlisted stakes at stated value)
                    - holdco net debt ] x (1 - holdco discount) / holdco shares

Listed stakes are valued as shares_held x latest close of the listed subsidiary, which only
needs the subsidiary's price (NSE bhavcopy has every listed company) - not its share count.
The stake sizes change rarely but must be kept current from the holdco's annual report.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import yaml


def load_sotp(path: Path) -> dict:
    if not path.exists():
        return {}
    return (yaml.safe_load(path.read_text()) or {}).get("holdcos", {}) or {}


def run_sotp(con: duckdb.DuckDBPyConnection, spec: dict, holdco_shares: float,
             holdco_net_debt: float, discount: float, as_of) -> tuple[float | None, dict]:
    parts, total, missing = [], 0.0, []
    for h in spec.get("listed", []) or []:
        sym, held = h.get("symbol"), h.get("shares_held")
        if not sym or not held:
            missing.append(f"listed {sym or '?'}: shares_held not set")
            continue
        row = con.execute("SELECT close FROM prices_daily WHERE symbol = ? AND date <= ? "
                          "ORDER BY date DESC LIMIT 1", [sym, as_of]).fetchone()
        if not row:
            missing.append(f"listed {sym}: no price")
            continue
        v = float(held) * float(row[0])
        parts.append({"name": sym, "type": "listed", "value": v, "price": float(row[0]),
                      "shares_held": float(held)})
        total += v
    for u in spec.get("unlisted", []) or []:
        if u.get("value_cr") is None or u.get("stake") is None:
            missing.append(f"unlisted {u.get('name', '?')}: value_cr / stake not set")
            continue
        v = float(u["value_cr"]) * 1e7 * float(u["stake"])
        parts.append({"name": u.get("name"), "type": "unlisted", "value": v,
                      "basis": u.get("basis", "")})
        total += v
    detail = {"parts": parts, "missing_inputs": missing, "holdco_discount": discount,
              "holdco_net_debt": holdco_net_debt}
    if missing or not parts or not (np.isfinite(holdco_shares) and holdco_shares > 0):
        return None, detail
    value = (total - (holdco_net_debt if np.isfinite(holdco_net_debt) else 0.0)) * (1 - discount)
    detail["gross_value"] = total
    return value / holdco_shares, detail
