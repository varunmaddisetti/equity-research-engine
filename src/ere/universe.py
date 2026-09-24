"""The investable universe: NSE constituent CSV merged with config overrides."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ere.config import UniverseConfig, ValuationModel, load_universe_config
from ere.paths import CONFIG_DIR

CSV_COLUMNS = {
    "Company Name": "name",
    "Industry": "industry",
    "Symbol": "symbol",
    "Series": "series",
    "ISIN Code": "isin",
}


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    industry: str
    isin: str
    valuation_model: ValuationModel
    short_history: bool

    @property
    def is_lender(self) -> bool:
        return self.valuation_model in ("residual_income", "insurance")


def read_constituents(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    missing = set(CSV_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{csv_path.name}: missing columns {sorted(missing)}")
    df = df.rename(columns=CSV_COLUMNS)[list(CSV_COLUMNS.values())]
    df = df.apply(lambda c: c.str.strip())
    if df["symbol"].duplicated().any():
        raise ValueError(f"duplicate symbols: {df.loc[df.symbol.duplicated(), 'symbol'].tolist()}")
    if not df["isin"].str.fullmatch(r"INE[A-Z0-9]{9}").all():
        bad = df.loc[~df["isin"].str.fullmatch(r"INE[A-Z0-9]{9}"), "symbol"].tolist()
        raise ValueError(f"malformed ISINs for {bad}")
    return df


def load_universe(config_dir: Path = CONFIG_DIR) -> list[Security]:
    cfg: UniverseConfig = load_universe_config(config_dir)
    df = read_constituents(config_dir / cfg.constituents_csv)
    symbols = set(df["symbol"])

    # Overrides that point at symbols no longer in the index are config rot: fail loudly.
    referenced = (
        {s for v in cfg.valuation_models.values() for s in v}
        | set(cfg.short_history)
        | set(cfg.peer_overrides)  # keys only; peers themselves may sit outside the index
    )
    stale = sorted(referenced - symbols)
    if stale:
        raise ValueError(
            f"universe.yaml references symbols not in {cfg.constituents_csv}: {stale}. "
            "The index probably rebalanced; update the overrides."
        )

    return [
        Security(
            symbol=r.symbol,
            name=r.name,
            industry=r.industry,
            isin=r.isin,
            valuation_model=cfg.model_for(r.symbol),
            short_history=r.symbol in cfg.short_history,
        )
        for r in df.itertuples(index=False)
    ]


def universe_frame(config_dir: Path = CONFIG_DIR) -> pd.DataFrame:
    return pd.DataFrame([s.__dict__ for s in load_universe(config_dir)])
