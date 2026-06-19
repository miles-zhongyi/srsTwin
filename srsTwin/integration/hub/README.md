# IQ Hub (M1)

ZeroMQ REQ/REP relay between one OCUDU gNB and N srsUE instances.

## Protocol

Matches srsRAN/ocudu ZMQ radio lockstep:

1. Receiver (REQ) sends 1 byte `0xFF`
2. Transmitter (REP) replies with `count * 8` bytes of interleaved complex float32 (`cf_t`)

## Topology

```
gNB DL REP :2000  <--REQ--  hub  --REP-->  UE i DL :300i
gNB UL REQ -> hub :2100 REP  <--sum--  hub REQ -> UE i UL :2001 REP
```

## Offline tests

```bash
cd integration/hub
python tests/test_hub_core.py
python tests/test_lockstep.py
```

## Docker

Built via `integration/Dockerfile.hub`, service `hub` at `10.53.1.4`.

Per-UE sample processing hooks live in `hub_core.py` (identity for M1).
