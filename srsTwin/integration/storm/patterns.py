# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Arrival-pattern engine for the srsTwin signaling storm.

Every pattern is a pure function that turns a request for ``n`` arrivals over a
``duration_s`` window into a sorted list of arrival *times* (seconds, float). The
orchestrator layers UE identity / slot assignment on top — these functions know
nothing about Docker, ZMQ or UERANSIM, which keeps them trivially unit-testable.

Patterns model the canonical causes of a 5G signaling storm:

  burst            flash-crowd: ~all UEs arrive inside a tight window
  outage_recovery  mass reconnect after power restored: spike + exp decay tail
  poisson          independent arrivals at mean rate (homogeneous Poisson process)
  ramp             linearly increasing arrival rate
  periodic         oscillating rate (diurnal waves / periodic IoT check-ins)

The non-uniform-intensity patterns (outage/ramp/periodic) are sampled by
inverse-CDF over a fine time grid so they return *exactly* ``n`` arrivals and are
reproducible from a seed.
"""
from __future__ import annotations

import numpy as np

# Resolution of the time grid used to invert non-uniform intensities.
_GRID = 4096


def _invert_intensity(intensity, n, duration_s, rng):
    """Sample ``n`` arrival times whose density follows ``intensity(t) >= 0``.

    Builds the normalized CDF of ``intensity`` on a grid over [0, duration_s] and
    maps ``n`` uniform quantiles through its inverse. Returns sorted times.
    """
    t = np.linspace(0.0, duration_s, _GRID)
    lam = np.asarray(intensity(t), dtype=np.float64)
    lam = np.clip(lam, 0.0, None)
    if lam.sum() <= 0:
        lam = np.ones_like(lam)
    cdf = np.cumsum(lam)
    cdf /= cdf[-1]
    u = rng.random(n)
    times = np.interp(u, cdf, t)
    return np.sort(times)


def burst(n, duration_s, *, start_s=0.0, window_s=1.0, rng=None):
    """Flash-crowd: ``n`` arrivals spread uniformly across a tight window."""
    rng = rng or np.random.default_rng()
    window_s = max(window_s, 1e-6)
    start_s = min(start_s, max(duration_s - window_s, 0.0))
    times = start_s + rng.random(n) * window_s
    return np.sort(np.clip(times, 0.0, duration_s))


def outage_recovery(n, duration_s, *, start_s=0.0, tau_s=10.0, rng=None):
    """Mass reconnect: a spike at ``start_s`` decaying with time-constant ``tau_s``.

    Arrival intensity ~ exp(-(t - start_s)/tau) for t >= start_s, else 0.
    """
    rng = rng or np.random.default_rng()
    tau_s = max(tau_s, 1e-6)

    def intensity(t):
        lam = np.exp(-(t - start_s) / tau_s)
        lam[t < start_s] = 0.0
        return lam

    return _invert_intensity(intensity, n, duration_s, rng)


def poisson(n, duration_s, *, rng=None, **_):
    """Homogeneous Poisson process conditioned on ``n`` events => uniform times."""
    rng = rng or np.random.default_rng()
    return np.sort(rng.random(n) * duration_s)


def ramp(n, duration_s, *, rate_start=0.2, rate_end=1.0, rng=None):
    """Linearly increasing (or decreasing) arrival rate over the window."""
    rng = rng or np.random.default_rng()

    def intensity(t):
        frac = t / max(duration_s, 1e-9)
        return rate_start + (rate_end - rate_start) * frac

    return _invert_intensity(intensity, n, duration_s, rng)


def periodic(n, duration_s, *, period_s=30.0, amplitude=0.8, phase=0.0, rng=None):
    """Oscillating arrival rate: lam(t) = 1 + amplitude*sin(2*pi*t/period + phase)."""
    rng = rng or np.random.default_rng()
    period_s = max(period_s, 1e-6)
    amplitude = float(np.clip(amplitude, 0.0, 1.0))

    def intensity(t):
        return 1.0 + amplitude * np.sin(2 * np.pi * t / period_s + phase)

    return _invert_intensity(intensity, n, duration_s, rng)


_PATTERNS = {
    "burst": burst,
    "outage_recovery": outage_recovery,
    "poisson": poisson,
    "ramp": ramp,
    "periodic": periodic,
}


def arrival_times(pattern_type, n, duration_s, params=None, seed=None):
    """Dispatch to a named pattern. Returns a sorted numpy array of ``n`` times."""
    if pattern_type not in _PATTERNS:
        raise ValueError(
            f"unknown pattern {pattern_type!r}; choose from {sorted(_PATTERNS)}"
        )
    if n <= 0:
        return np.array([], dtype=np.float64)
    rng = np.random.default_rng(seed)
    return _PATTERNS[pattern_type](n, duration_s, rng=rng, **(params or {}))


def build_timeline(pattern_type, n, duration_s, params=None, seed=None, ids=None):
    """Arrival times paired with UE ids => list of (time_s, ue_id), time-sorted.

    ``ids`` lets the caller supply its own identifiers (e.g. layer-prefixed); by
    default UEs are numbered 0..n-1 in arrival order.
    """
    times = arrival_times(pattern_type, n, duration_s, params, seed)
    if ids is None:
        ids = range(len(times))
    return list(zip((float(t) for t in times), ids))
