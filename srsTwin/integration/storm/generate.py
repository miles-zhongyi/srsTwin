#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Render all srsTwin signaling-storm artifacts from a scenario file.

Inputs : storm/scenario.yml
Outputs (under integration/storm/gen/):
  subscribers.storm.csv     Open5GS SIM DB for both layers (sequential IMSIs)
  ue_NN.conf                one srsUE config per Layer-A slot (RF-real pool)
  docker-compose.storm.yml  self-contained stack: 5gc + gnb(hub) + hub + N UEs
  manifest.json             everything orchestrate.py needs (slots, IPs, IMSIs,
                            profiles, arrival pattern, behavior)

Why generate instead of `docker compose --scale`: every replica would share the
same ZMQ ports / IP / SIM. We need each slot distinct, so we template them.

Layer A uses a BOUNDED pool of `pool_size` distinct SIMs (the CPU knob); the
orchestrator recycles those slots across `total_arrivals` events. Distinct-device
*scale* is Layer B's job (UERANSIM, many cheap SIMs) — see scenario `layer_b`.
"""
from __future__ import annotations

import json
import os
import shutil
from string import Template

import yaml

import rf_profiles

HERE = os.path.dirname(os.path.abspath(__file__))            # integration/storm
INTEGRATION = os.path.dirname(HERE)                          # integration/
GEN = os.path.join(HERE, "gen")
TEMPLATES = os.path.join(HERE, "templates")

# Shared test credentials (match the existing subscribers.csv / ue_zmq.conf).
KEY = "00112233445566778899aabbccddeeff"
OPC = "63bfa50ee6523365ff14c1f45f88737d"
AMF_VAL = "8000"
QCI = "9"

# IP plan on the 10.53.1.0/24 RAN net (5gc .2, gnb .3, hub .4 are fixed).
HUB_IP = "10.53.1.4"
AMF_IP = "10.53.1.2"
UE_IP_BASE = 20            # storm UE slots: 10.53.1.20, .21, ...
UERANSIM_IP = "10.53.1.11"
DL_PORT_BASE = 3000        # hub DL REP per slot: 3000, 3001, ...

IMSI_BASE = "001010123456780"
IMEI_BASE = "353490069873319"


def _digits_inc(base: str, n: int) -> str:
    """Increment a fixed-width numeric string, preserving width."""
    return str(int(base) + n).zfill(len(base))


def load_scenario(path):
    with open(path, encoding="utf-8") as f:
        sc = yaml.safe_load(f)
    sc.setdefault("name", "storm")
    sc.setdefault("duration_s", 120)
    sc.setdefault("seed", 0)
    a = sc.setdefault("layer_a", {})
    a.setdefault("pool_size", 3)
    a.setdefault("total_arrivals", a["pool_size"])
    a.setdefault("rf_profile_mix", {"ideal": 1.0})
    a.setdefault("behavior", {"ping_count": 5, "attach_timeout_s": 180, "idle_after_s": 0})
    b = sc.setdefault("layer_b", {})
    b.setdefault("total_ues", 0)
    sc.setdefault("pattern", {"type": "burst", "params": {"start_s": 10, "window_s": 5}})
    return sc


def build_slots(sc):
    """Per-slot descriptors for the Layer-A pool (one srsUE container each)."""
    n = int(sc["layer_a"]["pool_size"])
    profiles = rf_profiles.assign_profiles(
        n, sc["layer_a"]["rf_profile_mix"], seed=sc["seed"]
    )
    slots = []
    for i in range(n):
        slots.append({
            "slot": i,
            "container": f"srstwin_storm_ue{i:02d}",
            "service": f"srsue_{i:02d}",
            "ip": f"10.53.1.{UE_IP_BASE + i}",
            "dl_port": DL_PORT_BASE + i,
            "imsi": _digits_inc(IMSI_BASE, i),
            "imei": _digits_inc(IMEI_BASE, i),
            "profile": profiles[i],
        })
    return slots


def hub_channels(slots, seed):
    """JSON-able {slot: Channel-kwargs} for non-ideal slots (identity ones omitted)."""
    chans = {}
    for s in slots:
        params = rf_profiles.get(s["profile"])
        if s["profile"] in ("ideal",):
            continue
        params["seed"] = int(seed) + s["slot"] + 1   # reproducible per slot
        chans[str(s["slot"])] = params
    return chans


def write_subscribers(slots, sc):
    """Layer-A pool SIMs + Layer-B SIMs, sequential IMSIs, dynamic IP (blank)."""
    rows = ["# name,imsi,key,op_type,op_c,amf,qci,ip_alloc"]

    def row(name, imsi):
        return f"{name},{imsi},{KEY},opc,{OPC},{AMF_VAL},{QCI},"

    for s in slots:
        rows.append(row(s["service"], s["imsi"]))
    # Layer B distinct SIMs continue the IMSI sequence after the pool.
    nb = int(sc["layer_b"]["total_ues"])
    start = len(slots)
    for j in range(nb):
        imsi = _digits_inc(IMSI_BASE, start + j)
        rows.append(row(f"ueransim_{j:04d}", imsi))
    path = os.path.join(GEN, "subscribers.storm.csv")
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    return path, nb


def render_ue_configs(slots):
    with open(os.path.join(TEMPLATES, "ue.conf.tmpl"), encoding="utf-8") as f:
        tmpl = Template(f.read())
    for s in slots:
        conf = tmpl.substitute(
            SLOT=s["slot"], HUB_IP=HUB_IP, DL_PORT=s["dl_port"],
            OPC=OPC, KEY=KEY, IMSI=s["imsi"], IMEI=s["imei"],
        )
        with open(os.path.join(GEN, f"ue_{s['slot']:02d}.conf"), "w",
                  newline="\n", encoding="utf-8") as f:
            f.write(conf)


def render_ueransim(slots, nb):
    """Render UERANSIM gnb + ue configs for Layer B. Returns the base IMSI used."""
    if nb <= 0:
        return None
    base_imsi = _digits_inc(IMSI_BASE, len(slots))   # continues after the pool
    with open(os.path.join(TEMPLATES, "ueransim_gnb.yaml.tmpl"), encoding="utf-8") as f:
        gnb = Template(f.read()).substitute(GNB_IP=UERANSIM_IP, AMF_IP=AMF_IP)
    with open(os.path.join(TEMPLATES, "ueransim_ue.yaml.tmpl"), encoding="utf-8") as f:
        ue = Template(f.read()).substitute(
            BASE_IMSI=base_imsi, KEY=KEY.upper(), OPC=OPC.upper(), GNB_IP=UERANSIM_IP)
    with open(os.path.join(GEN, "ueransim_gnb.yaml"), "w", newline="\n", encoding="utf-8") as f:
        f.write(gnb)
    with open(os.path.join(GEN, "ueransim_ue.yaml"), "w", newline="\n", encoding="utf-8") as f:
        f.write(ue)
    return base_imsi


def render_compose(slots, sc, channels, nb):
    """Self-contained compose: 5gc + gnb(hub) + hub + N storm UE services.

    The UE services are defined but the orchestrator `create`s them and `start`s
    each per the arrival schedule (compose has no per-service 'do not start')."""
    behavior = sc["layer_a"]["behavior"]
    slot_spec = ",".join(f"{s['ip']}:2001@{s['dl_port']}" for s in slots)

    # The compose file is written to integration/, so build contexts and volume
    # paths reuse the SAME proven relative paths as the hand-written base compose
    # (../ocudu, ../srsRAN_4G, ./gnb_zmq.hub.yml, Dockerfile.hub). Only the
    # generated artifacts live under ./storm/gen/.
    services = {
        "5gc": {
            "container_name": "srstwin_5gc",
            "build": {
                "context": "../ocudu/docker/open5gs",
                "target": "open5gs",
                "args": {"OS_VERSION": "22.04", "OPEN5GS_VERSION": "v2.7.6"},
            },
            "env_file": ["./open5gs.env"],
            "volumes": ["./storm/gen/subscribers.storm.csv:/subscribers.csv:ro"],
            "privileged": True,
            "command": "5gc -c open5gs-5gc.yml",
            "healthcheck": {
                "test": ["CMD-SHELL", "nc -z 127.0.0.20 7777"],
                "interval": "3s", "timeout": "1s", "retries": 60,
            },
            "networks": {"ran": {"ipv4_address": "10.53.1.2"}},
        },
        "gnb": {
            "container_name": "srstwin_gnb",
            "build": {"context": "../ocudu", "dockerfile": "../integration/Dockerfile.gnb"},
            "cap_add": ["SYS_NICE", "NET_ADMIN"],
            "volumes": ["./gnb_zmq.hub.yml:/gnb_zmq.yml:ro", "gnb-logs:/tmp"],
            "depends_on": {"5gc": {"condition": "service_healthy"}},
            "command": ["-c", "/gnb_zmq.yml"],
            "networks": {"ran": {"ipv4_address": "10.53.1.3"}},
        },
        "hub": {
            "container_name": "srstwin_hub",
            "build": {"context": ".", "dockerfile": "Dockerfile.hub"},
            "restart": "unless-stopped",
            "environment": {
                "HUB_GNB_DL": "tcp://10.53.1.3:2000",
                "HUB_GNB_UL": "tcp://*:2100",
                "HUB_UE_SLOTS": slot_spec,
                "HUB_CHANNELS": json.dumps(channels, separators=(",", ":")),
                "HUB_DL_BARRIER_TIMEOUT_MS": "10000",
                "HUB_UL_MISS_LIMIT": "8",
                "HUB_REQ_TIMEOUT_MS": "1500",
            },
            "depends_on": ["gnb"],
            "networks": {"ran": {"ipv4_address": HUB_IP}},
        },
    }

    for s in slots:
        services[s["service"]] = {
            "container_name": s["container"],
            "build": {"context": "../srsRAN_4G", "dockerfile": "../integration/Dockerfile.ue"},
            "cap_add": ["NET_ADMIN", "SYS_NICE"],
            "devices": ["/dev/net/tun"],
            "entrypoint": ["/bin/bash", "/ue_lifecycle.sh"],
            "command": ["/ue_zmq.conf"],
            "environment": {
                "PING_COUNT": str(behavior.get("ping_count", 5)),
                "PING_TARGET": behavior.get("ping_target", "10.45.0.1"),
                "ATTACH_TIMEOUT": str(behavior.get("attach_timeout_s", 90)),
                "IDLE_AFTER": str(behavior.get("idle_after_s", 0)),
            },
            "volumes": [
                f"./storm/gen/ue_{s['slot']:02d}.conf:/ue_zmq.conf:ro",
                "./storm/ue_lifecycle.sh:/ue_lifecycle.sh:ro",
            ],
            "networks": {"ran": {"ipv4_address": s["ip"]}},
        }

    if nb > 0:
        services["ueransim"] = {
            "container_name": "srstwin_ueransim",
            "build": {"context": ".", "dockerfile": "Dockerfile.ueransim"},
            "cap_add": ["NET_ADMIN"],
            "devices": ["/dev/net/tun"],
            "volumes": [
                "./storm/gen/ueransim_gnb.yaml:/gnb.yaml:ro",
                "./storm/gen/ueransim_ue.yaml:/ue.yaml:ro",
                "./storm/ueransim_launch.sh:/ueransim_launch.sh:ro",
            ],
            "depends_on": {"5gc": {"condition": "service_healthy"}},
            "networks": {"ran": {"ipv4_address": UERANSIM_IP}},
        }

    compose = {
        "services": services,
        "volumes": {"gnb-logs": None},
        "networks": {
            "ran": {"ipam": {"driver": "default", "config": [{"subnet": "10.53.1.0/24"}]}}
        },
    }
    # Written to integration/ so build contexts match the base compose's paths.
    path = os.path.join(INTEGRATION, "docker-compose.storm.yml")
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        yaml.safe_dump(compose, f, sort_keys=False, default_flow_style=False)
    return path


def write_manifest(slots, sc, compose_path, sub_path, nb):
    manifest = {
        "name": sc["name"],
        "duration_s": sc["duration_s"],
        "seed": sc["seed"],
        "pattern": sc["pattern"],
        "layer_a": {
            "pool_size": len(slots),
            "total_arrivals": int(sc["layer_a"]["total_arrivals"]),
            "behavior": sc["layer_a"]["behavior"],
            "slots": slots,
        },
        "layer_b": {"total_ues": nb, "base_imsi": sc.get("_layer_b_base_imsi"),
                    "gnb_ip": UERANSIM_IP},
        "compose_file": os.path.relpath(compose_path, HERE),
        "subscribers": os.path.relpath(sub_path, HERE),
    }
    path = os.path.join(GEN, "manifest.json")
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return path


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Render srsTwin storm artifacts.")
    ap.add_argument("scenario", nargs="?", default=os.path.join(HERE, "scenario.yml"))
    args = ap.parse_args()

    sc = load_scenario(args.scenario)
    if os.path.isdir(GEN):
        shutil.rmtree(GEN)
    os.makedirs(GEN)

    slots = build_slots(sc)
    channels = hub_channels(slots, sc["seed"])
    sub_path, nb = write_subscribers(slots, sc)
    render_ue_configs(slots)
    sc["_layer_b_base_imsi"] = render_ueransim(slots, nb)
    compose_path = render_compose(slots, sc, channels, nb)
    manifest_path = write_manifest(slots, sc, compose_path, sub_path, nb)

    # Lockstep sanity: N full-PHY UEs share ONE ZMQ lockstep, so the virtual clock
    # slows ~linearly with N (it is lockstep-bound, not core-bound). Beyond ~4 the
    # per-UE attach time grows fast — warn and point scale at Layer B.
    pool = len(slots)
    warn = ""
    if pool > 4:
        warn = (f"  WARNING: pool_size={pool}. Full-PHY UEs share one ZMQ lockstep; "
                f"the cell's virtual clock slows ~linearly with the pool, so attach "
                f"latency climbs fast above ~4. Keep the pool small and push scale to "
                f"layer_b (UERANSIM).\n")

    print(f"Generated storm '{sc['name']}' in {GEN}/")
    print(f"  Layer A: {pool} RF-real slots, {sc['layer_a']['total_arrivals']} arrivals, "
          f"pattern={sc['pattern']['type']}")
    print(f"  RF profiles: {[s['profile'] for s in slots]}")
    print(f"  Channels (non-ideal): slots {sorted(int(k) for k in channels)}")
    print(f"  Layer B: {nb} UERANSIM UEs")
    print(f"  compose: {os.path.relpath(compose_path, HERE)}")
    print(f"  manifest: {os.path.relpath(manifest_path, HERE)}")
    if warn:
        print(warn, end="")


if __name__ == "__main__":
    main()
