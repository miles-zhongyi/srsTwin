#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Drive a srsTwin signaling storm from the generated manifest.

Steps:
  1. (optional) build, then bring up 5gc -> gnb -> hub (staged, like compose-up.sh)
  2. `docker compose create` the Layer-A UE containers (created, not started)
  3. Compute the arrival timeline (storm/patterns.py) and play it: each arrival
     grabs a FREE slot from the pool, `docker start`s its container (one full
     attach->ping->detach cycle), and the slot returns to the pool on exit.
     If arrivals outrun the pool they queue — modelling admission/backoff.
  4. Write storm/gen/events.csv (per-arrival scheduled/launch/attach/result).

Layer B (UERANSIM) is dispatched from the same timeline once built — see
_run_layer_b (Phase 2 stub) and storm/README.md.

Run:
  python storm/orchestrate.py                 # bring up + play default scenario
  python storm/orchestrate.py --build         # force image build first
  python storm/orchestrate.py --no-up         # stack already running
  python storm/orchestrate.py --down          # tear the stack down afterwards
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patterns  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "gen")
PROJECT = "srstwin_storm"
DOCKER = shutil.which("docker") or "docker"


def load_manifest():
    path = os.path.join(GEN, "manifest.json")
    if not os.path.exists(path):
        sys.exit("No manifest — run `python storm/generate.py` first.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def _run(*args, capture=True, check=False):
    """Run a command; return (rc, stdout_text)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE if capture else None,
        stderr=asyncio.subprocess.STDOUT if capture else None,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace") if out else ""
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args)}\n{text}")
    return proc.returncode, text


class Orchestrator:
    def __init__(self, manifest, args):
        self.m = manifest
        self.args = args
        self.compose_file = os.path.normpath(os.path.join(HERE, manifest["compose_file"]))
        self.slots = manifest["layer_a"]["slots"]
        self.behavior = manifest["layer_a"]["behavior"]
        self.t0 = None
        self.events = []
        self.free = asyncio.Queue()
        self.log_lock = asyncio.Lock()

    def _compose(self, *args):
        return [DOCKER, "compose", "-p", PROJECT, "-f", self.compose_file, *args]

    def _rel(self):
        return time.monotonic() - self.t0 if self.t0 else 0.0

    async def log(self, msg):
        async with self.log_lock:
            print(f"[{self._rel():7.1f}s] {msg}", flush=True)

    # ---- bring-up -------------------------------------------------------
    async def bringup(self):
        if self.args.build:
            await self.log("building images (this is slow the first time)…")
            await _run(*self._compose("build"), capture=False, check=True)

        await self.log("starting 5gc…")
        await _run(*self._compose("up", "-d", "5gc"), check=True)
        await self._wait_healthy("srstwin_5gc")

        await self.log("starting gnb…")
        await _run(*self._compose("up", "-d", "gnb"), check=True)
        await self._wait_log("srstwin_gnb", "/tmp/gnb.log",
                             "DU started successfully|Cell was activated|Cell scheduling was activated")

        await self.log("starting hub…")
        await _run(*self._compose("up", "-d", "hub"), check=True)
        await asyncio.sleep(3)

        if self.m["layer_b"]["total_ues"] > 0:
            await self.log("starting UERANSIM gNB (Layer B)…")
            await _run(*self._compose("up", "-d", "ueransim"), check=True)
            await asyncio.sleep(5)   # let nr-gnb complete its NGAP setup to the AMF

        services = [s["service"] for s in self.slots]
        await self.log(f"creating {len(services)} UE containers (not started)…")
        await _run(*self._compose("create", *services), check=True)

    async def _wait_healthy(self, container, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, out = await _run(DOCKER, "inspect", "-f",
                                 "{{.State.Health.Status}}", container)
            if rc == 0 and out.strip() == "healthy":
                return
            await asyncio.sleep(2)
        raise RuntimeError(f"{container} not healthy after {timeout}s")

    async def _wait_log(self, container, path, pattern, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, _ = await _run(DOCKER, "exec", container, "sh", "-c",
                               f"grep -qiE '{pattern}' {path}")
            if rc == 0:
                return
            await asyncio.sleep(2)
        raise RuntimeError(f"{container}: pattern not seen in {path} after {timeout}s")

    # ---- timeline play --------------------------------------------------
    async def play(self):
        for s in self.slots:
            self.free.put_nowait(s["slot"])

        n = int(self.m["layer_a"]["total_arrivals"])
        timeline = patterns.build_timeline(
            self.m["pattern"]["type"], n, self.m["duration_s"],
            self.m["pattern"].get("params"), seed=self.m["seed"],
        )
        await self.log(f"playing {n} arrivals over {self.m['duration_s']}s "
                       f"(pattern={self.m['pattern']['type']}, pool={len(self.slots)})")

        self.t0 = time.monotonic()
        tasks = [asyncio.create_task(self._arrival(i, float(t)))
                 for i, (t, _id) in enumerate(timeline)]

        # Layer B runs concurrently from the same timeline (Phase 2).
        if self.m["layer_b"]["total_ues"] > 0:
            tasks.append(asyncio.create_task(self._run_layer_b()))

        await asyncio.gather(*tasks)
        await self.log("all arrivals complete")

    async def _arrival(self, idx, sched_t):
        # wait for the scheduled instant, then for a free slot (queueing = backoff)
        delay = sched_t - self._rel()
        if delay > 0:
            await asyncio.sleep(delay)
        slot_idx = await self.free.get()
        slot = self.slots[slot_idx]
        launch_t = self._rel()
        ev = {"idx": idx, "slot": slot_idx, "container": slot["container"],
              "imsi": slot["imsi"], "profile": slot["profile"],
              "sched_s": round(sched_t, 2), "launch_s": round(launch_t, 2),
              "attach_s": "", "result": "", "exit_code": "", "end_s": ""}
        try:
            await self.log(f"arr#{idx:03d} -> slot {slot_idx} ({slot['profile']}) start "
                           f"{slot['container']} (queued {launch_t - sched_t:.1f}s)")
            await _run(DOCKER, "start", slot["container"], check=True)
            rc, _ = await _run(DOCKER, "wait", slot["container"])
            ev["exit_code"] = rc
            ev["end_s"] = round(self._rel(), 2)
            await self._read_markers(slot["container"], ev)
            await self.log(f"arr#{idx:03d} slot {slot_idx} {ev['result']} "
                           f"(latency={ev['attach_s']}s)")
        finally:
            self.events.append(ev)
            self.free.put_nowait(slot_idx)

    async def _read_markers(self, container, ev):
        """Parse the lifecycle markers from this container's most recent run."""
        _rc, logs = await _run(DOCKER, "logs", container)
        lines = logs.splitlines()
        # restrict to the last run (after the final 'LIFECYCLE start')
        starts = [i for i, ln in enumerate(lines) if "LIFECYCLE start" in ln]
        if starts:
            lines = lines[starts[-1]:]
        result = "unknown"
        for ln in lines:
            if "LIFECYCLE attached" in ln:
                result = "attached"
                for tok in ln.split():
                    if tok.startswith("latency="):
                        ev["attach_s"] = tok.split("=", 1)[1]
            elif "LIFECYCLE failed" in ln:
                reason = next((t.split("=", 1)[1] for t in ln.split()
                               if t.startswith("reason=")), "failed")
                result = f"failed:{reason}"
        ev["result"] = result

    async def _run_layer_b(self):
        """Register UERANSIM UEs over the SAME arrival pattern as Layer A.

        Arrivals are bucketed by second; each bucket is one `nr-ue -n <count>
        -i <imsi>` launch (one process carrying that second's UEs), so a burst
        hits the AMF as a registration surge. IMSIs are assigned in arrival order
        starting from the Layer-B base (provisioned in subscribers.storm.csv)."""
        nb = int(self.m["layer_b"]["total_ues"])
        base = int(self.m["layer_b"]["base_imsi"])
        # seed+1 so Layer-B arrivals are a distinct realization of the same pattern
        timeline = patterns.build_timeline(
            self.m["pattern"]["type"], nb, self.m["duration_s"],
            self.m["pattern"].get("params"), seed=self.m["seed"] + 1,
        )
        # bucket by integer second -> (imsi_start, count)
        buckets = {}
        for i, (t, _id) in enumerate(timeline):
            buckets.setdefault(int(t), []).append(i)
        await self.log(f"layer_b: {nb} UERANSIM UEs in {len(buckets)} arrival buckets")

        async def fire(sec, idxs):
            delay = sec - self._rel()
            if delay > 0:
                await asyncio.sleep(delay)
            imsi = base + idxs[0]
            rc, out = await _run(DOCKER, "exec", "srstwin_ueransim", "bash",
                                 "/ueransim_launch.sh", str(imsi), str(len(idxs)))
            await self.log(f"layer_b: +{len(idxs)} UEs from imsi-{imsi} "
                           f"({'ok' if rc == 0 else 'ERR'})")

        await asyncio.gather(*(fire(s, idxs) for s, idxs in sorted(buckets.items())))
        await self.log("layer_b: all registration batches launched")

    # ---- output ---------------------------------------------------------
    def write_events(self):
        import csv
        path = os.path.join(GEN, "events.csv")
        cols = ["idx", "slot", "container", "imsi", "profile", "sched_s",
                "launch_s", "attach_s", "result", "exit_code", "end_s"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for ev in sorted(self.events, key=lambda e: e["idx"]):
                w.writerow(ev)
        ok = sum(1 for e in self.events if e["result"] == "attached")
        print(f"\nWrote {path}")
        print(f"Summary: {ok}/{len(self.events)} arrivals attached "
              f"({100*ok/max(len(self.events),1):.0f}%).")
        lat = [float(e["attach_s"]) for e in self.events if e["attach_s"]]
        if lat:
            lat.sort()
            p50 = lat[len(lat)//2]
            p90 = lat[min(len(lat)-1, int(len(lat)*0.9))]
            print(f"Attach latency: p50={p50:.0f}s p90={p90:.0f}s max={lat[-1]:.0f}s")

    async def teardown(self):
        await self.log("tearing down stack…")
        await _run(*self._compose("down", "-v"), capture=False)


async def main_async():
    ap = argparse.ArgumentParser(description="Play a srsTwin signaling storm.")
    ap.add_argument("--build", action="store_true", help="build images first")
    ap.add_argument("--no-up", action="store_true", help="stack already running")
    ap.add_argument("--down", action="store_true", help="tear down after the run")
    args = ap.parse_args()

    manifest = load_manifest()
    orch = Orchestrator(manifest, args)
    try:
        if not args.no_up:
            await orch.bringup()
        await orch.play()
    finally:
        orch.write_events()
        if args.down:
            await orch.teardown()


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
