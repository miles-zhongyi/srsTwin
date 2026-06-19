"""
Paths and environment for the srsTwin signaling ML generator.

All scripts import from here instead of hard-coded machine paths. Override via:

  DIGITAL_TWIN_DATA_DIR   — model artifacts (sessions.json, checkpoints/, …)
  DIGITAL_TWIN_TRACE_DIR  — decoded trace JSON tree (22_decoded)
  SRS_TRACE_DIR           — alias for DIGITAL_TWIN_TRACE_DIR

Defaults:
  DATA  → this package directory (srsTwin/digital-twin)
  TRACE → srsTwin/22_decoded, else poc_StressTest/22_decoded, else digital-twin/merged
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SRS_ROOT = ROOT_DIR.parent
WORKSPACE_ROOT = SRS_ROOT.parent


def ensure_import_path() -> Path:
    """Put the package root on sys.path (for validation/*.py subprocess runs)."""
    root = str(ROOT_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    return ROOT_DIR


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.exists() else None


def resolve_data_dir() -> Path:
    return _env_path("DIGITAL_TWIN_DATA_DIR") or ROOT_DIR


def resolve_trace_dir() -> Path:
    for key in ("DIGITAL_TWIN_TRACE_DIR", "SRS_TRACE_DIR"):
        p = _env_path(key)
        if p is not None:
            return p
    for candidate in (
        SRS_ROOT / "22_decoded",
        WORKSPACE_ROOT / "poc_StressTest" / "22_decoded",
        ROOT_DIR / "merged",
    ):
        if candidate.is_dir():
            return candidate
    return SRS_ROOT / "22_decoded"


DATA_DIR = resolve_data_dir()
TRACE_DIR = resolve_trace_dir()
CHECKPOINT_DIR = DATA_DIR / "checkpoints"
GRAPHS_DIR = DATA_DIR / "graphs"

SESSIONS_FILE = DATA_DIR / "sessions.json"
VOCAB_FILE = DATA_DIR / "vocab.json"
ARRIVAL_MODEL_FILE = DATA_DIR / "arrival_model.json"
CHECKPOINT_FILE = CHECKPOINT_DIR / "best_model.pt"
TRAINING_LOG_FILE = DATA_DIR / "training_log.json"
VALIDATION_REPORT_FILE = DATA_DIR / "validation_report.json"
ANOMALY_SCORES_FILE = DATA_DIR / "anomaly_scores.json"
DEFAULT_SYNTHETIC_OUTPUT = DATA_DIR / "synthetic_traces.json"


def token_split_file(split: str) -> Path:
    """train | val | test token JSON."""
    return DATA_DIR / f"{split}_tokens.json"


def ensure_dirs() -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
