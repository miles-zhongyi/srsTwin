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
import subprocess
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION = os.path.normpath(os.path.join(HERE, ".."))

sys.path.insert(0, HERE)
from parse_callflow import build, render_html  # noqa: E402
from parse_4g import build_4g  # noqa: E402
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


def _ensure_info(events: list) -> None:
    for e in events:
        if not e.get("info", {}).get("purpose"):
            e["info"] = lookup_message_info(e["label"])


class DashboardState:
    def __init__(self, log_dir: str, mode: str):
        self.log_dir = log_dir
        self.mode = mode
        self.lock = threading.Lock()

    def _build_all(self):
        events, meta = build(self.log_dir)
        _ensure_info(events)
        rrc_twin, rrc_trace, rrc_meta = build_rrc(self.log_dir)
        signaling = build_signaling(rrc_twin, events)
        rrc_twin, events, signaling = apply_trace_replay(rrc_twin, events, signaling)
        write_transmit_plan(signaling, default_transmit_plan_path(self.log_dir))
        trace_dir = _TRACE_DIR_DEFAULT if os.path.isdir(_TRACE_DIR_DEFAULT) else None
        data_4g = build_4g(self.log_dir, trace_dir)
        return events, meta, rrc_twin, rrc_trace, rrc_meta, signaling, data_4g

    def payload(self) -> dict:
        with self.lock:
            events, meta, rrc_twin, rrc_trace, rrc_meta, signaling, data_4g = self._build_all()
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
            }

    def regenerate_html(self) -> None:
        with self.lock:
            events, meta, rrc_twin, rrc_trace, rrc_meta, signaling, data_4g = self._build_all()
            html = render_html(events, meta, rrc_twin, rrc_trace, rrc_meta, signaling,
                               data_4g=data_4g)
            for name in ("index.html", "callflow.html"):
                path = os.path.join(HERE, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)

    def pull_logs(self) -> list[str]:
        """Copy logs from running compose stack into log_dir."""
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

        # 4G LTE stack logs (only if the 4G compose overlay is active)
        compose4g = ["docker", "compose",
                     "-f", os.path.join(INTEGRATION, "docker-compose.yml"),
                     "-f", os.path.join(INTEGRATION, "docker-compose.4g.yml")]

        def cp4g(service: str, src: str, dst: str) -> None:
            dst_path = os.path.join(self.log_dir, dst)
            cmd = compose4g + ["cp", f"{service}:{src}", dst_path]
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                notes.append(f"pulled {service}:{src}")
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass  # 4G stack not running — silently skip

        cp4g("srsue4g", "/tmp/ue4g.log", "ue4g.log")
        cp4g("srsenb",  "/tmp/enb.log",  "enb.log")
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
                return self._json(state.payload())
            if parsed.path == "/api/refresh":
                return self._json(self._refresh())
            if parsed.path == "/api/health":
                return self._json({"ok": True, "mode": state.mode})
            if parsed.path == "/api/trace-sample":
                return self._trace_sample(parsed)
            if parsed.path == "/api/message-sources":
                return self._json(describe_sources())
            return SimpleHTTPRequestHandler.do_GET(self)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/message-sources":
                return self._save_message_sources()
            self.send_error(405)
            return None

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
