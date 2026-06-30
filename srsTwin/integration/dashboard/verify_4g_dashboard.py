#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Phase 3c verification: 4G dashboard end-to-end checks.

Tests:
  1. parse_4g.build_4g() loads trace records and PER templates from 22_decoded.
  2. Trace records have expected fields (pdu_type, decoded_choice, semantic_fields).
  3. PER templates have valid base64-or-hex byte payloads.
  4. Dashboard HTML contains the 4G panels, JS rendering functions, and DATA4G payload.
  5. DATA4G in HTML contains trace_recs and per_templates.
  6. 22_decoded side-by-side: at least one trace record has a PER template match.
  7. Signal flow ladder: buildLteLadderSvg function present; lane constants correct.
  8. parse_4g.py parser: known 4G message names are in PRETTY_4G.
  9. serve_dashboard.py imports parse_4g.build_4g.
 10. Docker compose 4G file wires RRC_TRACE_DIR environment variable.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION = os.path.normpath(os.path.join(HERE, ".."))
REPO_ROOT   = os.path.normpath(os.path.join(INTEGRATION, "..", ".."))
TRACE_DIR   = os.path.join(REPO_ROOT, "poc_StressTest", "22_decoded", "00")

sys.path.insert(0, HERE)

ok = fail = skipped = 0

def check(name: str, passed: bool, hint: str = "") -> None:
    global ok, fail
    if passed:
        print(f"OK   {name}")
        ok += 1
    else:
        print(f"FAIL {name}" + (f"  ({hint})" if hint else ""))
        fail += 1


def skip(name: str, hint: str = "") -> None:
    """Use for checks that can't run because a precondition (e.g. live log
    fixtures) wasn't met — not a code regression, so it shouldn't count
    toward FAIL or affect the exit code."""
    global skipped
    print(f"SKIP {name}" + (f"  ({hint})" if hint else ""))
    skipped += 1


# ---------------------------------------------------------------------------
# 1. build_4g imports and runs without error
# ---------------------------------------------------------------------------
try:
    from parse_4g import build_4g, PRETTY_4G  # type: ignore
    check("parse_4g imports OK", True)
except Exception as exc:
    check("parse_4g imports OK", False, str(exc))
    sys.exit(1)

try:
    data4g = build_4g(os.path.join(HERE, "logs"), TRACE_DIR if os.path.isdir(TRACE_DIR) else None)
    check("build_4g() runs without error", True)
except Exception as exc:
    check("build_4g() runs without error", False, str(exc))
    data4g = {"events": [], "trace_recs": [], "per_templates": {}}

# ---------------------------------------------------------------------------
# 2. Trace records loaded
# ---------------------------------------------------------------------------
trace_recs = data4g.get("trace_recs", [])
check("trace records loaded (> 0)", len(trace_recs) > 0,
      f"got {len(trace_recs)} — check TRACE_DIR={TRACE_DIR}")

if trace_recs:
    r0 = trace_recs[0]
    check("trace record has message_name",  "message_name" in r0)
    # decoded_choice lives inside decoding_metadata
    dmeta0 = r0.get("decoding_metadata") or {}
    check("trace record has decoded_message_choice",
          bool(dmeta0.get("decoded_message_choice") or r0.get("message_name")))
    check("trace record has direction",     "direction" in r0)
    check("trace record has interface",     "interface" in r0)

# ---------------------------------------------------------------------------
# 3. PER templates
# ---------------------------------------------------------------------------
per_tmpl = data4g.get("per_templates", {})
check("PER templates loaded (> 0)", len(per_tmpl) > 0,
      f"got {len(per_tmpl)} — run encode_templates.py first")

for choice, entry in list(per_tmpl.items())[:3]:
    # templates use 'per_encoded' (hex string) from encode_templates.py
    has_bytes = bool(entry.get("per_encoded") or entry.get("per_hex")
                     or entry.get("per_b64") or entry.get("per_bytes"))
    check(f"PER template '{choice}' has byte payload", has_bytes)

# ---------------------------------------------------------------------------
# 4. HTML structure checks
# ---------------------------------------------------------------------------
html_path = os.path.join(HERE, "index.html")
if not os.path.isfile(html_path):
    check("index.html exists", False, "run parse_callflow.py first")
    sys.exit(1)

html = open(html_path, encoding="utf-8").read()
check("index.html exists", True)

