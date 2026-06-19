"""Dual-source signaling dispatcher tests."""
import json

import pytest

from common.signaling import get_catalog, get_dispatcher
from common.signaling import procedures as proc
from common.signaling.message_sources import MessageSourcesConfig
from common.signaling.templates import tok

ROOT = "data/lte_templates.json"
SAMPLES = "data/trace_message_samples.json"
SOURCES = "data/message_sources.json"


def _unfilled(obj) -> bool:
    return "<<" in json.dumps(obj)


@pytest.fixture
def dispatcher(tmp_path):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"version": 1, "sources": {"*": "auto"}}), encoding="utf-8")
    return get_dispatcher(
        "lte",
        templates_path=ROOT,
        sources_path=str(sources),
        trace_samples_path=SAMPLES,
    )


def test_trace_mode_uses_decoded_body_from_samples(dispatcher):
    sources = MessageSourcesConfig(dispatcher._sources.path)
    sources.set_mode("RRC_RRC_CONNECTION_REQUEST", "trace")
    sources.save()
    disp = get_dispatcher("lte", templates_path=ROOT,
                          sources_path=str(sources.path),
                          trace_samples_path=SAMPLES)
    msg = disp.build(proc.RRC_CONNECTION_REQUEST, ue_id="ue-d-1", cell="cell-1", step="rrc_setup")
    assert not _unfilled(msg)
    assert msg["message_name"] == "RRC_RRC_CONNECTION_REQUEST"
    assert "decoded" in msg
    assert msg["_twin"]["signaling_source"] == "trace"
    assert msg["_twin"]["trace_sample"] is True


def test_ml_mode_tags_source_when_vocab_has_type(dispatcher, tmp_path):
    sources = tmp_path / "ml_sources.json"
    # Force ML for an S1 type that appears in digital-twin vocab
    sources.write_text(json.dumps({
        "version": 1,
        "sources": {"S1_INITIAL_UE_MESSAGE": "ml"},
    }), encoding="utf-8")
    disp = get_dispatcher("lte", templates_path=ROOT,
                          sources_path=str(sources),
                          trace_samples_path=SAMPLES)
    if not disp._ml.is_available("S1_INITIAL_UE_MESSAGE"):
        pytest.skip("ML vocab not present (digital-twin/vocab.json missing)")
    msg = disp.build(proc.RRC_CONNECTION_REQUEST, ue_id="ue-ml", cell="cell-1")
    # RRC request stays trace unless configured — check S1 via downlink step
    msg2 = disp.build(
        proc.S1_INITIAL_CONTEXT_SETUP_REQUEST,
        ue_id="ue-ml", cell="cell-1",
        source="ml",
    )
    assert msg2["_twin"]["signaling_source"] in ("ml", "trace_fallback")
    assert "decoded" in msg2
    assert not _unfilled(msg2)


def test_ml_unavailable_falls_back_to_trace(tmp_path):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({
        "version": 1,
        "sources": {"RRC_UE_CAPABILITY_ENQUIRY": "ml"},
    }), encoding="utf-8")
    disp = get_dispatcher("lte", templates_path=ROOT,
                          sources_path=str(sources),
                          trace_samples_path=SAMPLES)
    assert not disp._ml.is_available("RRC_UE_CAPABILITY_ENQUIRY")
    msg = disp.build(proc.RRC_UE_CAPABILITY_ENQUIRY, ue_id="ue-fb", cell="cell-1")
    assert msg["_twin"]["signaling_source"] == "trace"
    assert not _unfilled(msg)


def test_auto_resolves_ml_for_s1_when_vocab_loaded(dispatcher):
    name = "S1_UE_CONTEXT_RELEASE_REQUEST"
    if not dispatcher._ml.is_available(name):
        pytest.skip("ML vocab missing")
    mode = dispatcher.resolve_mode(name)
    assert mode == "ml"


def test_attach_flow_classify_roundtrip_with_dispatcher(dispatcher):
    for step in proc.ATTACH_FLOW:
        msg = dispatcher.build(
            step.uplink, ue_id="ue-att", cell="cell-1", step=step.name,
            demand_mbps=0.03 if step.action == proc.ACT_ADMIT else None,
        )
        assert not _unfilled(msg), step.uplink
        got = dispatcher.classify(msg)
        assert got is not None and got.name == step.name


def test_signaling_flow_tests_still_pass_pattern():
    """Mirror test_signaling_flow: dispatcher must not break DU attach."""
    from du.du_server import DU

    disp = get_dispatcher("lte", templates_path=ROOT, trace_samples_path=SAMPLES,
                          sources_path=SOURCES)
    du = DU()
    cell = "cell-1"
    for step in disp.attach_flow():
        fields = {"demand_mbps": 0.03} if step.action == proc.ACT_ADMIT else {}
        msg = disp.build(step.uplink, ue_id="ue-a", cell=cell, step=step.name,
                         position={"x": 10, "y": 0}, tx_power_dbm=23.0, **fields)
        tw = msg["_twin"]
        tw["cell"] = cell
        tw["rf"] = {"sinr_dl_db": 20.0, "sinr_ul_db": 20.0}
        du.dispatch(msg)
    assert du.cells[cell].admitted_total == 1


def test_describe_sources_lists_entries(dispatcher):
    desc = dispatcher.describe_sources()
    assert desc["entries"]
    assert any(e["message_name"] == "RRC_RRC_CONNECTION_REQUEST" for e in desc["entries"])


def test_message_sources_config_roundtrip(tmp_path):
    path = tmp_path / "ms.json"
    cfg = MessageSourcesConfig(path)
    cfg.set_mode("S1_INITIAL_UE_MESSAGE", "trace")
    cfg.save()
    cfg2 = MessageSourcesConfig(path)
    assert cfg2.get_mode("S1_INITIAL_UE_MESSAGE") == "trace"
    assert cfg2.get_mode("UNKNOWN") == "auto"
