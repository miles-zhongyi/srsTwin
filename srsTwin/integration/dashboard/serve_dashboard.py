#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Localhost dashboard server for srsTwin.

Serves the dashboard at http://127.0.0.1:8765/ with live JSON APIs:
  GET  /api/data     — current parsed events + stack status
  GET  /api/refresh  — pull logs from Docker containers, then return data

Usage (from integration/dashboard):
  python serve_dashboard.py
  python serve_dashboard.py --port 8765 --mode hub
  python serve_dashboard.py --no-open
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError, HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION = os.path.normpath(os.path.join(HERE, ".."))

sys.path.insert(0, HERE)
from parse_callflow import build, render_html  # noqa: E402
from parse_4g import build_4g  # noqa: E402
from parse_lw5g import build_lw5g  # noqa: E402
from parse_rrc import build_rrc  # noqa: E402
from parse_signaling import build_signaling  # noqa: E402
from trace_replay import apply_trace_replay  # noqa: E402
from trace_transmit import default_transmit_plan_path, write_transmit_plan  # noqa: E402
from message_catalog import lookup_message_info  # noqa: E402
from trace_catalog import get_catalog  # noqa: E402
from signaling_sources import describe_sources, save_sources  # noqa: E402

_TRACE_DIR_DEFAULT = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "poc_StressTest", "22_decoded", "00")
)

# Written by demo3ue/live_cycler.py (one JSON object per line, append-only) —
# this server only ever reads it. Independent processes; either can be
# restarted without affecting the other.
KPI_HISTORY_PATH = os.path.join(HERE, "logs", "kpi_history.jsonl")
KPI_HISTORY_MAX = 500  # cap what's sent to the browser; the file itself is never trimmed


def read_kpi_history() -> list[dict]:
    if not os.path.isfile(KPI_HISTORY_PATH):
        return []
    samples = []
    with open(KPI_HISTORY_PATH, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return samples[-KPI_HISTORY_MAX:]

# 3-UE demo pairs (integration/demo3ue/) — pair 1 is the original single-UE
# stack and keeps using the flat log_dir (ue4g.log/enb.log) for backward
# compatibility with everything that predates this. Pairs 2/3 are optional:
# if their containers aren't running, build_4g() just sees missing files and
# reports has_live=False for that pair, same as pair 1 does when the 4G
# stack isn't up at all.
# ue_name/enb_name are the actual `container_name:` values from the compose
# files — used directly with `docker exec`/`docker inspect`, neither of
# which knows about compose service names. ue_service/enb_service are the
# compose *service* names — needed for `docker compose start/stop`, which
# (unlike exec/inspect) operates on services, not container names.
PAIRS_4G = [
    {"key": "1", "ue_name": "srstwin_ue4g",  "enb_name": "srstwin_enb",
     "ue_service": "srsue4g",  "enb_service": "srsenb",  "subdir": None},
    {"key": "2", "ue_name": "srstwin_ue4g2", "enb_name": "srstwin_enb2",
     "ue_service": "srsue4g2", "enb_service": "srsenb2", "subdir": "pair2"},
    {"key": "3", "ue_name": "srstwin_ue4g3", "enb_name": "srstwin_enb3",
     "ue_service": "srsue4g3", "enb_service": "srsenb3", "subdir": "pair3"},
]
PAIRS_4G_BY_KEY = {p["key"]: p for p in PAIRS_4G}

# Compose file set covering every 4G pair (base stack + the single-pair 4G
# overlay + the 2-extra-pair overlay) — same three files the 3-UE demo's own
# docs use to start/stop pairs 2/3 by hand.
COMPOSE_4G = ["docker", "compose",
              "-f", os.path.join(INTEGRATION, "docker-compose.yml"),
              "-f", os.path.join(INTEGRATION, "docker-compose.4g.yml"),
              "-f", os.path.join(INTEGRATION, "docker-compose.3ue.yml")]

# srsue's PHY layer logs every subframe — bound how much we ever read so
# parse time stays roughly constant no matter how long the stack has been
# running (confirmed: 2.1-2.3M lines in ue4g.log after ~3h uptime, which
# made /api/data hang outright re-parsing it from scratch every 5s poll).
#
# The bound has to be generous relative to the actual logging rate, not just
# "small enough to parse fast": measured ~10,500 lines/min/UE, so the
# original 100_000 only covered ~9.5 minutes of history. A pair idle longer
# than that (attached but quiet, no fresh attach/release cycle) had its own
# last real attach scroll out of the window entirely — the dashboard would
# show "no events" / a stale outcome for a perfectly healthy container.
# 300_000 covers ~28 minutes and still parses in a few seconds (the
# non-blocking payload cache means a slower rebuild no longer blocks other
# pollers anyway — see payload()).
LOG_TAIL_LINES = 300_000

POC_STRESSTEST_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "poc_StressTest"))
SIM_DASHBOARD_URL = "http://127.0.0.1:9090"

