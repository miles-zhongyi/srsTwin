"""
ML vocabulary index — which message types the SessionTransformer can emit.

Does not load PyTorch at import time; reads ``vocab.json`` from srsTwin/digital-twin
when present. ML build still uses trace templates for the ``decoded`` body (hybrid).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _digital_twin_dir() -> Path:
    env = os.environ.get("DIGITAL_TWIN_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    # poc_StressTest/common/signaling -> poc_StressTest -> DigitalTwins -> srsTwin/digital-twin
    root = Path(__file__).resolve().parents[2]
    return root.parent / "srsTwin" / "digital-twin"


class MlVocabIndex:
    def __init__(self, vocab_path: Path | str | None = None):
        if vocab_path is None:
            vocab_path = _digital_twin_dir() / "vocab.json"
        self.path = Path(vocab_path)
        self._message_names: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            vocab = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(vocab, dict):
            return
        for tok in vocab:
            if tok.startswith("<") or tok.startswith("CELL_"):
                continue
            if "|" in tok:
                self._message_names.add(tok.split("|", 1)[0])
            else:
                self._message_names.add(tok)

    @property
    def loaded(self) -> bool:
        return bool(self._message_names)

    def is_available(self, message_name: str) -> bool:
        if not self._message_names:
            return False
        if message_name in self._message_names:
            return True
        # ML vocab uses S1_* names; RRC may appear as rrcConnectionRequest etc.
        prefix = message_name.split("_", 1)[-1].lower()
        return any(prefix in n.lower() or n.lower() in prefix for n in self._message_names)
