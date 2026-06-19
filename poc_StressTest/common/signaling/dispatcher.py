"""
Dual-source signaling dispatcher: trace replay vs ML (hybrid template backfill).

Both paths emit full ``22_decoded``-shaped records via ``fill(template, context)``.
"""
from __future__ import annotations

from .catalog import SignalingCatalog, twin
from .message_sources import MessageSourcesConfig
from .ml_source import MlVocabIndex
from .templates import fill
from .trace_index import TraceSampleIndex


class SignalingDispatcher:
    """Wraps a catalog; ``build()`` honours per-type source configuration."""

    def __init__(
        self,
        catalog: SignalingCatalog,
        sources: MessageSourcesConfig | None = None,
        trace_index: TraceSampleIndex | None = None,
        ml_index: MlVocabIndex | None = None,
    ):
        self._catalog = catalog
        self._sources = sources or MessageSourcesConfig()
        self._trace = trace_index or TraceSampleIndex()
        self._ml = ml_index or MlVocabIndex()

    @property
    def catalog(self) -> SignalingCatalog:
        return self._catalog

    def __getattr__(self, name: str):
        return getattr(self._catalog, name)

    def resolve_mode(self, message_name: str, source: str | None = None) -> str:
        mode = (source or self._sources.get_mode(message_name)).strip().lower()
        if mode == "auto":
            return "ml" if self._ml.is_available(message_name) else "trace"
        if mode == "ml" and not self._ml.is_available(message_name):
            return "trace"
        return mode

    def _resolve_template(self, real_name: str, record_id: int | None) -> dict | None:
        tpl = self._trace.get_template(real_name, record_id)
        if tpl is not None:
            return tpl
        return self._catalog.templates.get(real_name)

    def build(
        self,
        logical: str,
        *,
        ue_id: str,
        cell: str,
        txn=None,
        step=None,
        source: str | None = None,
        **twin_fields,
    ) -> dict:
        real_name = self._catalog.real_name(logical)
        mode = self.resolve_mode(real_name, source)
        record_id = self._catalog.record_id_for(real_name)
        template = self._resolve_template(real_name, record_id)

        if template is None:
            msg = self._catalog.build(
                logical, ue_id=ue_id, cell=cell, txn=txn, step=step, **twin_fields,
            )
            tw = msg.setdefault("_twin", {})
            tw["signaling_source"] = "catalog_fallback"
            tw["signaling_mode_requested"] = mode
            return msg

        ctx = self._catalog._token_context(ue_id, cell, real_name)
        msg = fill(template, ctx)
        msg["message_name"] = real_name
        msg["record_id"] = ctx["record_id"]
        iface, protocol = self._catalog._iface_for(real_name)
        msg["interface"] = iface
        msg["protocol"] = protocol
        msg.setdefault("decoding_status", "success")
        if txn is not None:
            msg["txn"] = txn

        effective = mode
        if mode == "ml" and not self._ml.is_available(real_name):
            effective = "trace_fallback"

        sidecar = {
            "logical": logical,
            "ue_id": ue_id,
            "cell": cell,
            "signaling_source": effective,
            "signaling_mode_requested": mode,
            "trace_sample": self._trace.has_sample(real_name, record_id),
            "ml_available": self._ml.is_available(real_name),
        }
        if step is not None:
            sidecar["step"] = step
        sidecar.update({k: v for k, v in twin_fields.items() if v is not None})
        msg["_twin"] = sidecar
        return msg

    def describe_sources(self) -> dict:
        entries = self._sources.list_for_catalog(self._catalog)
        for e in entries:
            e["trace_sample"] = self._trace.has_sample(e["message_name"], e.get("record_id"))
            e["ml_available"] = self._ml.is_available(e["message_name"])
            e["resolved"] = self.resolve_mode(e["message_name"])
        return {
            "path": str(self._sources.path),
            "trace_index": str(self._trace.path),
            "ml_vocab": str(self._ml.path),
            "trace_index_loaded": self._trace.loaded,
            "ml_vocab_loaded": self._ml.loaded,
            "entries": entries,
        }
