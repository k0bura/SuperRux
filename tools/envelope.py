#!/usr/bin/env python3
"""Compute a jaw envelope from speech audio and optionally play it on the bear.

Tier 1 of the audio->jaw_state mapping: an amplitude envelope low-passed to the
mechanism's measured 2 Hz corner. Nothing here tries to model phonemes -- the
jaw renders ~5% of a 10 Hz command, so phoneme detail is filtered out by the
mechanism no matter how it is computed. See PLAN.md.

    tools/envelope.py speech.wav --csv env.csv
    tools/envelope.py speech.wav --send /dev/ttyUSB0
"""
import argparse
import sys
import time

import numpy as np
import soundfile as sf

FRAME_MS   = 20      # must match the firmware's FRAME_MS
# Not the jaw's 2 Hz corner: the mechanism low-passes regardless, so filtering
# to 2 Hz in software too double-filters and costs the inter-phrase closure that
# reads as speech. Measured on hardware, 5 Hz gets *more* excursion (1550 vs
# 1501 counts p-p) and 3x the closure (16% vs 5%) for ~20ms more lag.
CORNER_HZ  = 5.0
MAX_FRAMES = 1500    # firmware buffer

# The jaw cannot reach its stops at speech rates, and commanding travel it
# cannot make just saturates the loop. Keep the command inside what it can do.
LOWER_MAX  = 55      # percent of usable range at full envelope
USCALE     = 20      # upper's commanded swing as % of lower's; ~40% visually

# Eyes. Amplitude-driven eyes look wrong -- eyes follow meaning and rhythm, not
# loudness -- so these are placed on phrase structure, never on level. Without
# the text we cannot know a question from a statement, so the bench track does
# the part that needs no semantics: blink in the gaps between phrases, and keep
# an idle rate up when the gaps are sparse.
EYES_NEUTRAL = 65
EYES_SHUT    = 5
BLINK_MS     = 340   # each way; the eyes slew ~4.06 counts/ms at duty 255
IDLE_BLINK_S = 4.0   # ~15/min, the low end of natural


def eye_track(lower, seed=0):
    """Eye percentages per frame, from the jaw envelope's phrase structure."""
    nf = len(lower)
    fps = 1000.0 / FRAME_MS
    eyes = np.full(nf, EYES_NEUTRAL, dtype=float)

    # Gaps: runs where the mouth is shut. Blink inside one and it reads as
    # punctuation; blink mid-word and it reads as a twitch.
    shut = lower == 0
    gaps, run = [], None
    for i, v in enumerate(shut):
        if v and run is None:
            run = i
        elif not v and run is not None:
            gaps.append((run, i)); run = None
    if run is not None:
        gaps.append((run, nf))

    half = int(BLINK_MS / FRAME_MS)
    at = []
    for a, b in gaps:
        if (b - a) >= half:                      # long enough to hide a blink
            at.append(a + (b - a) // 2)

    # Top up to a natural idle rate if the phrasing did not supply enough.
    rng = np.random.default_rng(seed)
    want = int(nf / fps / IDLE_BLINK_S)
    guard = int(1.5 * fps)
    tries = 0
    while len(at) < want and tries < 200:
        tries += 1
        c = int(rng.integers(half, max(half + 1, nf - half)))
        if all(abs(c - x) > guard for x in at):
            at.append(c)

    for c in at:
        for k in range(-half, half + 1):
            i = c + k
            if 0 <= i < nf:
                # linear down and back up; the mechanism rounds the corners
                f = 1.0 - abs(k) / float(half)
                v = EYES_NEUTRAL + (EYES_SHUT - EYES_NEUTRAL) * f
                eyes[i] = min(eyes[i], v)

    return np.round(eyes).astype(int), len(at)


def envelope(path, range_db=35.0, corner=CORNER_HZ, floor_db=-60.0):
    x, sr = sf.read(path, dtype='float32', always_2d=True)
    x = x.mean(axis=1)                       # mono

    n = int(sr * FRAME_MS / 1000.0)
    nf = len(x) // n
    if nf == 0:
        sys.exit('audio shorter than one %dms frame' % FRAME_MS)
    frames = x[:nf * n].reshape(nf, n)

    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)) + 1e-12
    db = 20 * np.log10(rms)

    # Normalise against a high percentile, not the peak: one transient should
    # not squash the whole utterance. The floor is relative to that reference,
    # not an absolute dBFS -- an absolute gate never fires on material with
    # anything under it, and the mouth then hangs open for the whole clip.
    ref  = np.percentile(db, 95)
    base = max(ref - range_db, floor_db)
    norm = np.clip((db - base) / max(ref - base, 1e-6), 0.0, 1.0)
    norm[db < base] = 0.0                    # hard gate: the mouth must shut

    # One-pole at the mechanism's corner, with a faster attack than release --
    # mouths open quickly and close slowly, and the gate above is what actually
    # shuts it between phrases.
    fs = 1000.0 / FRAME_MS
    a_rel = 1.0 - np.exp(-2 * np.pi * corner / fs)
    a_att = min(1.0, a_rel * 3.0)
    out = np.zeros_like(norm)
    y = 0.0
    for i, v in enumerate(norm):
        y += (a_att if v > y else a_rel) * (v - y)
        out[i] = y

    lower = np.round(out * LOWER_MAX).astype(int)
    upper = np.round(out * LOWER_MAX * USCALE / 100.0).astype(int)
    eyes, nblink = eye_track(lower)
    return upper, lower, eyes, nf, nblink