# ---------------------------------------------------------------------------
# Twin manager — the dashboard process is always on, but only one twin's
# *backend containers* run at a time (the principle the sidebar is built
# around). Each twin owns a compose file set + service/container list;
# "start" brings up its baseline, "stop" tears down everything belonging to
# it (including srsTwin pairs 2/3, even though those are normally controlled
# independently via the per-pair power buttons — switching twins means
# nothing from the old one should keep running).
# ---------------------------------------------------------------------------
TWINS = {
    "lte4g_full": {
        "title": "Full-stack 4G LTE Digital Twin",
        "compose": COMPOSE_4G,
        "cwd": INTEGRATION,
        "start_services": ["srsepc", "srsenb", "srsue4g"],
        "stop_services": ["srsepc", "srsenb", "srsue4g",
                           "srsenb2", "srsue4g2", "srsenb3", "srsue4g3"],
        "status_containers": ["srstwin_epc", "srstwin_enb", "srstwin_ue4g"],
    },
    "simulation": {
        "title": "Simulation",
        "compose": ["docker", "compose", "-f", os.path.join(POC_STRESSTEST_DIR, "docker-compose.yml")],
        "cwd": POC_STRESSTEST_DIR,
        "start_services": ["du", "ru", "ru2", "ru3", "ue-sim", "dashboard"],
        "stop_services": ["du", "ru", "ru2", "ru3", "ue-sim", "dashboard"],
        # `ue-sim` has no explicit `container_name:` in poc_StressTest's
        # compose file (unlike du/ru/ru2/ru3/dashboard), so Compose falls
        # back to its default naming: <project>-ue-sim-1. The project name
        # is the directory name lowercased — confirmed live as
        # "poc_stresstest-ue-sim-1". docker inspect needs the real
        # container name; docker compose start/stop/up use the service
        # name above and are unaffected by this.
        "status_containers": ["du", "ru", "ru2", "ru3", "poc_stresstest-ue-sim-1", "dashboard"],
    },
    "lightweight5g": {
        "title": "Lightweight 5G Twin",
        # Uses the base 5G compose file + the lw5g overlay (ru_dummy + test mode, no srsue)
        "compose": ["docker", "compose",
                    "-f", os.path.join(INTEGRATION, "docker-compose.yml"),
                    "-f", os.path.join(INTEGRATION, "docker-compose.lw5g.yml")],
        "cwd": INTEGRATION,
        "start_services": ["5gc", "gnb"],
        "stop_services": ["gnb", "srsue", "5gc"],
        "status_containers": ["srstwin_5gc", "srstwin_gnb"],
        # YAML config path inside the lightweight5g/ directory — used when
        # dynamically adjusting nof_ues without a full image rebuild.
        "gnb_config": os.path.join(INTEGRATION, "lightweight5g", "gnb_testmode.yml"),
        "gnb_container": "srstwin_gnb",
        "gnb_log_path": "/tmp/gnb.log",
    },
}


