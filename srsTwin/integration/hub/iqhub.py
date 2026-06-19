#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
srsTwin IQ hub — a ZeroMQ REQ/REP relay between ONE ocudu gNB and N srsUEs.

Per gNB UL request (one slot tick), strictly 1:1:
  1. Answer UL (complex sum of active UEs, or silence when none).
  2. Pull one DL block from the gNB (gNB TX needs UL before it produces DL).
  3. Fan out that DL block to UEs (N=1: wait for UE request; N>1: barrier).

A single loop keeps DL/UL slot counters aligned. Never disconnect UEs on DL
barrier timeout — during attach a UE pauses DL pulls briefly for PRACH.
"""
import os
import sys
import time
import signal
import logging

import json

import zmq

import hub_core
from hub_core import apply_dl, sum_ul

REQ = b"\xff"
DEFAULT_SLOT_BYTES = 11520 * 8


class Slot:
    def __init__(self, idx, ul_connect, dl_bind):
        self.idx = idx
        self.ul_connect = ul_connect
        self.dl_bind = dl_bind
        self.dl_sock = None
        self.ul_sock = None
        self.dl_ep = None
        self.connected = False
        self.last_seen = 0.0
        self.miss = 0           # consecutive UL timeouts -> recycle when too many


class IqHub:
    def __init__(self, gnb_dl_connect, gnb_ul_bind, slots,
                 req_timeout_ms=1500, dl_barrier_timeout_ms=3000, poll_ms=10,
                 default_slot_bytes=DEFAULT_SLOT_BYTES, ul_miss_limit=8,
                 logger=None):
        self.gnb_dl_connect = gnb_dl_connect
        self.gnb_ul_bind = gnb_ul_bind
        self.slots = slots
        self.req_timeout = req_timeout_ms
        self.dl_barrier_timeout = dl_barrier_timeout_ms / 1000.0
        self.poll_ms = poll_ms
        self.ul_miss_limit = ul_miss_limit
        self.default_slot_bytes = default_slot_bytes
        self.log = logger or logging.getLogger("iqhub")
        self.ctx = zmq.Context()
        self.poller = zmq.Poller()
        self.dlsock_slot = {}
        self.gnb_dl = None
        self.gnb_ul = None
        self.gnb_ul_ep = None
        self.running = False
        self.dl_blocks = 0
        self.ul_blocks = 0
        self.last_dl_size = default_slot_bytes

    def setup(self):
        self.gnb_dl = self._new_req(self.gnb_dl_connect)

        self.gnb_ul = self.ctx.socket(zmq.REP)
        self.gnb_ul.setsockopt(zmq.RCVTIMEO, self.req_timeout)
        self.gnb_ul.setsockopt(zmq.SNDTIMEO, self.req_timeout)
        self.gnb_ul.setsockopt(zmq.LINGER, 0)
        self.gnb_ul.bind(self.gnb_ul_bind)
        self.gnb_ul_ep = self.gnb_ul.getsockopt(zmq.LAST_ENDPOINT).decode()

        for s in self.slots:
            s.dl_sock = self.ctx.socket(zmq.REP)
            s.dl_sock.setsockopt(zmq.RCVTIMEO, -1)
            s.dl_sock.setsockopt(zmq.SNDTIMEO, self.req_timeout)
            s.dl_sock.setsockopt(zmq.LINGER, 0)
            s.dl_sock.bind(s.dl_bind)
            s.dl_ep = s.dl_sock.getsockopt(zmq.LAST_ENDPOINT).decode()
            self.poller.register(s.dl_sock, zmq.POLLIN)
            self.dlsock_slot[s.dl_sock] = s
            s.ul_sock = self._new_req(s.ul_connect)

        self.log.info("hub up: gnb_dl<-%s  gnb_ul=%s  slots=%s",
                      self.gnb_dl_connect, self.gnb_ul_ep,
                      [(s.idx, s.dl_ep, s.ul_connect) for s in self.slots])

    def _new_req(self, connect_ep):
        sock = self.ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, self.req_timeout)
        sock.setsockopt(zmq.SNDTIMEO, self.req_timeout)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(connect_ep)
        return sock

    def _pull_dl(self):
        try:
            self.gnb_dl.send(REQ)
            blk = self.gnb_dl.recv()
            self.last_dl_size = len(blk)
            return blk
        except zmq.Again:
            self.log.warning("timeout pulling DL from gNB; resetting socket")
            self.gnb_dl.close(0)
            self.gnb_dl = self._new_req(self.gnb_dl_connect)
            return None

    def _gather_ul(self, fill_len_bytes):
        """Request UL from each connected slot and sum the replies (serial).

        Each slot's UL is a strict REQ/REP handshake that MUST be completed every
        tick — an alive srsUE replies essentially immediately because `continuous_tx`
        keeps its UL block ready, so this never stalls for live UEs. A slot whose UE
        has gone away (detach / container stop) blocks for one `req_timeout` then is
        counted a miss; after `ul_miss_limit` misses the slot is recycled so a new
        UE can take it. (Do NOT abandon a handshake mid-flight to "parallelise" —
        that desyncs the UE's RF thread and freezes the whole cell.)"""
        blocks = []
        for s in self.slots:
            if not s.connected:
                continue
            try:
                s.ul_sock.send(REQ)
                blocks.append((s.idx, s.ul_sock.recv()))
                s.last_seen = time.monotonic()
                s.miss = 0
            except zmq.Again:
                s.ul_sock.close(0)
                s.ul_sock = self._new_req(s.ul_connect)
                s.miss += 1
                if s.miss >= self.ul_miss_limit:
                    self.log.info("UE slot %d left (%d UL misses); recycling slot",
                                  s.idx, s.miss)
                    s.connected = False
                    s.miss = 0
        return sum_ul(blocks, fill_len_bytes)

    def _pickup_joins(self, blk):
        """Non-blocking: any DISCONNECTED slot with a pending DL request joins now.

        A srsUE that (re)starts begins by pulling DL to do SSB/cell search before
        it ever transmits PRACH, so its first DL request is the join signal — even
        while other UEs are already connected. Serving it the broadcast DL block
        lets it acquire and RACH; from the next tick it is summed into the UL."""
        disc = [s for s in self.slots if not s.connected]
        if not disc:
            return
        ready, _, _ = zmq.select([s.dl_sock for s in disc], [], [], 0)
        for sock in ready:
            s = self.dlsock_slot[sock]
            try:
                s.dl_sock.recv()
                s.dl_sock.send(apply_dl(blk, s.idx))
            except zmq.ZMQError:
                continue
            s.connected = True
            s.miss = 0
            s.last_seen = time.monotonic()
            self.log.info("UE slot %d joined (DL request)", s.idx)

    def _serve_dl(self, blk):
        """Deliver `blk` to connected UEs. Never disconnect on barrier timeout."""
        active = [s for s in self.slots if s.connected]

        if len(active) == 1:
            s = active[0]
            if not self.running:
                return
            while self.running:
                ready, _, _ = zmq.select([s.dl_sock], [], [], 0.05)
                if ready:
                    break
            if not self.running:
                return
            s.dl_sock.recv()
            s.dl_sock.send(apply_dl(blk, s.idx))
            s.last_seen = time.monotonic()
            return

        if len(active) > 1:
            pending = {s.idx for s in active}
            served = set()
            deadline = time.monotonic() + self.dl_barrier_timeout
            while self.running and pending:
                wait_ms = int(max(1, min(self.poll_ms, (deadline - time.monotonic()) * 1000)))
                if time.monotonic() >= deadline:
                    break
                events = dict(self.poller.poll(wait_ms))
                for s in active:
                    if s.idx in served or s.dl_sock not in events:
                        continue
                    s.dl_sock.recv()
                    s.dl_sock.send(apply_dl(blk, s.idx))
                    s.last_seen = time.monotonic()
                    served.add(s.idx)
                    pending.discard(s.idx)
                if not pending:
                    return
            if pending:
                self.log.warning(
                    "DL barrier: slot(s) %s missed block (UEs stay connected)", pending
                )
            return

        while self.running:
            ready, _, _ = zmq.select([s.dl_sock for s in self.slots], [], [], 0.05)
            if not ready:
                continue
            s = self.dlsock_slot[ready[0]]
            s.dl_sock.recv()
            s.dl_sock.send(apply_dl(blk, s.idx))
            self.log.info("UE slot %d joined (DL request)", s.idx)
            s.connected = True
            s.last_seen = time.monotonic()
            return

    def run(self):
        self.running = True
        last_log = 0.0
        while self.running:
            try:
                self.gnb_ul.recv()
            except zmq.Again:
                continue
            except zmq.ZMQError:
                if not self.running:
                    break
                raise

            ul = self._gather_ul(self.last_dl_size)
            self.gnb_ul.send(ul)
            self.ul_blocks += 1

            blk = self._pull_dl()
            if blk is not None:
                self._serve_dl(blk)
                self._pickup_joins(blk)
                self.dl_blocks += 1

            now = time.monotonic()
            if now - last_log > 2.0:
                self.log.info(
                    "forwarding: dl_blocks=%d ul_blocks=%d connected=%d/%d",
                    self.dl_blocks, self.ul_blocks,
                    sum(s.connected for s in self.slots), len(self.slots),
                )
                last_log = now

    def stop(self):
        self.running = False

    def close(self):
        self.stop()
        for s in self.slots:
            if s.dl_sock:
                s.dl_sock.close(0)
            if s.ul_sock:
                s.ul_sock.close(0)
        if self.gnb_dl:
            self.gnb_dl.close(0)
        if self.gnb_ul:
            self.gnb_ul.close(0)
        self.ctx.term()


def parse_slots(spec):
    slots = []
    for i, part in enumerate(p for p in spec.split(",") if p.strip()):
        ul, dlport = part.strip().split("@")
        slots.append(Slot(i, "tcp://" + ul, "tcp://*:" + dlport))
    return slots


def main():
    logging.basicConfig(
        level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
        format="%(asctime)s [iqhub] %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    # Per-slot RF channels (near-far / fading / CFO / AWGN). JSON map of
    # slot-index -> Channel kwargs; absent => identity (verified baseline).
    chan_spec = os.environ.get("HUB_CHANNELS", "").strip()
    if chan_spec:
        try:
            raw = json.loads(chan_spec)
            hub_core.configure({int(k): v for k, v in raw.items()})
            logging.getLogger("iqhub").info(
                "RF channels configured for slots %s", sorted(int(k) for k in raw)
            )
        except (ValueError, TypeError) as e:
            logging.getLogger("iqhub").error("bad HUB_CHANNELS (%s); running identity", e)

    hub = IqHub(
        gnb_dl_connect=os.environ.get("HUB_GNB_DL", "tcp://10.53.1.3:2000"),
        gnb_ul_bind=os.environ.get("HUB_GNB_UL", "tcp://*:2100"),
        slots=parse_slots(os.environ.get("HUB_UE_SLOTS", "10.53.1.5:2001@3000")),
        req_timeout_ms=int(os.environ.get("HUB_REQ_TIMEOUT_MS", "1500")),
        dl_barrier_timeout_ms=int(os.environ.get("HUB_DL_BARRIER_TIMEOUT_MS", "10000")),
        poll_ms=int(os.environ.get("HUB_POLL_MS", "10")),
        ul_miss_limit=int(os.environ.get("HUB_UL_MISS_LIMIT", "8")),
    )
    hub.setup()

    def _sig(*_):
        hub.stop()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        hub.run()
    finally:
        hub.close()


if __name__ == "__main__":
    main()
