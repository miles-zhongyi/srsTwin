# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Named RF presets for the srsTwin signaling storm.

A profile is the per-UE radio condition the IQ hub applies (in ``hub_core``) to
that UE's uplink and downlink IQ. The values are deliberately *conservative*: the
baseline ZMQ link only works with balanced ~0 dB gains (a strong attenuation or
too much noise makes PBCH/PRACH undecodable and the UE never attaches — see the
project's ZMQ-link notes). So even the "edge" profile stays decodable; what it
buys is realistic near-far spread, which drives the PRACH **capture effect** and
makes preamble collisions resolve the way they do on a real cell.

Each profile maps to a channel-parameter dict consumed by ``hub_core.Channel``:

  ul_gain          linear amplitude applied to this UE's uplink before summation
                   (near-far: a distant UE contributes weaker IQ, so a colliding
                   near UE can "capture" the preamble)
  dl_snr_db        target SNR for this UE's downlink copy; the hub adds AWGN to
                   hit it (>=~20 dB stays comfortably decodable here)
  fading           "none" | "rician" | "rayleigh"
  k_factor_db      Rician K (ignored for rayleigh/none)
  doppler_hz       fading rate (sets the AR(1) coherence between IQ blocks)
  cfo_hz           residual carrier frequency offset
  delay_samples    propagation timing offset in samples at 11.52 Msps
"""
from __future__ import annotations

# srate is fixed by the cell (11.52 Msps, 10 MHz band-3). One sample ~= 86.8 ns
# ~= 26 m of propagation, so delay_samples stays small for a small-cell geometry.
#
# THROUGHPUT NOTE: the two most expensive per-subframe ops are CFO (a complex
# exponential over the whole block) and DL AWGN (two random arrays per block).
# On 8 slots they crater the lockstep to ~74 subframes/s (too slow to attach);
# disabling them lifts it to ~1700/s. So the default profiles keep the CHEAP,
# high-value realism — per-UE near-far `ul_gain` (which drives PRACH capture)
# plus small-scale fading — and leave `cfo_hz` / `dl_snr_db` at 0. Re-enable them
# only for a small pool (<=3 UEs) when you specifically want CFO / DL-SINR effects
# (see the "*_heavy" profiles below).
PROFILES = {
    # Close to the gNB: strong, near-pristine, light LOS Rician fading.
    "near": {
        "ul_gain": 1.0,
        "dl_snr_db": 0.0,
        "fading": "rician",
        "k_factor_db": 9.0,
        "doppler_hz": 5.0,
        "cfo_hz": 0.0,
        "delay_samples": 0,
    },
    # Mid-cell: moderate attenuation, mild Rician.
    "mid": {
        "ul_gain": 0.55,
        "dl_snr_db": 0.0,
        "fading": "rician",
        "k_factor_db": 4.0,
        "doppler_hz": 20.0,
        "cfo_hz": 0.0,
        "delay_samples": 2,
    },
    # Cell-edge: weakest UL (capture loser in a collision), NLOS Rayleigh.
    "edge": {
        "ul_gain": 0.30,
        "dl_snr_db": 0.0,
        "fading": "rayleigh",
        "k_factor_db": 0.0,
        "doppler_hz": 50.0,
        "cfo_hz": 0.0,
        "delay_samples": 4,
    },
    # Heavy realism (CFO + DL AWGN) — use only with a tiny pool (<=3 UEs).
    "mid_heavy": {
        "ul_gain": 0.55, "dl_snr_db": 22.0, "fading": "rician",
        "k_factor_db": 4.0, "doppler_hz": 20.0, "cfo_hz": 150.0, "delay_samples": 2,
    },
    "edge_heavy": {
        "ul_gain": 0.30, "dl_snr_db": 20.0, "fading": "rayleigh",
        "k_factor_db": 0.0, "doppler_hz": 50.0, "cfo_hz": 300.0, "delay_samples": 4,
    },
    # Pristine passthrough — identity channel (matches the verified 1-UE baseline).
    "ideal": {
        "ul_gain": 1.0,
        "dl_snr_db": 0.0,        # 0 => no AWGN added
        "fading": "none",
        "k_factor_db": 0.0,
        "doppler_hz": 0.0,
        "cfo_hz": 0.0,
        "delay_samples": 0,
    },
}

DEFAULT_PROFILE = "ideal"


def get(name):
    """Return a copy of the named profile's channel params."""
    if name not in PROFILES:
        raise ValueError(f"unknown RF profile {name!r}; choose from {sorted(PROFILES)}")
    return dict(PROFILES[name])


def assign_profiles(n, mix, seed=None):
    """Assign a profile name to each of ``n`` UEs following a mix of fractions.

    ``mix`` e.g. {"near":0.3,"mid":0.5,"edge":0.2}. Uses largest-remainder rounding
    so the counts sum to exactly ``n``, then shuffles deterministically.
    """
    import numpy as np

    if not mix:
        return [DEFAULT_PROFILE] * n
    names = list(mix)
    weights = np.array([mix[k] for k in names], dtype=float)
    weights = weights / weights.sum()
    raw = weights * n
    counts = np.floor(raw).astype(int)
    remainder = n - counts.sum()
    # hand out the leftover to the largest fractional parts
    order = np.argsort(-(raw - counts))
    for i in range(remainder):
        counts[order[i % len(order)]] += 1
    out = []
    for name, c in zip(names, counts):
        out.extend([name] * int(c))
    rng = np.random.default_rng(seed)
    rng.shuffle(out)
    return out
