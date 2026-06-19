"""
Catalog of record_id / message_name pairs and representative samples from 22_decoded.

Samples are matched by message_name (stable per-type id); record_id is the wire
taxonomy from record-id-messages.txt. Index is loaded from cache when available,
otherwise built once from trace files (bounded scan).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

_LINE = re.compile(r"^record_id\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE)
MAX_FILES = int(os.environ.get("TRACE_SAMPLE_MAX_FILES", "60"))
_CACHE_VERSION = 4


def iter_trace_records(path: Path | str):
    """Stream objects from a top-level JSON array without loading the full file."""
    path = Path(path)
    dec = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as fp:
        buf = ""
        while "[" not in buf:
            extra = fp.read(65536)
            if not extra:
                return
            buf += extra
        pos = buf.index("[") + 1

        while True:
            buf = buf[pos:].lstrip()
            pos = 0
            if not buf:
                extra = fp.read(65536)
                if not extra:
                    return
                buf = extra
                continue
            if buf[pos] == "]":
                return
            while True:
                try:
                    obj, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    extra = fp.read(262144)
                    if not extra:
                        raise
                    buf += extra
                    continue
                yield obj
                pos = end
                while pos < len(buf) and buf[pos] in ", \r\n\t":
                    pos += 1
                if pos < len(buf) and buf[pos] == "]":
                    return
                break


def _label(rec: dict) -> str:
    meta = rec.get("decoding_metadata") or {}
    return (
        rec.get("message_name")
        or meta.get("decoded_message_choice")
        or meta.get("normalized_label")
        or ""
    )


def protocol_of_record_id(record_id: int) -> str:
    if record_id < 100:
        return "RRC"
    if record_id < 200:
        return "S1"
    return "X2"


def parse_record_id_messages(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    section = "OTHER"
    entries: list[dict] = []
    seen: set[str] = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") and not _LINE.match(line):
            section = line.lstrip("#").strip().split()[0] or section
            continue
        m = _LINE.match(line)
        if not m:
            continue
        record_id = int(m.group(1))
        for message_name in _split_message_names(m.group(2)):
            key = f"{record_id}:{message_name}"
            if key in seen:
                continue
            seen.add(key)
            proto = protocol_of_record_id(record_id)
            entries.append({
                "key": key,
                "record_id": record_id,
                "message_name": message_name,
                "section": section,
                "protocol": proto,
                "label": f"{section} · record {record_id} · {message_name}",
            })
    return entries


def _split_message_names(blob: str) -> list[str]:
    names: list[str] = []
    for pipe_part in re.split(r"\s*\|\s*", blob):
        for part in pipe_part.split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def _sample_payload(rid: int, rec: dict, path: Path) -> dict:
    return {
        "key": str(rid),
        "record_id": rid,
        "message_name": _label(rec) or rec.get("message_name"),
        "protocol": rec.get("interface") or protocol_of_record_id(rid),
        "source_file": path.name,
        "source_path": str(path),
        "message": rec,
    }


def default_paths() -> tuple[Path, Path, Path, Path]:
    """Return (catalog_txt, trace_dir, cache_json, poc_cache_json)."""
    here = Path(__file__).resolve().parent
    srs_root = here.parent.parent
    poc_root = srs_root.parent / "poc_StressTest"
    catalog = poc_root / "CallFlow" / "record-id-messages.txt"
    trace_dir = srs_root / "22_decoded"
    if not trace_dir.is_dir():
        trace_dir = poc_root / "22_decoded"
    cache = here / "data" / "trace_message_samples.json"
    poc_cache = poc_root / "data" / "trace_message_samples.json"
    return catalog, trace_dir, cache, poc_cache


class TraceCatalog:
    def __init__(self, catalog_path: Path, trace_dir: Path, cache_path: Path | None = None):
        self.catalog_path = catalog_path
        self.trace_dir = trace_dir
        self.cache_path = cache_path
        self.entries = parse_record_id_messages(catalog_path) if catalog_path.is_file() else []
        self.wanted_ids = sorted({e["record_id"] for e in self.entries})
        self.wanted_names = sorted({e["message_name"] for e in self.entries})
        self._lock = threading.Lock()
        self._by_record: dict[int, dict] = {}
        self._by_name: dict[str, dict] = {}
        self._status = {
            "ready": False,
            "building": False,
            "scanned_done": False,
            "found": 0,
            "total": len(self.wanted_ids),
            "entries_found": 0,
            "entries_total": len(self.entries),
            "files_scanned": 0,
            "error": None,
            "cache_loaded": False,
            "trace_dir": str(trace_dir),
        }
        if cache_path and cache_path.is_file():
            self._load_cache(cache_path)

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def list_entries(self) -> list[dict]:
        with self._lock:
            ids = set(self._by_record)
            return [{**e, "found": e["record_id"] in ids} for e in self.entries]

    def get_by_record_id(self, rid) -> dict | None:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return None
        with self._lock:
            hit = self._by_record.get(rid)
        return dict(hit) if hit else None

    def get_sample(self, key: str) -> dict | None:
        key = str(key).strip()
        if key.isdigit():
            return self.get_by_record_id(int(key))
        if ":" in key and key.split(":", 1)[0].isdigit():
            return self.get_by_record_id(int(key.split(":", 1)[0]))
        with self._lock:
            hit = self._by_name.get(key)
        return dict(hit) if hit else None

    def ensure_index(self):
        with self._lock:
            if self._status["scanned_done"] or self._status["building"]:
                return
            self._status["building"] = True
        threading.Thread(target=self._build_index, daemon=True).start()

    def _build_index(self):
        try:
            if not self.trace_dir.is_dir():
                raise FileNotFoundError(f"trace dir not found: {self.trace_dir}")
            wanted_ids = set(self.wanted_ids)
            wanted_names = set(self.wanted_names)
            files = sorted(self.trace_dir.rglob("*_TRC_decoded.json"))[:MAX_FILES]
            files_scanned = 0
            for path in files:
                with self._lock:
                    rem_ids = wanted_ids - set(self._by_record)
                    rem_names = wanted_names - set(self._by_name)
                if not rem_ids and not rem_names:
                    break
                files_scanned += 1
                for rec in iter_trace_records(path):
                    if rec.get("decoding_status") not in (None, "success"):
                        continue
                    rid = rec.get("record_id")
                    label = _label(rec)
                    if rid in rem_ids or label in rem_names:
                        self._store(rid, label, rec, path, rem_ids, rem_names)
                    if not rem_ids and not rem_names:
                        break
            with self._lock:
                self._status.update(
                    files_scanned=files_scanned,
                    found=len(self._by_record),
                    entries_found=sum(1 for e in self.entries if e["record_id"] in self._by_record),
                    ready=True, building=False, scanned_done=True,
                )
            self._save_cache()
        except Exception as exc:
            with self._lock:
                self._status.update(error=str(exc), building=False, ready=True, scanned_done=True)

    def _store(self, rid, label, rec, path, rem_ids, rem_names):
        payload = _sample_payload(rid, rec, path)
        with self._lock:
            if rid in rem_ids and rid not in self._by_record:
                self._by_record[rid] = payload
                rem_ids.discard(rid)
            if label in rem_names and label not in self._by_name:
                self._by_name[label] = payload
                rem_names.discard(label)

    def _load_cache(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if data.get("version") != _CACHE_VERSION or not isinstance(data.get("by_record"), dict):
            return
        with self._lock:
            self._by_record.update({int(k): v for k, v in data["by_record"].items()})
            self._by_name.update(data.get("by_name") or {})
            self._status.update(
                found=len(self._by_record),
                entries_found=sum(1 for e in self.entries if e["record_id"] in self._by_record),
                files_scanned=int(data.get("files_scanned", 0)),
                ready=True, cache_loaded=True, scanned_done=True,
            )

    def _save_cache(self):
        if not self.cache_path:
            return
        with self._lock:
            payload = {
                "version": _CACHE_VERSION,
                "files_scanned": self._status["files_scanned"],
                "by_record": {str(k): v for k, v in self._by_record.items()},
                "by_name": dict(self._by_name),
            }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


_catalog: TraceCatalog | None = None
_catalog_err: str | None = None


def get_catalog() -> tuple[TraceCatalog | None, str | None]:
    global _catalog, _catalog_err
    if _catalog is not None or _catalog_err is not None:
        return _catalog, _catalog_err
    catalog_path, trace_dir, cache_path, poc_cache = default_paths()
    if not catalog_path.is_file():
        _catalog_err = f"catalog file not found: {catalog_path}"
        return None, _catalog_err
    load_cache = poc_cache if poc_cache.is_file() else cache_path
    try:
        _catalog = TraceCatalog(catalog_path, trace_dir, cache_path)
        if load_cache.is_file() and not _catalog._by_record:
            _catalog._load_cache(load_cache)
        _catalog.ensure_index()
    except Exception as exc:
        _catalog_err = str(exc)
    return _catalog, _catalog_err
