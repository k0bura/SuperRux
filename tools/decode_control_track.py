#!/usr/bin/env python3
"""Decode the physical layer of the original Teddy Ruxpin cassette control track.

The right channel of the story tapes carries a framed pulse signal at ~56 Hz.
This recovers the frame structure and the six variable data intervals in each
frame, and writes them as CSV. It does *not* interpret them as servo positions
-- see PLAN.md for what is and is not established about the payload.

    ./decode_control_track.py story.flac -o story.csv
"""
import argparse
import csv
import os
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ctrl

# Frame layout, measured. Cols 0-3 are the sync burst's own zero crossings;
# from col 4 a fixed ~0.4 ms marker alternates with a variable data interval.
DATA_COLS = [5, 7, 9, 11, 13, 15]
MARKER_COLS = [4, 6, 8, 10, 12, 14, 16]
DOMINANT_ZC = 18


def decode(path, zc_count=DOMINANT_ZC):
    x, sr = sf.read(path, dtype='int16')
    R = x[:, 1].astype(np.float32)
    L = x[:, 0].astype(np.float32)
    del x

    n = ctrl.agc(R, sr)
    del R
    sync, thr, score = ctrl.find_sync_auto(n, sr)
    zc = ctrl.zero_crossings(n)
    del n

    d = np.diff(sync)
    med = np.median(d)
    locked = np.flatnonzero(np.abs(d - med) <= 0.08 * med)

    # speech energy of the story channel, for validation columns
    F = 64
    e = ctrl.block_reduce(L, F)
    esr = sr / F
    del L

    rows = []
    for k in locked:
        a, b = sync[k], sync[k + 1]
        z = zc[(zc > a) & (zc < b)]
        if len(z) != zc_count:
            continue
        iv = np.diff(np.concatenate(([a], z, [b]))) / sr * 1000.0
        rows.append((a / sr, iv))

    if not rows:
        return None

    t = np.array([r[0] for r in rows])
    M = np.array([r[1] for r in rows])
    dur = M.sum(axis=1)
    Mn = M / dur[:, None] * dur.mean()      # remove tape-speed common mode
    energy = e[np.clip((t * esr).astype(int), 0, len(e) - 1)]

    return dict(t=t, data=Mn[:, DATA_COLS], dur=dur, energy=energy,
                sr=sr, thr=thr, score=score, med_ms=med / sr * 1000.0,
                n_sync=len(sync), n_locked=len(locked), n_rows=len(rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('flac')
    ap.add_argument('-o', '--out')
    a = ap.parse_args()

    r = decode(a.flac)
    if r is None:
        sys.exit('no frames decoded from %s' % a.flac)

    print('%-34s thr=%.1f lock=%.0f%% period=%.2fms sync=%d locked=%d frames=%d'
          % (a.flac.split('/')[-1][:34], r['thr'], 100 * r['score'],
             r['med_ms'], r['n_sync'], r['n_locked'], r['n_rows']))

    if a.out:
        with open(a.out, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['t_s', 'frame_ms', 'speech_energy']
                       + ['d%d_ms' % c for c in DATA_COLS])
            for i in range(len(r['t'])):
                w.writerow(['%.4f' % r['t'][i], '%.3f' % r['dur'][i],
                            '%.1f' % r['energy'][i]]
                           + ['%.4f' % v for v in r['data'][i]])
        print('  wrote %s (%d rows)' % (a.out, len(r['t'])))


if __name__ == '__main__':
    main()