html_checks = [
    ('panel-lte4g',          'id="panel-lte4g"'),
    ('panel-lte4gtrace',     'id="panel-lte4gtrace"'),
    ('4G LTE tab button',    'data-tab="lte4g"'),
    ('Overview tab button',  'data-tab="overview"'),
    ('sidebar element',      'id="sidebar"'),
    ('pair power button JS', 'function togglePairPower'),
    ('4G stack topology diagram', 'id="lte4g-topo"'),
    ('favicon link',         'rel="icon"'),
    ('DATA4G_MULTI variable', 'let DATA4G_MULTI ='),
    ('lte-ev-list element',  'id="lte-ev-list"'),
    ('ltetrace-tbody',       'id="ltetrace-tbody"'),
    ('lte-inject-bar',       'id="lte-inject-bar"'),
    ('per-template-json',    'id="per-template-json"'),
    ('buildLteLadderSvg fn', 'function buildLteLadderSvg'),
    ('renderLtetraceTable',  'function renderLtetraceTable'),
    ('4G trace count span',  'id="ltetrace-count"'),
    ('LANES constant',       "const LANES ="),
    ('LTE_LANE_IDX constant', "const LTE_LANE_IDX ="),
    ('initLte4g called',     'initLte4g()'),
    ('status badges CSS',    'status-badge'),
    ('statusBadge JS',       'function statusBadge'),
    ('templateStatus JS',    'function templateStatusForTrace'),
    ('Simulation sidebar item', 'data-twin="simulation" data-title="Simulation"'),
    ('Simulation tab button', 'data-tab="simulation" data-twin="simulation"'),
    ('panel-simulation',     'id="panel-simulation"'),
    ('single shared twin-ctrl-bar in header', 'id="twin-ctrl-bar"'),
    ('setTwinBackend JS',    'function setTwinBackend'),
    ('renderSimulation JS',  'function renderSimulation'),
    ('showTwinView JS',      'function showTwinView'),
    ('Simulation tab green accent', 'tab-sim'),
    ('Simulation mobility map canvas', 'id="sim-geo-canvas"'),
    ('drawSimGeoMap JS',     'function drawSimGeoMap'),
    ('Simulation sidebar green accent', '.sidebar-item[data-twin="simulation"].on'),
    ('Simulation analytics: PRB donut', 'id="sim-prb-donut"'),
    ('Simulation analytics: outcome bar', 'id="sim-outcome-bar"'),
    ('Lightweight 5G sidebar item', 'data-twin="lightweight5g" data-title="Lightweight 5G Twin"'),
    ('Lightweight 5G Call Flow tab', 'data-tab="lw5g-callflow"'),
    ('Lightweight 5G Overview tab', 'data-tab="lw5g-overview"'),
    ('panel-lw5g-callflow',  'id="panel-lw5g-callflow"'),
    ('panel-lw5g-overview',  'id="panel-lw5g-overview"'),
    ('Lightweight 5G blue accent', 'tab-lw5g'),
    ('Lightweight 5G sidebar blue accent', '.sidebar-item[data-twin="lightweight5g"].on'),
    ('activation pattern selector JS', 'LW5G_PATTERNS'),
    ('NOT_BUILT_TWINS JS',   'NOT_BUILT_TWINS'),
]
for name, needle in html_checks:
    check(f"HTML: {name}", needle in html)

# ---------------------------------------------------------------------------
# 5. DATA4G payload in HTML contains trace_recs and per_templates
#
# The dashboard embeds one payload PER 4G pair (DATA4G_MULTI = {"1": {...},
# "2": {...}, "3": {...}}) since the multi-pair refactor — there's no longer
# a single `const DATA4G = {...}` literal. Pull pair "1" (falling back to
# whichever key is first) the same way the page's own JS does:
# `DATA4G_MULTI[ltePairSel] || DATA4G_MULTI['1'] || DATA4G`.
#
# trace_recs/per_templates/per_record_status live in a separate
# DATA4G_SHARED object, not inside each pair — they're identical across
# every pair (same trace_dir), so embedding them per-pair used to triple
# ~7.7MB of JSON (the dashboard HTML had grown to ~24MB). `aligned[]`
# entries reference a trace record by `trace_idx` into
# DATA4G_SHARED.trace_recs rather than embedding a full copy inline.
# ---------------------------------------------------------------------------
data4g_m = re.search(r'let DATA4G_MULTI = (\{.+?\});', html, re.DOTALL)
shared_m = re.search(r'let DATA4G_SHARED = (\{.+?\});', html, re.DOTALL)
if data4g_m and shared_m:
    try:
        multi = json.loads(data4g_m.group(1))
        shared = json.loads(shared_m.group(1))
        payload = multi.get("1") or next(iter(multi.values()), {})
        check("DATA4G_SHARED.trace_recs present",    "trace_recs" in shared)
        check("DATA4G_SHARED.per_templates present", "per_templates" in shared)
        check("DATA4G.aligned in payload",       "aligned" in payload)
        check("DATA4G.inject_meta in payload",   "inject_meta" in payload)
        check("DATA4G_SHARED.per_record_status present", "per_record_status" in shared)
        check("trace_recs/per_templates NOT duplicated per-pair",
              "trace_recs" not in payload and "per_templates" not in payload)
        n_tr   = len(shared.get("trace_recs",   []))
        n_tmpl = len(shared.get("per_templates", {}))
        check(f"DATA4G_SHARED has {n_tr} trace_recs (> 0)",    n_tr > 0)
        check(f"DATA4G_SHARED has {n_tmpl} PER templates (> 0)", n_tmpl > 0)
        statuses = shared.get("per_record_status", {})
        check("per-record status has rrcConnectionRequest exact",
              statuses.get("rrcConnectionRequest", {}).get("status") == "exact")
        check("per-record status has S1AP reconstructed",
              statuses.get("S1_DOWNLINK_NAS_TRANSPORT", {}).get("status") == "reconstructed")
        tr_statuses = {r.get("_template_status") for r in shared.get("trace_recs", [])}
        check("trace records include template status annotations", bool(tr_statuses - {None}))
        aligned_with_idx = [a for a in payload.get("aligned", []) if a.get("trace_idx") is not None]
        check("aligned[] uses trace_idx, not inline trace copies",
              all("trace" not in a for a in payload.get("aligned", [])))
        check("at least one aligned entry resolves to a real trace_idx",
              len(aligned_with_idx) > 0 or not payload.get("has_live"))
    except json.JSONDecodeError as exc:
        check("DATA4G JSON parses OK", False, str(exc)[:80])
