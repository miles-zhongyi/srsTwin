# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Per-UE IQ sample operations for the srsTwin IQ hub.

srsRAN/ocudu carry baseband as interleaved complex float32 (`cf_t`: [re, im],
8 bytes per sample) over the ZeroMQ REQ/REP link. These helpers operate on that
exact byte layout and never change a block's length, so the hub's transport and
lockstep logic in iqhub.py is unaffected.

Channel models
--------------
Each UE slot may have a `Channel` registered (via `configure`). When it does:

  * `apply_ul` distorts that UE's UPLINK before summation — near-far amplitude
    (`ul_gain`), small-scale fading, residual CFO and a propagation delay. The
    near-far spread is what makes colliding PRACH preambles resolve by capture.
  * `apply_dl` degrades that UE's DOWNLINK copy by adding AWGN to hit a target
    SNR. Amplitude is left intact on purpose: the verified ZMQ link only decodes
    PBCH/PDSCH at balanced ~0 dB gains, so we model worse reception as added
    noise (lower SINR) rather than attenuation.

When NO channel is registered for a slot both ops are the strict IDENTITY — this
preserves the verified single-UE/2-UE baseline and the offline unit tests.
"""
from __future__ import annotations

import numpy as np

# srsRAN cf_t == two float32 (re, im).
CF32 = np.float32
DEFAULT_SRATE = 11.52e6

# slot -> Channel. Empty == every op is identity.
_CHANNELS: dict[int, "Channel"] = {}


def _db_to_lin_amp(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _db_to_lin_pow(db: float) -> float:
    return float(10.0 ** (db / 10.0))


class Channel:
    """Stateful per-UE channel. State (fading tap, CFO phase, delay line) carries
    across IQ blocks so impairments are continuous over the stream."""

    def __init__(self, *, ul_gain=1.0, dl_snr_db=0.0, fading="none",
                 k_factor_db=0.0, doppler_hz=0.0, cfo_hz=0.0, delay_samples=0,
                 srate=DEFAULT_SRATE, seed=None):
        self.ul_gain = float(ul_gain)
        self.dl_snr_db = float(dl_snr_db)
        self.fading = fading
        self.k_lin = _db_to_lin_pow(k_factor_db)
        self.doppler_hz = float(doppler_hz)
        self.cfo_hz = float(cfo_hz)
        self.delay_samples = int(delay_samples)
        self.srate = float(srate)
        self.rng = np.random.default_rng(seed)

        # running state
        self._tap = complex(1.0, 0.0)
        self._cfo_n = 0                      # samples elapsed (for CFO phase)
        self._delay_buf = np.zeros(self.delay_samples, dtype=np.complex64)

    # An all-default channel does nothing; let the hub skip it entirely.
    @property
    def is_identity(self) -> bool:
        return (self.ul_gain == 1.0 and self.fading == "none"
                and self.cfo_hz == 0.0 and self.delay_samples == 0
                and self.dl_snr_db == 0.0)

    # -- small-scale fading: one complex tap per block, AR(1) toward Doppler --
    def _next_tap(self, block_dt: float) -> complex:
        if self.fading == "none" or self.doppler_hz <= 0:
            # fixed unit-power tap (Rician LOS only / none)
            if self.fading == "rician":
                los = np.sqrt(self.k_lin / (self.k_lin + 1.0))
                return complex(los, 0.0) + np.sqrt(1.0 / (self.k_lin + 1.0)) * self._tap_diffuse_fixed()
            return complex(1.0, 0.0)
        rho = float(np.exp(-2.0 * np.pi * self.doppler_hz * block_dt))
        innov = (self.rng.standard_normal() + 1j * self.rng.standard_normal()) / np.sqrt(2.0)
        self._tap = rho * self._tap + np.sqrt(max(1.0 - rho * rho, 0.0)) * innov
        diffuse = self._tap
        if self.fading == "rician":
            los = np.sqrt(self.k_lin / (self.k_lin + 1.0))
            return complex(los, 0.0) + np.sqrt(1.0 / (self.k_lin + 1.0)) * diffuse
        return diffuse  # rayleigh

    def _tap_diffuse_fixed(self):
        if abs(self._tap) == 0:
            self._tap = (self.rng.standard_normal() + 1j * self.rng.standard_normal()) / np.sqrt(2.0)
        return self._tap

    def _apply_cfo(self, c: np.ndarray) -> np.ndarray:
        if self.cfo_hz == 0.0:
            return c
        n = np.arange(self._cfo_n, self._cfo_n + c.size)
        phase = np.exp(2j * np.pi * self.cfo_hz * n / self.srate).astype(np.complex64)
        self._cfo_n += c.size
        return c * phase

    def _apply_delay(self, c: np.ndarray) -> np.ndarray:
        """FIFO delay line: returns a same-length block delayed by delay_samples,
        carrying the tail across block boundaries (no wrap-around corruption)."""
        if self.delay_samples <= 0:
            return c
        joined = np.concatenate([self._delay_buf, c])
        out = joined[: c.size]
        self._delay_buf = joined[c.size:]
        return out.astype(np.complex64)

    def uplink(self, c: np.ndarray, block_dt: float) -> np.ndarray:
        c = self._apply_delay(c)
        tap = self._next_tap(block_dt)
        c = c * (self.ul_gain * tap)
        c = self._apply_cfo(c)
        return c.astype(np.complex64)

    def downlink(self, c: np.ndarray) -> np.ndarray:
        if self.dl_snr_db <= 0.0:
            return c
        sig_pow = float(np.mean(np.abs(c) ** 2)) if c.size else 0.0
        if sig_pow <= 0.0:
            return c
        noise_pow = sig_pow / _db_to_lin_pow(self.dl_snr_db)
        sigma = np.sqrt(noise_pow / 2.0)
        noise = (self.rng.standard_normal(c.size) + 1j * self.rng.standard_normal(c.size)) * sigma
        return (c + noise.astype(np.complex64)).astype(np.complex64)


def configure(channels: dict) -> None:
    """Register per-slot channels. ``channels`` maps slot index -> param dict
    (matching ``Channel`` kwargs) or a ready ``Channel``. Pass {} to reset to
    pure identity (the verified baseline)."""
    _CHANNELS.clear()
    for slot, spec in (channels or {}).items():
        ch = spec if isinstance(spec, Channel) else Channel(**spec)
        if not ch.is_identity:
            _CHANNELS[int(slot)] = ch


def channel_for(slot: int):
    return _CHANNELS.get(int(slot))


def apply_dl(block: bytes, slot: int) -> bytes:
    """Transform a downlink IQ block on its way to UE ``slot``.

    Identity unless a non-trivial channel is registered for the slot."""
    ch = _CHANNELS.get(int(slot))
    if ch is None:
        return block
    c = np.frombuffer(block, dtype=CF32).view(np.complex64)
    out = ch.downlink(c)
    return out.view(CF32).tobytes()


def apply_ul(samples: np.ndarray, slot: int) -> np.ndarray:
    """Transform an uplink IQ block from UE ``slot`` before summation.

    Identity unless a non-trivial channel is registered for the slot. Returns a
    same-length interleaved float32 array so ``sum_ul`` superposition is unchanged."""
    ch = _CHANNELS.get(int(slot))
    if ch is None:
        return samples
    c = np.ascontiguousarray(samples, dtype=CF32).view(np.complex64)
    block_dt = c.size / ch.srate if ch.srate else 0.0
    out = ch.uplink(c, block_dt)
    return out.view(CF32)


def _fit_block(data: bytes, nbytes: int, *, truncate: bool = True) -> bytes:
    """Return ``nbytes`` of IQ when ``truncate``; otherwise pad short blocks only."""
    if nbytes <= 0:
        return data
    if len(data) == nbytes:
        return data
    if len(data) < nbytes:
        return data + b"\x00" * (nbytes - len(data))
    if truncate:
        return data[:nbytes]
    return data


def sum_ul(blocks, fill_len_bytes: int) -> bytes:
    """
    Complex sum of uplink IQ blocks from the active UEs.

    ``blocks`` is a list of (slot, raw_bytes). Each UE's block is run through
    apply_ul() (channel model, or identity) and the results are summed
    element-wise. In lockstep all UEs return equal-length blocks; if they ever
    differ we zero-pad to the longest so a late/short block contributes silence on
    its tail rather than corrupting the sum or resizing the link.

    With no active UEs the uplink is silence: zeros of ``fill_len_bytes`` (sized to
    the matching downlink slot so the gNB RX sees the block size it expects).

    Active UE blocks are never truncated — only zero-padded when shorter than
    ``fill_len_bytes``. Truncating would clip PRACH/PUSCH energy at the tail and
    break attach; the gNB ZMQ RX buffer is large enough to accept the full block.
    """
    arrs = [(slot, np.frombuffer(b, dtype=CF32)) for slot, b in blocks]
    if not arrs:
        return _fit_block(b"", fill_len_bytes)
    processed = [(slot, apply_ul(a, slot)) for slot, a in arrs]
    length = max(a.size for _, a in processed)
    acc = np.zeros(length, dtype=CF32)
    for _slot, a in processed:
        acc[: a.size] += a
    return _fit_block(acc.tobytes(), fill_len_bytes, truncate=False)
