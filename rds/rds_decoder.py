"""
RDS (Radio Data System) decoder — numpy + stdlib only, zero I/O.

Decodes the 1187.5 bps RDS data subcarrier carried at 57 kHz on an FM
broadcast MPX signal into structured group records (PS station name, PI
code, RadioText, PTY, TP/TA, clock-time). RDS is public broadcast
metadata, EN 50067 / IEC 62106.

This module is the pure CORE: it takes complex IQ samples (or a raw MPX
signal) and returns a list of decoded group dicts. No sockets, no
ClickHouse — that lives in rds_reader.py and rds_ingest.py. Style model:
ais/ais_decoder.py (documented bit offsets, stdlib discipline, decoder
dispatch). The only dependency beyond stdlib is numpy, matching the host
baseline and spectrum/scanner.py.

DSP-for-audio-engineers notes:
  - The FM discriminator (angle of x[n]*conj(x[n-1])) is an instantaneous
    frequency estimator — the derivative of phase, exactly like tracking
    pitch as the rate-of-change of a phasor.
  - The 57 kHz RDS subcarrier is the 3rd harmonic of the 19 kHz stereo
    pilot; we regenerate a coherent 57 kHz reference by tripling the
    pilot's measured phase — same trick as a PLL frequency multiplier.
  - Biphase (Manchester) symbols are a mid-bit transition code; we recover
    each bit by integrating the first half-symbol minus the second half
    (a matched filter for the ± transition), just like edge detection.

Unlike spectrum/scanner.py (which does FFT + np.hanning and no convolution),
this module introduces the windowed-sinc / np.convolve FIR posture the D1
plan calls for — still numpy-only, no scipy anywhere.
"""

from datetime import datetime

import numpy as np

# ─── Constants ───────────────────────────────────────────

FS_DEFAULT = 228000          # rtl_tcp sample rate (valid RTL rate; integer ratios)
PILOT_HZ = 19000             # stereo pilot; fs/12
SUBCARRIER_HZ = 57000        # RDS subcarrier; fs/4 = 3 * pilot
BITRATE = 1187.5             # RDS bit rate; fs/192

# CRC-10 generator g(x) = x^10+x^8+x^7+x^5+x^4+x^3+1  (0b10110111001 = 0x5B9)
POLY = 0x5B9

# Offset words (EN 50067 Annex A), XORed into the 10-bit checkword per block.
OFFSET_WORDS = {
    "A": 0x0FC,    # 0011111100
    "B": 0x198,    # 0110011000
    "C": 0x168,    # 0101101000  (version A block 3)
    "Cp": 0x350,   # 1101010000  (C', version B block 3)
    "D": 0x1B4,    # 0110110100
}


# ─── CRC-10 / block sync ─────────────────────────────────

def crc10(info: int) -> int:
    """10-bit RDS checkword for a 16-bit info word.

    checkword = remainder of info(x) * x^10 mod g(x). Implemented as a
    26-bit shift-register polynomial division (pure ints, ais_decoder
    style — no lookup table needed for a one-word CRC).
    """
    reg = (info & 0xFFFF) << 10
    for p in range(25, 9, -1):
        if reg & (1 << p):
            reg ^= POLY << (p - 10)
    return reg & 0x3FF


def crc_remainder26(block: int) -> int:
    """Remainder of a full 26-bit block (16 info + 10 check) mod g(x)."""
    reg = block & 0x3FFFFFF
    for p in range(25, 9, -1):
        if reg & (1 << p):
            reg ^= POLY << (p - 10)
    return reg & 0x3FF


def block_ok(block26: int, offset_name: str) -> bool:
    """True iff a 26-bit block validates against the named offset word.

    Received block = info<<10 | (crc10(info) ^ offset). XORing the offset
    back into the low 10 bits reconstructs the clean codeword, whose
    remainder mod g(x) is zero.
    """
    return crc_remainder26(block26 ^ OFFSET_WORDS[offset_name]) == 0


# ─── FIR helpers (numpy-only, windowed-sinc) ─────────────

