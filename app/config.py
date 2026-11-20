"""Configuration and persisted settings management (supports both dev mode and PyInstaller desktop .exe)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from pydantic import BaseModel

IS_FROZEN = getattr(sys, "frozen", False)

# Read-only bundled assets (templates/, static/) live in _MEIPASS when packaged as a desktop .exe
if IS_FROZEN and hasattr(sys, "_MEIPASS"):
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS"))
    EXE_DIR = Path(sys.executable).resolve().parent
else:
    RESOURCE_DIR = Path(__file__).resolve().parent.parent
    EXE_DIR = RESOURCE_DIR

BASE_DIR = RESOURCE_DIR
STATIC_DIR = RESOURCE_DIR / "static"

# Writable runtime state (jobs.db, settings.json) lives in %LOCALAPPDATA%\RWS\PatentTMEstimator when frozen
if IS_FROZEN:
    _local_appdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    DATA_DIR = Path(_local_appdata) / "RWS" / "PatentTMEstimator"
else:
    DATA_DIR = RESOURCE_DIR / "data"

SETTINGS_PATH = DATA_DIR / "settings.json"


class AppSettings(BaseModel):
    epo_ops_key: str = ""
    epo_ops_secret: str = ""
    default_sample_size: int = 30
    default_language: str = "EN"
    default_include_rte: bool = False
    # Standard RWS Trados Studio 22 payable weights (0.30 for Reps/100%/95-99%, 0.50 for 85-94%, 1.00 for <85%)
    weight_repetitions: float = 0.30
    weight_100: float = 0.30
    weight_95_99: float = 0.30
    weight_85_94: float = 0.50
    weight_75_84: float = 1.00
    weight_50_74: float = 1.00
    weight_no_match: float = 1.00


def _load_dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    for candidate in (EXE_DIR / ".env", RESOURCE_DIR / ".env"):
        if candidate.exists():
            for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return values


def load_settings() -> AppSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dotenv_vals = _load_dotenv()
    data: dict = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    if not data.get("epo_ops_key"):
        data["epo_ops_key"] = os.environ.get("EPO_OPS_KEY") or dotenv_vals.get("EPO_OPS_KEY", "")
    if not data.get("epo_ops_secret"):
        data["epo_ops_secret"] = os.environ.get("EPO_OPS_SECRET") or dotenv_vals.get("EPO_OPS_SECRET", "")

    return AppSettings(**data)


def save_settings(settings: AppSettings) -> AppSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    return settings
