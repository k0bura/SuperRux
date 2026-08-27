"""Shared helpers for decoding the original Teddy Ruxpin cassette control track.

The right channel of the story tapes carries pulse-position servo data at
~58 Hz. Tape level drifts, so everything here works on an AGC-normalised
signal rather than raw amplitude.
"""
import numpy as np
import soundfile as sf


def load(path):
    x, sr = sf.read(path, dtype='int16')
    return x[:, 0].astype(np.float32), x[:, 1].astype(np.float32), sr


def envelope(sig, sr, ms):
    """Moving-average of |sig| over `ms`, same length as sig.

    cumsum rather than convolve: the AGC window is thousands of taps against
    tens of millions of samples, and the O(n*k) form does not finish.
    """
    n = max(1, int(sr * ms / 1000.0))
    c = np.cumsum(np.abs(sig, dtype=np.float64))
    c = np.concatenate(([0.0], c))
    lo = np.clip(np.arange(len(sig)) - n // 2, 0, len(sig))
    hi = np.clip(lo + n, 0, len(sig))
    return ((c[hi] - c[lo]) / np.maximum(hi - lo, 1)).astype(np.float32)


def agc(sig, sr, ms=200.0, floor_frac=0.05):
    """Normalise by a slow envelope so a fixed threshold means the same thing
    everywhere on the tape. Guarded so silence does not blow up to full scale."""
    e = envelope(sig, sr, ms)
    floor = float(e.max()) * floor_frac
    return sig / np.maximum(e, max(floor, 1e-6))


def zero_crossings(sig):
    """Indices i where sign(sig[i]) != sign(sig[i+1])."""
    s = np.signbit(sig)
    return np.flatnonzero(np.diff(s))


FRAME_MS = 17.2          # measured; see PLAN.md
FRAME_MIN_MS = 13.0      # accept this much drift before calling it a dropout
FRAME_MAX_MS = 22.0


def find_sync(sig, sr, thresh=2.2, burst_ms=0.4, env=None):
    """Sync-burst onsets in an AGC-normalised control signal.

    The burst is the one part of a frame that clearly exceeds the body, so
    threshold the short envelope and take each rising edge, suppressing
    re-triggers within a frame. Pass `env` to reuse one envelope across a
    threshold sweep.
    """
    e = envelope(sig, sr, burst_ms) if env is None else env
    above = (e > thresh).astype(np.int8)
    on = np.flatnonzero(np.diff(above) == 1)
    if len(on) == 0:
        return on
    guard = int(sr * FRAME_MIN_MS / 1000.0)
    keep = [on[0]]
    for i in on[1:]:
        if i - keep[-1] > guard:
            keep.append(i)
    return np.array(keep)


def frame_lock_score(sync, sr, tol=0.08):
    """How tightly the sync intervals cluster on one period.

    Deliberately *not* "fraction of intervals inside [FRAME_MIN, FRAME_MAX]":
    the re-trigger guard already forces every interval above FRAME_MIN, so that
    version scores a detector that simply fires the moment the guard expires as
    a perfect lock. Measured tightness around the median cannot be gamed that
    way -- a guard-limited train is spread, not clustered.
    """
    if len(sync) < 8:
        return 0.0
    d = np.diff(sync) / sr * 1000.0
    med = float(np.median(d))
    if not (FRAME_MIN_MS <= med <= FRAME_MAX_MS):
        return 0.0
    return float((np.abs(d - med) <= tol * med).mean())


def find_sync_auto(sig, sr, thresholds=None, cal_windows=3, cal_s=20.0):
    """find_sync with the burst threshold chosen per file.

    Tape-to-tape balance between the sync burst and the frame body varies
    enough that one fixed threshold either misses syncs on some tapes or
    double-triggers on others. Calibrate the threshold on a few short windows
    spread through the file, then make a single full pass with the winner --
    sweeping the whole file per threshold is far too slow to be worth it.
    """
    if thresholds is None:
        thresholds = np.arange(1.6, 4.01, 0.1)
    env = envelope(sig, sr, 0.4)

    n = int(sr * cal_s)
    starts = np.linspace(0, max(0, len(sig) - n), cal_windows).astype(int)
    best_thr, best_score = None, -1.0
    for t in thresholds:
        scores = []
        for a in starts:
            w = env[a:a + n]
            scores.append(frame_lock_score(find_sync(None, sr, float(t), env=w), sr))
        sc = float(np.mean(scores))
        if sc > best_score:
            best_thr, best_score = float(t), sc

    return find_sync(None, sr, best_thr, env=env), best_thr, best_score


def block_reduce(sig, factor):
    """Mean of |sig| over consecutive blocks. Decimating before any long
    cumsum keeps the analysis passes off the multi-hundred-MB float64 path."""
    n = (len(sig) // factor) * factor
    return np.abs(sig[:n]).reshape(-1, factor).mean(axis=1)


def detrend(x, n):
    """Subtract an n-sample moving average -- drops slow common drift that
    otherwise dominates a cross-correlation and pushes the peak to the edge
    of the lag search."""
    c = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    lo = np.clip(np.arange(len(x)) - n // 2, 0, len(x))
    hi = np.clip(lo + n, 0, len(x))
    return x - ((c[hi] - c[lo]) / np.maximum(hi - lo, 1))


def best_lag(a, b, max_lag):
    """Peak |correlation| of a against b over +/-max_lag samples."""
    guard = max_lag + 1
    bb = b[guard:len(b) - guard]
    bb = (bb - bb.mean()) / (bb.std() + 1e-12)
    best = (0, 0.0)
    for lag in range(-max_lag, max_lag + 1):
        aa = a[guard + lag:len(a) - guard + lag]
        aa = (aa - aa.mean()) / (aa.std() + 1e-12)
        r = float((aa * bb).mean())
        if abs(r) > abs(best[1]):
            best = (lag, r)
    return best