def sinc_lpf(cutoff_hz: float, fs: float, ntaps: int) -> np.ndarray:
    """Windowed-sinc low-pass FIR kernel (Hamming window), normalized to
    unity DC gain. Apply with np.convolve(x, h, mode='same'). This is the
    same posture spectrum/scanner.py describes but folds it into a real
    convolution kernel.
    """
    k = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = np.sinc(2.0 * cutoff_hz * k / fs) * np.hamming(ntaps)
    h /= h.sum()
    return h.astype(np.float64)


def fm_discriminate(iq: np.ndarray) -> np.ndarray:
    """WFM discriminator: instantaneous frequency via the phase of
    x[n]*conj(x[n-1]). Equivalent to diff(unwrap(angle(x))) but immune to
    unwrap glitches. Output scale is irrelevant downstream. Returns an
    array one sample shorter than the input.
    """
    return np.angle(iq[1:] * np.conj(iq[:-1]))


# ─── MJD → calendar (EN 50067 Annex D arithmetic) ────────

def mjd_to_ymd(mjd: int) -> tuple[int, int, int]:
    yp = int((mjd - 15078.2) / 365.25)
    mp = int((mjd - 14956.1 - int(yp * 365.25)) / 30.6001)
    d = mjd - 14956 - int(yp * 365.25) - int(mp * 30.6001)
    k = 1 if mp in (14, 15) else 0
    year = yp + k + 1900
    month = mp - 1 - k * 12
    return year, month, d


# ─── Character decoding ──────────────────────────────────

def _rds_char(code: int) -> str:
    """RDS PS/RT character → printable ASCII. The full EN 50067 Annex E
    charset table is deliberately skipped (a known simplification): Greek
    stations mostly transmit basic ASCII, and non-printable codes map to a
    space so a garbled byte never corrupts the display string.
    """
    if 0x20 <= code <= 0x7E:
        return chr(code)
    return " "


# ─── Group assembler ─────────────────────────────────────

class GroupAssembler:
    """Turns validated 4-block groups into decoded record dicts, keeping
    the PS (8-char) and RadioText (64-char) segment buffers and the last
    PI. Emits exactly one dict per fully-valid group. Mirrors the
    acars.flight_latest / spectrum accumulation shape.
    """

    def __init__(self):
        self.pi = None
        self._reset_ps()
        self._reset_rt()

    def _reset_ps(self):
        self.ps = [0x20] * 8
        self.ps_seen = 0

    def _reset_rt(self):
        self.rt = [0x20] * 64
        self.rt_seen = 0
        self.rt_ab = None

    def _ps_string(self) -> str:
        return "".join(_rds_char(c) for c in self.ps).rstrip()

    def _rt_string(self) -> str:
        # RadioText terminates at 0x0D (carriage return).
        text = self.rt
        if 0x0D in text:
            text = text[: text.index(0x0D)]
        return "".join(_rds_char(c) for c in text).rstrip()

    def _clock(self, mjd: int, hour: int, minute: int) -> str | None:
        if hour > 23 or minute > 59 or mjd < 1:
            return None
        try:
            y, m, d = mjd_to_ymd(mjd)
            return datetime(y, m, d, hour, minute, 0).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError):
            return None

    def process_group(self, names: list[str], infos: list[int], block_errors: int) -> dict:
        """names: block offset names A,B,C|Cp,D. infos: the four 16-bit
        info words. Returns the decoded record for this group.
        """
        pi = infos[0]
        b = infos[1]
        c = infos[2]
        d = infos[3]

        # Block B layout (present in every synced group):
        #   bits 15-12 group type, bit 11 version (0=A,1=B),
        #   bit 10 TP, bits 9-5 PTY, bits 4-0 group-specific.
        gtype = (b >> 12) & 0xF
        ver = (b >> 11) & 1
        group_type = f"{gtype}{'B' if ver else 'A'}"
        tp = (b >> 10) & 1
        pty = (b >> 5) & 0x1F

        if self.pi != pi:
            self.pi = pi
            self._reset_ps()
            self._reset_rt()

        ta = 0
        ms = 0
        ps_out = ""
        rt_out = ""
        clock_utc = None
        clock_off = 0

        if gtype == 0:
            # 0A/0B — Programme Service name.
            ta = (b >> 4) & 1
            ms = (b >> 3) & 1
            seg = b & 0x3
            self.ps[2 * seg] = (d >> 8) & 0xFF
            self.ps[2 * seg + 1] = d & 0xFF
            self.ps_seen |= (1 << seg)
            if self.ps_seen == 0xF:
                ps_out = self._ps_string()
        elif gtype == 2:
            # 2A/2B — RadioText.
            ab = (b >> 4) & 1
            if ab != self.rt_ab:
                self.rt_ab = ab
                self.rt = [0x20] * 64
                self.rt_seen = 0
            seg = b & 0xF
            if ver == 0:  # 2A: 4 chars from blocks C and D
                chars = [(c >> 8) & 0xFF, c & 0xFF, (d >> 8) & 0xFF, d & 0xFF]
                base = 4 * seg
            else:         # 2B: 2 chars from block D (block C repeats PI)
                chars = [(d >> 8) & 0xFF, d & 0xFF]
                base = 2 * seg
            for i, ch in enumerate(chars):
                if base + i < 64:
                    self.rt[base + i] = ch
            self.rt_seen |= (1 << seg)
            rt_out = self._rt_string()
        elif gtype == 4 and ver == 0:
            # 4A — clock-time and date.
            mjd = ((b & 0x3) << 15) | ((c >> 1) & 0x7FFF)
            hour = ((c & 1) << 4) | ((d >> 12) & 0xF)
            minute = (d >> 6) & 0x3F
            sign = (d >> 5) & 1
            off = d & 0x1F
            clock_off = (-1 if sign else 1) * off * 30
            clock_utc = self._clock(mjd, hour, minute)

        return {
            "pi": pi,
            "group_type": group_type,
            "tp": tp,
            "pty": pty,
            "ta": ta,
            "ms": ms,
            "ps": ps_out,
            "radiotext": rt_out,
            "clock_utc": clock_utc,
            "clock_offset_min": clock_off,
            "block_errors": block_errors,
            "raw_group": " ".join(f"{x:04X}" for x in infos),
        }


