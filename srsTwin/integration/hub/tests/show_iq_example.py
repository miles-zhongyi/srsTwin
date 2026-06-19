#!/usr/bin/env python3
"""Print what ZMQ IQ blocks look like on the wire (srsRAN/ocudu cf_t format)."""
import struct

import numpy as np

SAMPLES_PER_SLOT = 11520
BYTES_PER_SLOT = SAMPLES_PER_SLOT * 8


def show_block(label, values, max_show=8):
    arr = np.array(values, dtype=np.complex64)
    wire = arr.view(np.float32).tobytes()
    print(f"=== {label} ===")
    print(f"  complex samples: {len(arr)}  ->  wire bytes: {len(wire)}")
    n = min(max_show, len(arr))
    print(f"  first {n} samples (re + j*im):")
    for i, z in enumerate(arr[:n]):
        print(f"    [{i}] {z.real:+.6f} + {z.imag:+.6f}j  |mag|={abs(z):.6f}")
    head = wire[:32]
    print(f"  raw bytes (first 32): {head.hex(' ')}")
    floats = np.frombuffer(head, dtype=np.float32)
    print(f"  as float32 [re, im, re, im, ...]: {floats.tolist()}")
    print()


print("=== ZMQ sample REQUEST (1 byte, sent before every IQ block) ===")
print("  hex: ff   decimal: [255]")
print()

show_block("Silence — one complex sample", [0 + 0j])
show_block("Silence — NR slot (11520 samples)", [0 + 0j] * SAMPLES_PER_SLOT, max_show=4)
print(f"  ... samples [4..{SAMPLES_PER_SLOT - 1}] are all 0+0j")
print(f"  full slot on wire: {BYTES_PER_SLOT} bytes ({SAMPLES_PER_SLOT} x 8)")
print()

show_block("Test IQ (hub/tests/test_lockstep.py slot k=0, 4 samples)", [(1 + 1j)] * 4)

qpsk = [0.707 + 0.707j, -0.707 + 0.707j, -0.707 - 0.707j, 0.707 - 0.707j]
show_block("Illustrative QPSK points (not real captured RAR IQ)", qpsk)

z = 1.5 + 2.25j
wire_one = np.array([z], dtype=np.complex64).view(np.float32).tobytes()
print("=== One cf_t sample (8 bytes, little-endian float32) ===")
print(f"  value: {z}")
print(f"  hex:   {wire_one.hex(' ')}")
re, im = struct.unpack("<ff", wire_one)
print(f"  decoded: re={re}, im={im}")