def twin_status(key: str) -> dict:
    """Real running/stopped state for one twin's containers, straight from
    `docker inspect` — same reasoning as container_status_4g(): independent
    of whatever the twin's own app-level logs say."""
    twin = TWINS[key]
    names = twin["status_containers"]
    running = {}
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}}={{.State.Status}}", *names],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            if "=" in line:
                name, status_val = line.split("=", 1)
                running[name.lstrip("/")] = status_val
    except (subprocess.SubprocessError, OSError):
        pass
    containers = {n: running.get(n, "not created") for n in names}
    up = sum(1 for v in containers.values() if v == "running")
    overall = "running" if up == len(names) else ("partial" if up > 0 else "stopped")
    return {"containers": containers, "overall": overall}


def stop_twin(key: str) -> dict:
    twin = TWINS[key]
    cmd = twin["compose"] + ["stop"] + twin["stop_services"]
    try:
        result = subprocess.run(cmd, cwd=twin["cwd"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout or "").strip()[-400:]}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "docker compose stop timed out after 60s"}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def start_twin(key: str) -> dict:
    """`up -d` (not `--force-recreate`/`--build`) so this is fast on every
    call after the first: Compose only builds/recreates when something
    actually changed, otherwise it just starts existing containers."""
    twin = TWINS[key]
    cmd = twin["compose"] + ["up", "-d"] + twin["start_services"]
    try:
        result = subprocess.run(cmd, cwd=twin["cwd"], capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout or "").strip()[-400:]}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "docker compose up timed out after 300s (first build can be slow)"}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def activate_twin(key: str) -> dict:
    """Stop every other twin, then start this one. Best-effort on the stop
    side — a stale/already-stopped twin failing to "stop" again shouldn't
    block activating the one the user actually asked for."""
    if key not in TWINS:
        return {"ok": False, "error": f"unknown twin {key!r}"}
    stop_errors = []
    for other_key in TWINS:
        if other_key != key:
            r = stop_twin(other_key)
            if not r.get("ok"):
                stop_errors.append(f"{other_key}: {r.get('error')}")
    result = start_twin(key)
    if stop_errors:
        result["stop_warnings"] = stop_errors
    return result


def all_twin_status() -> dict:
    return {key: twin_status(key) for key in TWINS}


def _sim_fetch(path: str, timeout: float = 4) -> tuple[int, bytes]:
    with urlopen(f"{SIM_DASHBOARD_URL}{path}", timeout=timeout) as resp:
        return resp.status, resp.read()


def sim_metrics() -> dict:
    """Proxy poc_StressTest's own dashboard metrics endpoint. Its dashboard
    container does the actual DU/UE-sim polling; this just relays the
    result so the browser only ever talks to one origin (this server)."""
    try:
        code, body = _sim_fetch("/api/metrics")
        return json.loads(body)
    except (URLError, HTTPError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"simulation dashboard unreachable: {exc}"}


def lw5g_data() -> dict:
    """Parse the lightweight-5G gnb log and return events + KPIs.

    Tries to read from the running container via docker exec first; falls back
    to the log file written by pull_logs() if the container isn't running."""
    twin = TWINS["lightweight5g"]
    container = twin["gnb_container"]
    log_local = os.path.join(HERE, "logs", "gnb.log")
    return build_lw5g(log_path=None, container=container)