# ─── Block framer / sync tracker ─────────────────────────

class Framer:
    """Consumes a stream of differentially-decoded data bits, acquires
    block sync via the offset words, and hands validated groups to a
    GroupAssembler. Sliding 26-bit window; acquire on two blocks exactly
    26 bits apart validating as A then B; then step 26 bits per block
    expecting A,B,C|C',D cyclically. 8 consecutive bad blocks drop sync.
    """

    SEQ = ["A", "B", "C", "D"]

    def __init__(self, assembler: GroupAssembler):
        self.a = assembler
        self.reg = 0
        self.nbits_seen = 0
        self.synced = False
        self.pending = []      # candidate A positions: [countdown, a_reg]
        self.expect = 0        # index into SEQ of the next expected block
        self.since = 0         # bits since the last block boundary
        self.blocks = []       # (offset_name, info16, valid) for current group
        self.bad_run = 0
        self.err_since_emit = 0

    def push_bit(self, bit: int) -> list[dict]:
        self.reg = ((self.reg << 1) | (bit & 1)) & 0x3FFFFFF
        if self.nbits_seen < 26:
            self.nbits_seen += 1
        if not self.synced:
            self._search()
            return []
        rec = self._track()
        return [rec] if rec is not None else []

    def _search(self):
        if self.nbits_seen < 26:
            return
        newp = []
        for item in self.pending:
            item[0] -= 1
            if item[0] == 0:
                if block_ok(self.reg, "B"):
                    self._acquire(item[1], self.reg)
                    self.pending = []
                    return
            else:
                newp.append(item)
        self.pending = newp
        if block_ok(self.reg, "A"):
            self.pending.append([26, self.reg])

    def _acquire(self, a_reg: int, b_reg: int):
        self.synced = True
        self.blocks = [("A", a_reg >> 10, True), ("B", b_reg >> 10, True)]
        self.expect = 2
        self.since = 0
        self.bad_run = 0
        self.err_since_emit = 0

    def _drop(self):
        self.synced = False
        self.pending = []
        self.blocks = []
        self.expect = 0
        self.since = 0
        self.bad_run = 0
        self.err_since_emit = 0
        self.nbits_seen = 26   # window already full; keep sliding

    def _track(self):
        self.since += 1
        if self.since < 26:
            return None
        self.since = 0

        name = self.SEQ[self.expect]
        block = self.reg
        info = block >> 10

        if name == "C":
            if block_ok(block, "C"):
                valid, used = True, "C"
            elif block_ok(block, "Cp"):
                valid, used = True, "Cp"
            else:
                valid, used = False, "C"
        else:
            valid, used = block_ok(block, name), name

        self.blocks.append((used, info, valid))

        if valid:
            self.bad_run = 0
        else:
            self.bad_run += 1
            self.err_since_emit = min(255, self.err_since_emit + 1)
            if self.bad_run >= 8:
                self._drop()
                return None

        self.expect += 1
        if self.expect == 4:
            rec = self._complete()
            self.expect = 0
            self.blocks = []
            return rec
        return None

    def _complete(self):
        if all(v for (_, _, v) in self.blocks):
            errs = self.err_since_emit
            self.err_since_emit = 0
            names = [x[0] for x in self.blocks]
            infos = [x[1] for x in self.blocks]
            return self.a.process_group(names, infos, errs)
        return None


