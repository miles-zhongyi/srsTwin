# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""Unit tests for the hub_core per-UE channel models.

Asserts the impairments do what they claim AND that the verified baseline is
preserved: with no channel configured every op is the strict identity and the
UL superposition is byte-exact (so the 1-UE/2-UE link is untouched).
"""
import os
import sys

import numpy as np

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "hub")
sys.path.insert(0, HUB)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hub_core  # noqa: E402
import rf_profiles  # noqa: E402

N = 1152  # samples per test block


def _block(val=1 + 1j, n=N):
    return np.full(n, val, dtype=np.complex64).view(np.float32).tobytes()


def teardown():
    hub_core.configure({})


def test_unconfigured_is_identity():
    hub_core.configure({})
    blk = _block()
    assert hub_core.apply_dl(blk, 0) == blk
    arr = np.frombuffer(blk, dtype=np.float32)
    assert np.array_equal(hub_core.apply_ul(arr, 0), arr)


def test_ideal_profile_stays_identity():
    # 'ideal' has no impairments => configure() must drop it (identity fast-path)
    hub_core.configure({5: rf_profiles.get("ideal")})
    assert hub_core.channel_for(5) is None
    teardown()


def test_ul_gain_attenuates_power():
    hub_core.configure({0: {"ul_gain": 0.25, "fading": "none"}})
    arr = np.frombuffer(_block(), dtype=np.float32)
    out = hub_core.apply_ul(arr.copy(), 0).view(np.complex64)
    inp = arr.view(np.complex64)
    ratio = np.mean(np.abs(out)) / np.mean(np.abs(inp))
    assert abs(ratio - 0.25) < 1e-5, ratio
    teardown()


def test_ul_preserves_length():
    hub_core.configure({0: rf_profiles.get("edge")})
    arr = np.frombuffer(_block(), dtype=np.float32)
    out = hub_core.apply_ul(arr.copy(), 0)
    assert out.size == arr.size
    teardown()


def test_dl_adds_noise_to_hit_snr():
    hub_core.configure({0: {"dl_snr_db": 10.0, "fading": "none"}})
    blk = _block(val=1 + 0j, n=20000)
    out = np.frombuffer(hub_core.apply_dl(blk, 0), dtype=np.float32).view(np.complex64)
    sig = np.frombuffer(blk, dtype=np.float32).view(np.complex64)
    noise = out - sig
    snr = 10 * np.log10(np.mean(np.abs(sig) ** 2) / np.mean(np.abs(noise) ** 2))
    assert abs(snr - 10.0) < 1.5, f"measured DL SNR {snr:.1f} dB != 10 dB"
    assert len(out) == len(sig)
    teardown()


def test_dl_byte_length_preserved():
    hub_core.configure({0: rf_profiles.get("edge")})
    blk = _block()
    assert len(hub_core.apply_dl(blk, 0)) == len(blk)
    teardown()


def test_delay_line_shifts_and_carries():
    hub_core.configure({0: {"ul_gain": 1.0, "fading": "none", "delay_samples": 3}})
    ramp = np.arange(1, N + 1, dtype=np.float32)
    blk = (ramp + 1j * 0).astype(np.complex64).view(np.float32)
    out = hub_core.apply_ul(blk.copy(), 0).view(np.complex64)
    # first 3 samples come from the (zero) delay buffer; sample 3 == input[0]
    assert np.allclose(out[:3], 0)
    assert np.isclose(out[3].real, 1.0)
    teardown()


def test_sum_ul_exact_when_identity():
    hub_core.configure({})
    a = [1 + 1j, 2 - 2j, 3 + 0j]
    b = [4 + 0j, -1 + 1j, 5 + 5j]
    ia = np.array(a, dtype=np.complex64).view(np.float32).tobytes()
    ib = np.array(b, dtype=np.complex64).view(np.float32).tobytes()
    out = np.frombuffer(hub_core.sum_ul([(0, ia), (1, ib)], 0), dtype=np.float32).view(np.complex64)
    assert np.allclose(out, np.array(a) + np.array(b))


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    hub_core.configure({})
    print(f"\n{len(fns)} channel tests passed.")


if __name__ == "__main__":
    _run()