def lw5g_set_ues(nof_ues: int) -> dict:
    """Hot-patch the nof_ues value in gnb_testmode.yml and restart the gnb
    container so the new UE count takes effect immediately."""
    if not (1 <= nof_ues <= 32):
        return {"ok": False, "error": "nof_ues must be 1-32"}
    twin = TWINS["lightweight5g"]
    cfg_path = twin["gnb_config"]
    try:
        with open(cfg_path, encoding="utf-8") as f:
            content = f.read()
        # Replace the nof_ues line (YAML key under test_mode.test_ue)
        import re as _re
        new_content = _re.sub(
            r"(^\s+nof_ues:\s*)\d+",
            lambda m: m.group(1) + str(nof_ues),
            content, flags=_re.MULTILINE,
        )
        if new_content == content:
            return {"ok": False, "error": "nof_ues line not found in gnb_testmode.yml"}
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as exc:
        return {"ok": False, "error": f"config edit failed: {exc}"}
    # Restart gnb container so the new config (volume-mounted into the container) takes effect
    compose = twin["compose"] + ["restart", "gnb"]
    try:
        result = subprocess.run(compose, cwd=twin["cwd"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout or "").strip()[-400:]}
        return {"ok": True, "nof_ues": nof_ues}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "docker compose restart timed out"}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def sim_set_ues(num_ues: int) -> dict:
    body = json.dumps({"num_ues": num_ues}).encode("utf-8")
    req = Request(f"{SIM_DASHBOARD_URL}/api/ues", data=body, method="POST",
                   headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            return {"ok": True, "status": resp.status, "body": json.loads(resp.read())}
    except HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}: {exc.read()[:200]}"}
    except (URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "error": f"simulation dashboard unreachable: {exc}"}


def _ensure_info(events: list) -> None:
    for e in events:
        if not e.get("info", {}).get("purpose"):
            e["info"] = lookup_message_info(e["label"])


def container_status_4g() -> dict:
    """Real container running/stopped state per pair, straight from `docker
    ps` — not derived from logs, so it isn't subject to srsRAN's own file
    logger buffering (which can lag real activity by a while). This is what
    gives instant feedback when you stop/start a UE, independent of how
    stale the log-derived KPIs are."""
    names = []
    for pair in PAIRS_4G:
        names += [pair["ue_name"], pair["enb_name"]]
    running = {}
    try:
        # `docker inspect` exits non-zero overall if ANY name is missing
        # (container never created), but still prints valid output for the
        # ones that DO exist — don't let check=False/missing-some abort the
        # whole status map, just skip names it couldn't resolve.
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}}={{.State.Status}}", *names],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            if "=" not in line:
                continue
            name, status_val = line.split("=", 1)
            running[name.lstrip("/")] = status_val
    except (subprocess.SubprocessError, OSError):
        pass

    status = {}
    for pair in PAIRS_4G:
        status[pair["key"]] = {
            "ue":  running.get(pair["ue_name"], "not created"),
            "enb": running.get(pair["enb_name"], "not created"),
        }
    return status


def set_pair_power(key: str, action: str) -> dict:
    """Start or stop one 4G pair's eNB+UE *together* via `docker compose
    start/stop` (never `up`/`--force-recreate` — those rebuild the
    container). Always targets both services in one call, never the UE
    alone against an already-running eNB or vice versa: recreating/cycling
    a UE alone while its eNB keeps running causes a RACH retry storm that
    never settles (confirmed empirically during the 3-UE demo work) — `stop`
    doesn't recreate anything, but toggling them together is still the
    safer, simpler invariant to hold everywhere this stack is controlled.
    """
    pair = PAIRS_4G_BY_KEY.get(key)
    if pair is None:
        return {"ok": False, "error": f"unknown pair {key!r}"}
    if action not in ("start", "stop"):
        return {"ok": False, "error": f"unknown action {action!r}"}
    cmd = COMPOSE_4G + [action, pair["ue_service"], pair["enb_service"]]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout or "").strip()[-400:]}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"docker compose {action} timed out after 30s"}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


POLL_DEBOUNCE_S = 2.0  # coalesce concurrent pollers (multiple open browser
# tabs) onto one shared pull/rebuild instead of each triggering its own —
# without this, N tabs polling every 5s meant N concurrent sets of 6
# `docker exec` calls fighting over the same containers, which under load
# made individual `docker exec` calls intermittently stall for seconds and
# compounded into /api/data hanging outright.


