"""
Per-message-type signaling source configuration (trace vs ML vs auto).

Persisted to ``data/message_sources.json`` under the poc_StressTest repo root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

VALID_MODES = frozenset({"trace", "ml", "auto"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_sources_path() -> Path:
    env = os.environ.get("MESSAGE_SOURCES_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return _repo_root() / "data" / "message_sources.json"


class MessageSourcesConfig:
    """Maps wire ``message_name`` → ``trace`` | ``ml`` | ``auto``."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_sources_path()
        self._sources: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self._sources = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._sources = {}
            return
        raw = data.get("sources") if isinstance(data, dict) else {}
        self._sources = {
            str(k): v for k, v in (raw or {}).items()
            if str(v) in VALID_MODES
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "sources": dict(sorted(self._sources.items()))}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get_mode(self, message_name: str) -> str:
        return self._sources.get(message_name, self._sources.get("*", "auto"))

    def set_mode(self, message_name: str, mode: str) -> None:
        mode = str(mode).strip().lower()
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode {mode!r} (want trace|ml|auto)")
        self._sources[str(message_name)] = mode

    def list_for_catalog(self, catalog) -> list[dict]:
        """All known wire names with current mode and availability hints."""
        names: set[str] = set()
        names.update(catalog.MESSAGE_NAMES.values())
        names.update(getattr(catalog, "RECORD_IDS", {}).keys())
        names.update(catalog.templates.keys())
        out = []
        for name in sorted(names):
            iface, protocol = catalog._iface_for(name)
            proto = "RRC" if protocol == "RRC" else ("S1" if protocol == "S1AP" else "X2" if "X2" in name else "OTHER")
            rid = catalog.record_id_for(name)
            out.append({
                "message_name": name,
                "record_id": rid,
                "protocol": proto,
                "interface": iface,
                "mode": self.get_mode(name),
            })
        return out