else:
    check("DATA4G JSON found in HTML", False)

# ---------------------------------------------------------------------------
# 6. Side-by-side match: at least one trace rec has a PER template
# ---------------------------------------------------------------------------
if trace_recs and per_tmpl:
    def _rec_choice(r: dict) -> str:
        dmeta = r.get("decoding_metadata") or {}
        return (dmeta.get("decoded_message_choice")
                or r.get("message_name") or "")

    matched = [r for r in trace_recs if _rec_choice(r) in per_tmpl]
    sample_choices = [_rec_choice(r) for r in trace_recs[:8]]
    check("at least one trace record has a PER template match", len(matched) > 0,
          f"sample choices: {sample_choices}, templates: {list(per_tmpl.keys())}")

# ---------------------------------------------------------------------------
# 7. PRETTY_4G has required LTE message names
# ---------------------------------------------------------------------------
required_msgs = [
    "rrcConnectionRequest", "rrcConnectionSetup", "rrcConnectionReject",
    "rrcConnectionSetupComplete", "securityModeCommand",
    "initialUEMessage", "s1Setup", "systemInformation",
]
for msg in required_msgs:
    check(f"PRETTY_4G has '{msg}'", msg in PRETTY_4G)

# ---------------------------------------------------------------------------
# 7b. Attach flow ordering (3GPP phase order within procedure)
# ---------------------------------------------------------------------------
try:
    from parse_4g import order_attach_flow, flow_rank_and_phase  # type: ignore

    live_events = data4g.get("events", [])
    if len(live_events) >= 4:
        ranks = [e.get("flow_rank", 9999) for e in live_events]
        check("4G events have flow_rank", all(r < 9000 for r in ranks[:20]))
        # First attach cycle: Msg3 must not precede PRACH
        first_cycle = live_events[:12]
        labels = [e.get("label", "").lower() for e in first_cycle]
        if any("msg3" in l for l in labels) and any("prach" in l for l in labels):
            i_prach = next(i for i, l in enumerate(labels) if "prach" in l)
            i_msg3 = next(i for i, l in enumerate(labels) if "msg3" in l)
            check("Msg3 after PRACH in first attach cycle", i_msg3 > i_prach,
                  f"prach@{i_prach} msg3@{i_msg3}")
        if any("random access response" in l for l in labels) and any("reject" in l for l in labels):
            i_rar = next(i for i, l in enumerate(labels) if "random access response" in l)
            i_rej = next(i for i, l in enumerate(labels) if "reject" in l)
            check("RAR before Reject in first attach cycle", i_rar < i_rej,
                  f"rar@{i_rar} reject@{i_rej}")
    else:
        skip("4G live events for flow-order test", "need ue4g.log + enb.log with a parsed attach cycle (>=4 events)")
except Exception as exc:
    check("attach flow ordering checks", False, str(exc))

# ---------------------------------------------------------------------------
# 8. serve_dashboard.py imports build_4g
# ---------------------------------------------------------------------------
serve_path = os.path.join(HERE, "serve_dashboard.py")
serve_src = open(serve_path, encoding="utf-8").read()
check("serve_dashboard imports build_4g",  "from parse_4g import build_4g" in serve_src)
check("serve_dashboard calls build_4g",    "build_4g(" in serve_src)
check("serve_dashboard pulls ue4g.log",    "ue4g.log" in serve_src)
check("serve_dashboard pulls enb.log",     "enb.log" in serve_src)