class DashboardState:
    def __init__(self, log_dir: str, mode: str):
        self.log_dir = log_dir
        self.mode = mode
        self.lock = threading.Lock()
        self._pull4g_lock = threading.Lock()
        self._pull4g_at = 0.0
        self._payload_cache = None
        self._payload_at = 0.0

    def _build_all(self):
        events, meta = build(self.log_dir)
        _ensure_info(events)
        rrc_twin, rrc_trace, rrc_meta = build_rrc(self.log_dir)
        signaling = build_signaling(rrc_twin, events)
        rrc_twin, events, signaling = apply_trace_replay(rrc_twin, events, signaling)
        write_transmit_plan(signaling, default_transmit_plan_path(self.log_dir))
        trace_dir = _TRACE_DIR_DEFAULT if os.path.isdir(_TRACE_DIR_DEFAULT) else None

        data_4g_multi = {}
        for pair in PAIRS_4G:
            pair_log_dir = os.path.join(self.log_dir, pair["subdir"]) if pair["subdir"] else self.log_dir
            data_4g_multi[pair["key"]] = build_4g(pair_log_dir, trace_dir)

        # trace_recs/per_templates/per_record_status come from the same
        # trace_dir for every pair, so they're byte-identical across all 3 —
        # build_4g() returns them per-pair for convenience (it's also used
        # standalone), but embedding them 3x in data_4g_multi was the single
        # biggest contributor to the dashboard HTML bloating to ~24MB
        # (trace_recs ~3.8MB + aligned's old inline trace copies ~3.9MB,
        # each tripled). Hoist one shared copy out, strip the rest.
        data_4g_shared = {}
        for key in ("trace_recs", "per_templates", "per_record_status"):
            for pair_data in data_4g_multi.values():
                if key in pair_data:
                    data_4g_shared[key] = pair_data[key]
                    break
        for pair_data in data_4g_multi.values():
            for key in ("trace_recs", "per_templates", "per_record_status"):
                pair_data.pop(key, None)

        data_4g = data_4g_multi["1"]
        return events, meta, rrc_twin, rrc_trace, rrc_meta, signaling, data_4g, data_4g_multi, data_4g_shared

    def payload(self) -> dict:
        """Never makes a poller wait on a rebuild it didn't start.

        If the cache is fresh enough, or a rebuild is already in progress
        on another thread, return the last known-good snapshot immediately.
        Only the one thread that finds the cache stale AND acquires the
        lock actually pays the `_build_all()` cost (observed 5-10s+ under
        load with 3 live 4G pairs) — everyone else gets an instant reply.
        With multiple browser tabs polling every 5s, blocking every poller
        on its own fresh build serialized them all and latency climbed
        with each one queued (6s, then 10s, then 15s+ until they timed
        out); this way only the cache ever updates, every reply is instant
        except the rebuild's own.
        """
        now = time.time()
        if self._payload_cache is not None and (now - self._payload_at) < POLL_DEBOUNCE_S:
            return self._payload_cache
        if not self.lock.acquire(blocking=False):
            # someone else is already rebuilding. If we already have a
            # cache (the common case), hand it back without waiting. If
            # not — only possible in the brief startup window before the
            # first build ever completes — wait on the SAME build rather
            # than racing it with an unlocked build of our own (that race
            # is exactly the N-concurrent-builds pileup this is meant to
            # prevent: it's what produced the req2/3/4/7 timeouts when this
            # fallback used to call _build_payload_now() unlocked).
            if self._payload_cache is not None:
                return self._payload_cache
            self.lock.acquire(blocking=True)
        try:
            now = time.time()
            if self._payload_cache is not None and (now - self._payload_at) < POLL_DEBOUNCE_S:
                return self._payload_cache
            result = self._build_payload_now()
            self._payload_cache = result
            self._payload_at = time.time()
            return result
        finally:
            self.lock.release()

    def _build_payload_now(self) -> dict:
        (events, meta, rrc_twin, rrc_trace, rrc_meta, signaling,
         data_4g, data_4g_multi, data_4g_shared) = self._build_all()
        return {
            "events": events,
            "meta": meta,
            "message_count": meta["message_count"],
            "captured": meta["captured"],
            "rrc_twin": rrc_twin,
            "rrc_trace": rrc_trace,
            "rrc_meta": rrc_meta,
            "signaling": signaling,
            "data_4g": data_4g,
            "data_4g_multi": data_4g_multi,
            "data_4g_shared": data_4g_shared,
            "container_status_4g": container_status_4g(),
            "kpi_history": read_kpi_history(),
        }

    def regenerate_html(self) -> None:
        with self.lock:
            (events, meta, rrc_twin, rrc_trace, rrc_meta, signaling,
             data_4g, data_4g_multi, data_4g_shared) = self._build_all()
            html = render_html(events, meta, rrc_twin, rrc_trace, rrc_meta, signaling,
                               data_4g=data_4g, data_4g_multi=data_4g_multi,
                               data_4g_shared=data_4g_shared,
                               container_status_4g=container_status_4g(),
                               kpi_history=read_kpi_history())
            for name in ("index.html", "callflow.html"):
                path = os.path.join(HERE, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)

    def _cp4g(self, container: str, src: str, dst: str, notes: list[str]) -> None:
        """Pull only the TAIL of a 4G log via `docker exec ... tail`, not the
        whole file via `docker compose cp`.

        srsue's PHY layer logs every subframe — after a few hours of uptime
        ue4g.log reaches millions of lines (confirmed: 2.1-2.3M lines after
        ~3h). build_4g() only ever looks at the most recent attach-procedure
        group anyway (split_attach_procedures()[-1]), so re-parsing the
        entire multi-million-line history on every 5s poll was pure waste —
        and at this size it's not just slow, it hangs /api/data outright.
        A bounded tail keeps parse time roughly constant regardless of how
        long the stack has been running.
        """
        dst_path = os.path.join(self.log_dir, dst)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        cmd = ["docker", "exec", container, "tail", "-n", str(LOG_TAIL_LINES), src]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0:
                with open(dst_path, "wb") as f:
                    f.write(result.stdout)
                notes.append(f"pulled (tail {LOG_TAIL_LINES}) {container}:{src}")
        except (subprocess.SubprocessError, OSError):
            pass  # that pair isn't running, or docker exec itself is slow — silently skip

    def pull_logs_4g(self) -> list[str]:
        """Lightweight pull: just the 3 4G pairs (6 small `docker exec tail`s),
        no 5G/hub. Cheap enough to run on every /api/data poll so stopping or
        (re)starting a UE/eNB container shows up within one poll interval
        instead of needing the Refresh button.

        Runs the 6 pulls concurrently, not sequentially: under real load
        (6 containers + this server's own 5s polling) `docker exec` itself
        occasionally stalls for the full subprocess timeout (observed:
        single calls that normally finish in <1s taking 10s+). Sequentially
        that compounds to 60s+ and made /api/data hang outright; in
        parallel, one slow call no longer blocks the other five.

        Also debounced (POLL_DEBOUNCE_S): with multiple browser tabs open,
        each polling every 5s independently, every one of them used to
        trigger its own 6 concurrent `docker exec`s — N tabs meant N*6
        processes hitting the same 6 containers at once, which is exactly
        the load that caused those stalls in the first place. If a pull
        already ran recently (by this thread or another), skip silently;
        the most recent successful pull's files are still on disk.
        """
        if not self._pull4g_lock.acquire(blocking=False):
            return []  # another thread is already pulling right now
        try:
            if time.time() - self._pull4g_at < POLL_DEBOUNCE_S:
                return []  # pulled recently enough, files are still fresh
            os.makedirs(self.log_dir, exist_ok=True)
            notes: list[str] = []
            jobs = []
            for pair in PAIRS_4G:
                sub = pair["subdir"] or ""
                jobs.append((pair["ue_name"],  "/tmp/ue4g.log", os.path.join(sub, "ue4g.log")))
                jobs.append((pair["enb_name"], "/tmp/enb.log",  os.path.join(sub, "enb.log")))
            threads = [threading.Thread(target=self._cp4g, args=(c, src, dst, notes)) for c, src, dst in jobs]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=12)
            self._pull4g_at = time.time()
            return notes
        finally:
            self._pull4g_lock.release()

    def pull_logs(self) -> list[str]:
        """Full pull: 5G + all 4G pairs + hub. Used by the Refresh button
        (heavier — also fine to be deliberate/manual)."""
        os.makedirs(self.log_dir, exist_ok=True)
        notes: list[str] = []
        compose = ["docker", "compose", "-f", os.path.join(INTEGRATION, "docker-compose.yml")]
        if self.mode == "hub":
            compose += ["-f", os.path.join(INTEGRATION, "docker-compose.hub.yml")]

        def cp(service: str, src: str, dst: str) -> None:
            dst_path = os.path.join(self.log_dir, dst)
            cmd = compose + ["cp", f"{service}:{src}", dst_path]
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                notes.append(f"pulled {service}:{src}")
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                notes.append(f"{service} log unavailable ({exc})")

        cp("gnb", "/tmp/gnb.log", "gnb.log")
        cp("srsue", "/tmp/ue.log", "ue.log")

        notes += self.pull_logs_4g()
        if self.mode == "hub":
            hub_dst = os.path.join(self.log_dir, "hub.log")
            try:
                subprocess.run(
                    compose + ["cp", "hub:/tmp/stdout", hub_dst],
                    check=True, capture_output=True, text=True,
                )
                notes.append("pulled hub:/tmp/stdout")
            except subprocess.CalledProcessError:
                try:
                    out = subprocess.run(
                        compose + ["logs", "hub"],
                        check=True, capture_output=True, text=True,
                    )
                    with open(hub_dst, "w", encoding="utf-8") as f:
                        f.write(out.stdout)
                    notes.append("captured hub logs via docker compose logs")
                except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                    notes.append(f"hub log unavailable ({exc})")
        else:
            open(os.path.join(self.log_dir, "hub.log"), "w", encoding="utf-8").close()
        return notes


