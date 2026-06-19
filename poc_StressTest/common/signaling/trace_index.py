"""
Index of 22_decoded trace samples (templates keyed by message_name / record_id).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .templates import abstract_record


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_samples_path() -> Path:
    env = os.environ.get("TRACE_SAMPLES_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return _repo_root() / "data" / "trace_message_samples.json"


class TraceSampleIndex:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_samples_path()
        self._by_name: dict[str, dict] = {}
        self._by_record: dict[int, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        by_record = data.get("by_record") or {}
        if isinstance(by_record, dict):
            for k, v in by_record.items():
                try:
                    self._by_record[int(k)] = v
                except (TypeError, ValueError):
                    continue
        by_name = data.get("by_name") or {}
        if isinstance(by_name, dict):
            self._by_name.update(by_name)

    @property
    def loaded(self) -> bool:
        return bool(self._by_name or self._by_record)

    def has_sample(self, message_name: str, record_id: int | None = None) -> bool:
        return self.get_template(message_name, record_id) is not None

    def _has_record_for(self, message_name: str) -> bool:
        for payload in self._by_record.values():
            if (payload.get("message_name") or "") == message_name:
                return True
        return False

    def get_template(self, message_name: str, record_id: int | None = None) -> dict | None:
        """Return a fillable template (``<<token>>`` leaves) for ``message_name``."""
        hit = self._by_name.get(message_name)
        if hit is None and record_id is not None:
            hit = self._by_record.get(int(record_id))
        if hit is None:
            for payload in self._by_record.values():
                if payload.get("message_name") == message_name:
                    hit = payload
                    break
        if hit is None:
            return None
        if hit.get("template"):
            return hit["template"]
        msg = hit.get("message")
        if isinstance(msg, dict):
            return abstract_record(msg)
        return None

    def get_record(self, message_name: str) -> dict | None:
        hit = self._by_name.get(message_name)
        if hit and isinstance(hit.get("message"), dict):
            return hit["message"]
        for payload in self._by_record.values():
            if payload.get("message_name") == message_name:
                m = payload.get("message")
                if isinstance(m, dict):
                    return m
        return None