def _decode_to_wav(src):
    """aplay only speaks WAV, and the TTS arrives as MP3."""
    import tempfile
    x, sr = sf.read(src, dtype='float32', always_2d=True)
    fh = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    fh.close()
    sf.write(fh.name, x, sr, subtype='PCM_16')
    return fh.name


def send(port, upper, lower, eyes, audio=None, lead_ms=120):
    import serial
    if len(lower) > MAX_FRAMES:
        print('  truncating %d -> %d frames (firmware buffer)'
              % (len(lower), MAX_FRAMES))
        upper = upper[:MAX_FRAMES]
        lower = lower[:MAX_FRAMES]
        eyes  = eyes[:MAX_FRAMES]

    s = serial.Serial(port, 115200, timeout=0.3)

    def wait_for(token, timeout, what):
        """Read until token appears. Never blast data at a board that has not
        said it is listening -- opening the port resets it, and anything sent
        during boot is parsed as console commands instead of payload."""
        buf, t0 = '', time.time()
        while time.time() - t0 < timeout:
            d = s.read(4096)
            if d:
                buf += d.decode('utf-8', 'replace')
                if token in buf:
                    return buf
        sys.exit('  timed out waiting for %s (got %r)' % (what, buf[-200:]))

    time.sleep(2.5)                          # opening the port resets the board
    s.reset_input_buffer()
    s.write(b'\n')
    wait_for('>', 10, 'the console prompt')

    s.write(b'envload %d\n' % len(lower))
    wait_for('send', 10, 'the envload acknowledgement')

    # Chunked, so the board's RX buffer is never asked to hold more than it can
    # while it is echoing or parsing.
    for i in range(0, len(lower), 50):
        blk = ''.join('%d,%d,%d\n' % (u, l, e)
                      for u, l, e in zip(upper[i:i + 50], lower[i:i + 50],
                                         eyes[i:i + 50]))
        s.write(blk.encode())
        s.flush()
        time.sleep(0.03)
    out = wait_for('loaded', 20, 'the load confirmation')
    sys.stdout.write('  ' + out.strip().split('\n')[-1].strip() + '\n')

    # The jaw lags its command by ~120ms (dead time plus the time the loop needs
    # to close), so command it that far ahead of the audio or the mouth visibly
    # trails the voice. --lead makes it tunable by eye.
    proc = None
    if audio:
        import subprocess
        wav = _decode_to_wav(audio)
        s.write(b'envplay 1\n')
        time.sleep(max(0.0, lead_ms / 1000.0))
        proc = subprocess.Popen(['aplay', '-q', wav],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        print('  playing audio, jaw leading by %dms' % lead_ms)
    else:
        s.write(b'envplay 1\n')

    t0, last, buf = time.time(), time.time(), []
    while time.time() - t0 < 90:
        d = s.read(4096)
        if d:
            buf.append(d.decode('utf-8', 'replace'))
            last = time.time()
        elif time.time() - last > 6:
            break
    sys.stdout.write(''.join(buf))
    if proc:
        proc.wait()
    s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('audio')
    ap.add_argument('--csv')
    ap.add_argument('--send', metavar='PORT')
    ap.add_argument('--play', metavar='PORT',
                    help='like --send, but also play the audio through the '
                         'desktop so the sync can be judged by eye')
    ap.add_argument('--lead', type=int, default=120, metavar='MS',
                    help='how far ahead of the audio the jaw is commanded')
    ap.add_argument('--range', dest='range_db', type=float, default=35.0,
                    help='dB below the 95th percentile that counts as silence')
    ap.add_argument('--corner', type=float, default=CORNER_HZ)
    a = ap.parse_args()

    upper, lower, eyes, nf, nblink = envelope(a.audio, a.range_db, a.corner)
    print('  %d frames (%.2fs)  lower %d-%d%%  shut %.0f%% of frames'
          % (nf, nf * FRAME_MS / 1000.0, lower.min(), lower.max(),
             100.0 * (lower == 0).mean()))
    print('  %d blinks (%.1f/min)'
          % (nblink, nblink / (nf * FRAME_MS / 1000.0) * 60))

    if a.csv:
        with open(a.csv, 'w') as fh:
            fh.write('upper_pct,lower_pct,eyes_pct\n')
            for u, l, e in zip(upper, lower, eyes):
                fh.write('%d,%d,%d\n' % (u, l, e))
        print('  wrote %s' % a.csv)

    if a.send:
        send(a.send, upper, lower, eyes)
    if a.play:
        send(a.play, upper, lower, eyes, audio=a.audio, lead_ms=a.lead)


if __name__ == '__main__':
    main()
