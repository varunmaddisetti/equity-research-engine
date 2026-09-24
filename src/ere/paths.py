"""Project paths. Override the root with the ERE_ROOT environment variable."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    env = os.environ.get("ERE_ROOT")
    if env:
        return Path(env).resolve()
    # src/ere/paths.py -> repo root is three levels up
    return Path(__file__).resolve().parents[2]


ROOT = project_root()
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
DB_PATH = DATA_DIR / "ere.duckdb"
REPORTS_DIR = ROOT / "reports"
