# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Offline end-to-end test of the IQ hub over real ZeroMQ sockets (no containers).

Fakes a gNB and N srsUEs around a live IqHub and asserts:
  * DL fan-out: one block pulled from the gNB is delivered, byte-for-byte, to
    EVERY UE, and the gNB DL is pulled exactly once per slot (not once per UE).
  * Lockstep order: each UE receives the DL blocks in order with no gaps/dupes.
  * UL summation: the gNB receives the exact complex sum of the UEs' UL blocks.

All sockets bind to port 0 and advertise their real endpoint (LAST_ENDPOINT),
so there are no port-allocation races.
"""
import logging
import os
import sys
import threading
import time

import numpy as np
import zmq

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from iqhub import IqHub, Slot  # noqa: E402

logging.getLogger("iqhub").setLevel(logging.CRITICAL)  # quiet teardown warnings

REQ = b"\xff"
K = 6  # rounds (slots)


def _iq(values):
    return np.array(values, dtype=np.complex64).view(np.float32).tobytes()


def _bind0(ctx, kind):
    s = ctx.socket(kind)
    s.bind("tcp://127.0.0.1:0")
    return s, s.getsockopt(zmq.LAST_ENDPOINT).decode()


def run_lockstep(num_ues):
    ctx = zmq.Context.instance()

    dl_blocks = [_iq([(k + 1) + 1j * (k + 1)] * 4) for k in range(K)]
    ul_blocks = [[_iq([(10 * (u + 1) + k) + 0j] * 4) for k in range(K)]
                 for u in range(num_ues)]

    # Fake gNB DL: REP server the hub REQs from.
    gnb_dl, gnb_dl_ep = _bind0(ctx, zmq.REP)
    gnb_dl_served = [0]

    def gnb_dl_thread():
        for k in range(K):
            gnb_dl.recv()
            gnb_dl.send(dl_blocks[k])
            gnb_dl_served[0] += 1

    # Fake UE UL: REP servers the hub REQs from.
    ue_ul_socks, ue_ul_eps = [], []
    for _ in range(num_ues):
        s, ep = _bind0(ctx, zmq.REP)
        ue_ul_socks.append(s); ue_ul_eps.append(ep)

    def ue_ul_thread(u):
        for k in range(K):
            ue_ul_socks[u].recv()
            ue_ul_socks[u].send(ul_blocks[u][k])

    # Build the hub (binds gnb_ul + per-UE DL on :0; connects gnb_dl + per-UE UL).
    slots = [Slot(u, ue_ul_eps[u], "tcp://127.0.0.1:0") for u in range(num_ues)]
    hub = IqHub(gnb_dl_connect=gnb_dl_ep, gnb_ul_bind="tcp://127.0.0.1:0",
                slots=slots, req_timeout_ms=1500, dl_barrier_timeout_ms=2000, poll_ms=5,
                default_slot_bytes=len(dl_blocks[0]))
    hub.setup()
    # Pre-connect slots so the UL loop gathers from tick 0 (deterministic test).
    for s in slots:
        s.connected = True
        s.last_seen = time.monotonic()

    # Fake UEs DL: REQ clients to the hub's per-UE DL REP. Pre-queue the first
    # request BEFORE the hub runs so slot 0 is delivered deterministically.
    ue_rx = [[] for _ in range(num_ues)]
    ue_dl_socks = []
    for u in range(num_ues):
        s = ctx.socket(zmq.REQ); s.setsockopt(zmq.RCVTIMEO, 3000)
        s.connect(slots[u].dl_ep)
        ue_dl_socks.append(s)

    def ue_dl_thread(u):
        for _ in range(K):
            ue_dl_socks[u].send(REQ)
            ue_rx[u].append(ue_dl_socks[u].recv())

    # Fake gNB UL: REQ client to the hub's gnb_ul REP.
    gnb_rx = []
    gnb_ul = ctx.socket(zmq.REQ); gnb_ul.setsockopt(zmq.RCVTIMEO, 3000)
    gnb_ul.connect(hub.gnb_ul_ep)

    def gnb_ul_thread():
        for _ in range(K):
            gnb_ul.send(REQ)
            gnb_rx.append(gnb_ul.recv())

    threads = [threading.Thread(target=gnb_dl_thread)]
    for u in range(num_ues):
        threads.append(threading.Thread(target=ue_dl_thread, args=(u,)))
        threads.append(threading.Thread(target=ue_ul_thread, args=(u,)))
    threads.append(threading.Thread(target=gnb_ul_thread))
    for t in threads:
        t.daemon = True
        t.start()
    time.sleep(0.2)  # let the UE DL requests queue at the hub before it runs

    hub_thread = threading.Thread(target=hub.run, daemon=True)
    hub_thread.start()

    for t in threads:
        t.join(timeout=15)
    hub.stop()
    hub_thread.join(timeout=5)
    hub.close()
    for s in ue_ul_socks + ue_dl_socks + [gnb_dl, gnb_ul]:
        s.close(0)

    # --- assertions ---
    # DL fan-out: gNB pulled exactly K times (one per slot, not once per UE),
    # and every UE received all K blocks in order, byte-identical.
    assert gnb_dl_served[0] == K, f"gNB DL pulled {gnb_dl_served[0]} times, expected {K}"
    for u in range(num_ues):
        assert ue_rx[u] == dl_blocks, f"UE {u} DL mismatch (got {len(ue_rx[u])} blocks)"
    # UL summation: with the slots pre-connected, every gNB UL answer is the
    # complex sum of that slot's UL block from each UE.
    assert len(gnb_rx) == K
    for k in range(K):
        got = np.frombuffer(gnb_rx[k], dtype=np.float32).view(np.complex64)
        want = sum(np.frombuffer(ul_blocks[u][k], dtype=np.float32).view(np.complex64)
                   for u in range(num_ues))
        assert np.allclose(got, want), f"UL sum mismatch at slot {k}: {got} != {want}"


def test_lockstep_n1_identity():
    run_lockstep(1)


def test_lockstep_n2_sum():
    run_lockstep(2)


def test_lockstep_n3_sum():
    run_lockstep(3)


def _run():
    for name in ["test_lockstep_n1_identity", "test_lockstep_n2_sum", "test_lockstep_n3_sum"]:
        globals()[name]()
        print(f"PASS {name}")
    print("\nlockstep tests passed.")


if __name__ == "__main__":
    _run()
