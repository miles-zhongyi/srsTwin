# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""Unit tests for the arrival-pattern engine (no Docker / ZMQ)."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import patterns  # noqa: E402

DUR = 120.0
N = 400


def _times(kind, **params):
    return patterns.arrival_times(kind, N, DUR, params, seed=7)


def test_count_sorted_in_range():
    for kind in ["burst", "outage_recovery", "poisson", "ramp", "periodic"]:
        t = _times(kind)
        assert len(t) == N, kind
        assert np.all(np.diff(t) >= 0), f"{kind} not sorted"
        assert t.min() >= 0 and t.max() <= DUR, f"{kind} out of range"


def test_zero_arrivals_empty():
    assert patterns.arrival_times("poisson", 0, DUR).size == 0


def test_unknown_pattern_raises():
    try:
        patterns.arrival_times("nope", 5, DUR)
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown pattern")


def test_burst_concentrated_in_window():
    t = _times("burst", start_s=30.0, window_s=5.0)
    inside = np.sum((t >= 29.0) & (t <= 36.0))
    assert inside >= 0.95 * N, f"burst leaked outside window: {inside}/{N}"


def test_outage_decays():
    # exponential decay => far more arrivals early than late
    t = _times("outage_recovery", start_s=0.0, tau_s=10.0)
    first = np.sum(t < DUR / 4)
    last = np.sum(t > 3 * DUR / 4)
    assert first > 5 * last, f"outage not front-loaded: {first} vs {last}"


def test_poisson_roughly_uniform():
    t = _times("poisson")
    # mean of uniform[0,DUR] ~ DUR/2; allow generous slack
    assert abs(t.mean() - DUR / 2) < DUR * 0.1


def test_ramp_increasing_density():
    t = _times("ramp", rate_start=0.1, rate_end=1.0)
    first = np.sum(t < DUR / 2)
    second = np.sum(t >= DUR / 2)
    assert second > first, f"ramp not increasing: {first} vs {second}"


def test_periodic_count_and_spread():
    t = _times("periodic", period_s=30.0, amplitude=0.8)
    assert len(t) == N
    assert t.std() > 10.0  # genuinely spread, not collapsed


def test_reproducible_seed():
    a = patterns.arrival_times("poisson", 50, DUR, seed=42)
    b = patterns.arrival_times("poisson", 50, DUR, seed=42)
    assert np.array_equal(a, b)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} pattern tests passed.")


if __name__ == "__main__":
    _run()
