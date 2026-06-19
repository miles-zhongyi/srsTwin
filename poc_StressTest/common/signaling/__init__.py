"""
Pluggable signalling layer for the digital twin.

``get_catalog()`` returns the LTE/NR message catalog.
``get_dispatcher()`` wraps it with per-type trace/ML source selection.
"""
from __future__ import annotations

import os

from .catalog import SignalingCatalog, twin
from .dispatcher import SignalingDispatcher
from .lte import LteCatalog
from .message_sources import MessageSourcesConfig
from .ml_source import MlVocabIndex
from .nr import NrCatalog
from .trace_index import TraceSampleIndex

_CATALOGS = {"lte": LteCatalog, "nr": NrCatalog}
_catalog_cache: dict[tuple[str, str], SignalingCatalog] = {}
_dispatcher_cache: dict[tuple[str, str, str, str], SignalingDispatcher] = {}


def get_catalog(tech: str | None = None, templates_path: str | None = None) -> SignalingCatalog:
    tech = (tech or os.environ.get("RADIO_TECH", "lte")).strip().lower()
    if templates_path is None:
        templates_path = os.environ.get("LTE_TEMPLATES", "")
    key = (tech, templates_path or "")
    if key not in _catalog_cache:
        cls = _CATALOGS.get(tech)
        if cls is None:
            raise ValueError(f"unknown RADIO_TECH={tech!r} (known: {', '.join(_CATALOGS)})")
        _catalog_cache[key] = cls(templates_path=templates_path or None)
    return _catalog_cache[key]


def get_dispatcher(
    tech: str | None = None,
    templates_path: str | None = None,
    sources_path: str | None = None,
    trace_samples_path: str | None = None,
) -> SignalingDispatcher:
    tech = (tech or os.environ.get("RADIO_TECH", "lte")).strip().lower()
    if templates_path is None:
        templates_path = os.environ.get("LTE_TEMPLATES", "")
    if sources_path is None:
        sources_path = os.environ.get("MESSAGE_SOURCES_PATH", "")
    if trace_samples_path is None:
        trace_samples_path = os.environ.get("TRACE_SAMPLES_PATH", "")
    key = (tech, templates_path or "", sources_path or "", trace_samples_path or "")
    if key not in _dispatcher_cache:
        cat = get_catalog(tech, templates_path or None)
        sources = MessageSourcesConfig(sources_path or None)
        trace_idx = TraceSampleIndex(trace_samples_path or None)
        ml_idx = MlVocabIndex()
        _dispatcher_cache[key] = SignalingDispatcher(cat, sources, trace_idx, ml_idx)
    return _dispatcher_cache[key]


def clear_dispatcher_cache() -> None:
    _dispatcher_cache.clear()


__all__ = [
    "clear_dispatcher_cache",
    "get_catalog",
    "get_dispatcher",
    "SignalingCatalog",
    "SignalingDispatcher",
    "twin",
]
