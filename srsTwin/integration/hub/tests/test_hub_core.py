# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""Offline unit tests for hub_core: UL complex summation + identity ops."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub_core import apply_dl, apply_ul, sum_ul  # noqa: E402


def _iq(values):
    """complex list -> interleaved float32 bytes (srsRAN cf_t)."""
    return np.array(values, dtype=np.complex64).view(np.float32).tobytes()


def _to_complex(buf):
    return np.frombuffer(buf, dtype=np.float32).view(np.complex64)


def test_identity_ops():
    blk = _iq([1 + 2j, -3 + 4j])
    assert apply_dl(blk, 0) == blk
    arr = np.frombuffer(blk, dtype=np.float32)
    assert np.array_equal(apply_ul(arr, 0), arr)


def test_sum_two_ues():
    a = [1 + 1j, 2 - 2j, 0 + 3j]
    b = [4 + 0j, -1 + 1j, 5 + 5j]
    out = _to_complex(sum_ul([(0, _iq(a)), (1, _iq(b))], fill_len_bytes=0))
    assert np.allclose(out, np.array(a) + np.array(b))


def test_sum_single_ue_is_identity():
    a = [1 + 1j, 2 + 2j]
    out = sum_ul([(0, _iq(a))], fill_len_bytes=0)
    assert out == _iq(a)


def test_no_ue_is_silence():
    # 4 complex samples == 32 bytes of zeros
    out = sum_ul([], fill_len_bytes=32)
    assert out == b"\x00" * 32
    assert np.all(_to_complex(out) == 0)


def test_never_truncates_longer_ue_block():
    a = [1 + 1j] * 10
    out = sum_ul([(0, _iq(a))], fill_len_bytes=16)  # 16 bytes < 80 byte block
    assert out == _iq(a)


def test_sum_pads_to_fill_len():
    a = [1 + 1j, 2 + 2j]
    out = np.frombuffer(sum_ul([(0, _iq(a))], fill_len_bytes=64), dtype=np.float32).view(np.complex64)
    assert len(out) == 8  # 64 bytes / 8
    assert np.allclose(out[:2], np.array(a))
    assert np.allclose(out[2:], 0)


def test_unequal_lengths_zero_pad():
    a = [1 + 1j, 2 + 2j, 9 + 9j]   # 3 samples
    b = [4 + 4j]                    # 1 sample -> padded with silence
    out = _to_complex(sum_ul([(0, _iq(a)), (1, _iq(b))], fill_len_bytes=0))
    assert np.allclose(out, np.array([5 + 5j, 2 + 2j, 9 + 9j]))


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} core tests passed.")


if __name__ == "__main__":
    _run()