# ─── Streaming demodulator ───────────────────────────────

class RDSDemodulator:
    """Streaming RDS demodulator: feed IQ chunks to process(), get a list
    of decoded group dicts back. State (filter warmup history, absolute
    sample phase for the coherent mixers, biphase clock phase, differential
    carry bit, framer/assembler) persists across chunks so groups that
    straddle a chunk boundary decode without loss.
    """

    NTAPS_DATA = 501    # 57 kHz baseband LPF, transition ~1.5 kHz
    NTAPS_PILOT = 257   # 19 kHz pilot LPF, cutoff 1 kHz

    def __init__(self, fs: int = FS_DEFAULT, pilot_thresh: float = 0.0):
        self.fs = fs
        self.samples_per_bit = int(round(fs / BITRATE))   # 192 at 228 kHz
        self.half_bit = self.samples_per_bit // 2         # 96
        self.h_pilot = sinc_lpf(1000.0, fs, self.NTAPS_PILOT)
        self.h_data = sinc_lpf(2400.0, fs, self.NTAPS_DATA)
        self.pilot_thresh = pilot_thresh
        self.guard = self.NTAPS_DATA // 2                 # dropped filter transient

        self.assembler = GroupAssembler()
        self.framer = Framer(self.assembler)

        self._iq_prev = None
        self._hist = np.zeros(0, dtype=np.float64)   # mpx warmup context
        self._n0 = 0                                 # true mpx samples seen
        self._committed = 0                          # mpx samples turned into soft output
        self._softbuf = np.zeros(0, dtype=np.float64)
        self._buf_abs = 0                            # true index of _softbuf[0]
        self._bit_phase = None                       # abs index ≡ phase (mod samples_per_bit)
        self._carry = None                           # last hard bit for differential decode

        self._w19 = 2.0 * np.pi * PILOT_HZ / fs
        self._w57 = 2.0 * np.pi * SUBCARRIER_HZ / fs

    def process(self, iq_chunk) -> list[dict]:
        iq = np.asarray(iq_chunk, dtype=np.complex128)
        if iq.size < 2:
            return []

        # WFM discriminator with cross-chunk continuity (one previous sample).
        prev = self._iq_prev if self._iq_prev is not None else iq[0]
        ext = np.concatenate(([prev], iq))
        mpx_new = fm_discriminate(ext)     # length == iq.size
        self._iq_prev = iq[-1]

        mpx_full = np.concatenate((self._hist, mpx_new))
        H = self._hist.size
        M = mpx_new.size
        half = self.guard

        if mpx_full.size <= 2 * half + self.samples_per_bit:
            # Too short to filter safely — accumulate and wait.
            self._hist = mpx_full
            self._n0 += M
            return []

        # Absolute (true) sample index of each mpx_full sample; keeps the
        # 19/57 kHz mixers phase-continuous across chunks.
        n_abs = np.arange(mpx_full.size, dtype=np.float64) + (self._n0 - H)

        # (1) Pilot recovery: mix to baseband, LPF, measure instantaneous phase.
        p = mpx_full * np.exp(-1j * self._w19 * n_abs)
        p_lp = np.convolve(p, self.h_pilot, mode="same")
        phi = np.angle(p_lp)

        interior = slice(half, mpx_full.size - half)
        if np.median(np.abs(p_lp[interior])) < self.pilot_thresh:
            # No stereo pilot → no coherent 57 kHz reference (mono station,
            # or the FM notch is still installed). Advance state, emit nothing.
            self._advance(mpx_full, M, half)
            return []

        # (2) Coherent 57 kHz mix (carrier tripled from the pilot phase) + LPF.
        r = mpx_full * np.exp(-1j * (self._w57 * n_abs + 3.0 * phi))
        r_lp = np.convolve(r, self.h_data, mode="same")

        # (3) Residual BPSK phase (0°/90° subcarrier lock ambiguity); the
        # leftover sign is killed by differential decode downstream.
        psi = 0.5 * np.angle(np.mean(r_lp[interior] * r_lp[interior]))
        b = np.real(r_lp * np.exp(-1j * psi))

        # Commit the newly-valid soft samples (drop the right filter transient,
        # which becomes warmup context for the next chunk). commit_start picks
        # up the half-bit region deferred by the previous chunk.
        deferred = self._n0 - self._committed
        commit_start = H - deferred
        commit_end = mpx_full.size - half
        if commit_end > commit_start:
            csoft = b[commit_start:commit_end]
            if self._softbuf.size == 0:
                self._buf_abs = self._committed
            self._softbuf = np.concatenate((self._softbuf, csoft))
            self._committed += csoft.size

        self._advance(mpx_full, M, half)

        return self._extract_bits()

    def _advance(self, mpx_full, M, half):
        keep = self.NTAPS_DATA - 1
        self._hist = mpx_full[-keep:] if mpx_full.size >= keep else mpx_full
        self._n0 += M

    def _extract_bits(self) -> list[dict]:
        spb = self.samples_per_bit
        hb = self.half_bit
        buf = self._softbuf

        # Determine the biphase sample phase: pick the offset whose
        # first-half-minus-second-half energy is maximal. Re-derived whenever
        # the framer drops sync (below), so a slow tuner-clock (ppm) slip
        # re-acquires on the next chunk instead of persisting as decode loss.
        if self._bit_phase is None:
            if buf.size < spb * 4:
                return []
            best_o, best_score = 0, -1.0
            for o in range(spb):
                n = (buf.size - o) // spb
                if n < 2:
                    continue
                mat = buf[o:o + n * spb].reshape(n, spb)
                score = np.abs(mat[:, :hb].sum(1) - mat[:, hb:].sum(1)).sum() / n
                if score > best_score:
                    best_score, best_o = score, o
            self._bit_phase = (self._buf_abs + best_o) % spb

        first = (self._bit_phase - self._buf_abs) % spb
        nbits = (buf.size - first) // spb
        out: list[dict] = []
        if nbits > 0:
            mat = buf[first:first + nbits * spb].reshape(nbits, spb)
            soft = mat[:, :hb].sum(1) - mat[:, hb:].sum(1)
            hard = (soft > 0).astype(np.int8)
            prev = self._carry
            was_synced = self.framer.synced
            for hv in hard:
                hv = int(hv)
                if prev is None:
                    prev = hv
                    continue
                data_bit = hv ^ prev
                prev = hv
                out.extend(self.framer.push_bit(data_bit))
            self._carry = prev

            # If the framer held sync on entry but lost it here, a sample-clock
            # (ppm) slip is the likely cause. Clear the sample phase so the next
            # chunk re-searches it, instead of waiting to slide a full bit
            # through the matched filter before re-locking (D1 verify W1).
            if was_synced and not self.framer.synced:
                self._bit_phase = None
                self._carry = None

            consumed = first + nbits * spb
            self._softbuf = buf[consumed:]
            self._buf_abs += consumed
        return out


# ─── Convenience one-shot ────────────────────────────────

def decode_iq(iq, fs: int = FS_DEFAULT, pilot_thresh: float = 0.0) -> list[dict]:
    """Decode a whole IQ array at once (offline convenience). Live/streaming
    callers should hold an RDSDemodulator and feed chunks to process().
    """
    demod = RDSDemodulator(fs=fs, pilot_thresh=pilot_thresh)
    return demod.process(np.asarray(iq))