def make_handler(state: DashboardState):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=HERE, **kwargs)

        def log_message(self, fmt, *args):
            if args and isinstance(args[0], str) and args[0].startswith("GET /api"):
                sys.stderr.write(f"[dashboard] {args[0]}\n")
            elif args and isinstance(args[0], str) and "200" in str(args):
                pass  # quiet static assets
            else:
                super().log_message(fmt, *args)

        def end_headers(self):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            super().end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html", "/callflow.html"):
                self.path = "/index.html"
                return SimpleHTTPRequestHandler.do_GET(self)
            if parsed.path == "/api/data":
                # Light 4G-only pull on every poll (the browser hits this every
                # 5s) so stopping/starting a UE+eNB pair shows up live without
                # needing the heavier Refresh button.
                state.pull_logs_4g()
                return self._json(state.payload())
            if parsed.path == "/api/refresh":
                return self._json(self._refresh())
            if parsed.path == "/api/health":
                return self._json({"ok": True, "mode": state.mode})
            if parsed.path == "/api/trace-sample":
                return self._trace_sample(parsed)
            if parsed.path == "/api/message-sources":
                return self._json(describe_sources())
            if parsed.path == "/api/twins/status":
                return self._json(all_twin_status())
            if parsed.path == "/api/sim/metrics":
                return self._json(sim_metrics())
            if parsed.path == "/api/lw5g/data":
                return self._json(lw5g_data())
            return SimpleHTTPRequestHandler.do_GET(self)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/message-sources":
                return self._save_message_sources()
            pair_power_m = re.match(r"^/api/4g/pair/([^/]+)/power$", parsed.path)
            if pair_power_m:
                return self._set_pair_power(pair_power_m.group(1))
            if parsed.path == "/api/twins/activate":
                return self._twin_action(activate_twin)
            if parsed.path == "/api/twins/stop":
                return self._twin_action(stop_twin)
            if parsed.path == "/api/sim/ues":
                return self._sim_set_ues()
            if parsed.path == "/api/lw5g/ues":
                return self._lw5g_set_ues()
            self.send_error(405)
            return None

        def _twin_action(self, fn):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            key = body.get("twin") if isinstance(body, dict) else None
            if key not in TWINS:
                return self._json({"ok": False, "error": f"unknown twin {key!r}"}, 400)
            result = fn(key)
            result["twins"] = all_twin_status()
            return self._json(result, 200 if result.get("ok") else 400)

        def _sim_set_ues(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            num_ues = body.get("num_ues") if isinstance(body, dict) else None
            if not isinstance(num_ues, int) or num_ues < 0:
                return self._json({"ok": False, "error": "expected {num_ues: <non-negative int>}"}, 400)
            result = sim_set_ues(num_ues)
            return self._json(result, 200 if result.get("ok") else 502)

        def _lw5g_set_ues(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            nof_ues = body.get("nof_ues") if isinstance(body, dict) else None
            if not isinstance(nof_ues, int) or nof_ues < 1:
                return self._json({"ok": False, "error": "expected {nof_ues: <1-32>}"}, 400)
            result = lw5g_set_ues(nof_ues)
            return self._json(result, 200 if result.get("ok") else 400)

        def _set_pair_power(self, key: str):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            action = body.get("action") if isinstance(body, dict) else None
            result = set_pair_power(key, action)
            result["container_status_4g"] = container_status_4g()
            return self._json(result, 200 if result.get("ok") else 400)

        def _save_message_sources(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                return self._json({"error": str(exc)}, 400)
            sources = body.get("sources") if isinstance(body, dict) else None
            if not isinstance(sources, dict):
                return self._json({"error": "expected {sources: {...}}"}, 400)
            return self._json(save_sources(sources))

        def _trace_sample(self, parsed):
            cat, err = get_catalog()
            if cat is None:
                return self._json({"error": err}, 503)
            qs = parse_qs(parsed.query)
            key = (qs.get("record_id") or qs.get("key") or qs.get("name") or [""])[0]
            if not key:
                return self._json({"error": "missing record_id/key/name"}, 400)
            sample = cat.get_sample(key)
            if sample is None:
                return self._json({"error": f"no indexed 22_decoded sample for {key}"}, 404)
            return self._json(sample)

        def _refresh(self) -> dict:
            notes = state.pull_logs()
            state.regenerate_html()
            body = state.payload()
            body["notes"] = notes
            return body

        def _json(self, obj: dict, code: int = 200):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="srsTwin localhost dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mode", choices=("direct", "hub"), default="direct",
                    help="compose overlay for docker log pull")
    ap.add_argument("--log-dir", default=os.path.join(HERE, "logs"))
    ap.add_argument("--no-open", action="store_true", help="do not open browser")
    ap.add_argument("--pull", action="store_true",
                    help="pull container logs once before starting")
    args = ap.parse_args()

    state = DashboardState(args.log_dir, args.mode)
    if args.pull:
        notes = state.pull_logs()
        for n in notes:
            print(f"  {n}")
    state.regenerate_html()

    url = f"http://{args.host}:{args.port}/"
    handler = make_handler(state)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)

    print(f"srsTwin dashboard  {url}")
    print(f"  mode={args.mode}  logs={args.log_dir}")
    print("  APIs: /api/data  /api/refresh  (auto-poll every 5s in browser)")

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.server_close()


if __name__ == "__main__":
    main()