# ---------------------------------------------------------------------------
# 9. docker-compose.4g.yml wires RRC_TRACE_DIR
# ---------------------------------------------------------------------------
dc4g = os.path.join(INTEGRATION, "docker-compose.4g.yml")
if os.path.isfile(dc4g):
    dc_src = open(dc4g, encoding="utf-8").read()
    check("docker-compose.4g.yml has RRC_TRACE_DIR", "RRC_TRACE_DIR" in dc_src)
    check("docker-compose.4g.yml mounts 22_decoded",  "22_decoded" in dc_src)
else:
    check("docker-compose.4g.yml exists", False)

# ---------------------------------------------------------------------------
# 10. Lightweight 5G twin: parser, backend, HTML elements, JS renderers
# ---------------------------------------------------------------------------
try:
    from parse_lw5g import build_lw5g  # type: ignore
    check("parse_lw5g imports OK", True)
except Exception as exc:
    check("parse_lw5g imports OK", False, str(exc))

try:
    lw5g_log = os.path.join(HERE, "logs", "gnb.log")
    lw5g_data = build_lw5g(log_path=lw5g_log if os.path.isfile(lw5g_log) else None)
    check("build_lw5g() runs without error", True)
    check("build_lw5g() returns events list",  "events"  in lw5g_data)
    check("build_lw5g() returns ue_kpis dict", "ue_kpis" in lw5g_data)
    check("build_lw5g() returns summary dict", "summary" in lw5g_data)
except Exception as exc:
    check("build_lw5g() runs without error", False, str(exc))

check("serve_dashboard imports build_lw5g", "from parse_lw5g import build_lw5g" in serve_src)
check("serve_dashboard has /api/lw5g/data",  "/api/lw5g/data" in serve_src)
check("serve_dashboard has /api/lw5g/ues",   "/api/lw5g/ues"  in serve_src)
check("serve_dashboard has lightweight5g twin registry", '"lightweight5g"' in serve_src)

dc_lw5g = os.path.join(INTEGRATION, "docker-compose.lw5g.yml")
check("docker-compose.lw5g.yml exists", os.path.isfile(dc_lw5g))
if os.path.isfile(dc_lw5g):
    dc_lw5g_src = open(dc_lw5g, encoding="utf-8").read()
    check("docker-compose.lw5g.yml uses gnb_testmode.yml", "gnb_testmode.yml" in dc_lw5g_src)
    check("docker-compose.lw5g.yml disables srsue",        "disabled" in dc_lw5g_src)

gnb_cfg = os.path.join(INTEGRATION, "lightweight5g", "gnb_testmode.yml")
check("gnb_testmode.yml exists", os.path.isfile(gnb_cfg))
if os.path.isfile(gnb_cfg):
    gnb_src = open(gnb_cfg, encoding="utf-8").read()
    check("gnb_testmode.yml has ru_dummy",   "ru_dummy:" in gnb_src)
    check("gnb_testmode.yml has test_mode",  "test_mode:" in gnb_src)
    check("gnb_testmode.yml has nof_ues",    "nof_ues:" in gnb_src)

html_src = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
check("index.html has lw5g-ladder-svg",       "lw5g-ladder-svg" in html_src)
check("index.html has lw5g-kpi-cards",        "lw5g-kpi-cards"  in html_src)
check("index.html has lw5g-lat-canvas",       "lw5g-lat-canvas" in html_src)
check("index.html has lw5g-timeline-canvas",  "lw5g-timeline-canvas" in html_src)
check("index.html has lw5g-topo-svg",         "lw5g-topo-svg"   in html_src)
check("index.html has pollLw5g",              "pollLw5g" in html_src)
check("index.html has renderLw5gCallFlow",    "renderLw5gCallFlow" in html_src)
check("index.html has renderLw5gOverview",    "renderLw5gOverview" in html_src)
check("index.html UE slider max=10",          'max="10"' in html_src or "max=\\'10\\'" in html_src
      or re.search(r"max=['\"]10['\"]", html_src) is not None)
check("index.html POST /api/lw5g/ues",        "/api/lw5g/ues" in html_src)
check("lightweight5g NOT in NOT_BUILT_TWINS", "NOT_BUILT_TWINS = new Set([])" in html_src
      or re.search(r"NOT_BUILT_TWINS\s*=\s*new Set\(\[\s*\]\)", html_src) is not None)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*54}")
print(f"  {ok}/{ok+fail} checks passed" + (f", {skipped} skipped" if skipped else ""))
if fail:
    print(f"  {fail} FAILED — see above")
    sys.exit(1)
else:
    print("  ALL PASS")
