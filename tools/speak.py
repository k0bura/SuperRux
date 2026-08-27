#!/usr/bin/env python3
"""Speak text through Fish Audio and animate the bear in realtime.

Streams TTS as PCM over Fish's websocket, turns each chunk into jaw and eye
frames as it arrives, and feeds them to the bear's `envstream` on the 20 ms
frame grid. The audio is held back by the jaw's measured lag so the mouth does
not visibly trail the voice.

Credentials come from OS1's .env (FISH_API_KEY); the voice id defaults to
Teddy's. Needs ~/.venv/teddy -- see PLAN.md.

    tools/speak.py "Hello Jeff, do you want to read a story?"
"""
import argparse
import os
import subprocess
import sys
import threading
import time
from collections import deque

import numpy as np

FRAME_MS   = 20
SAMPLE_RATE = 44100
CORNER_HZ  = 5.0        # see PLAN.md: not the jaw's 2 Hz corner, deliberately
LOWER_MAX  = 55
USCALE     = 20
EYES_NEUTRAL = 65
EYES_SHUT    = 5
BLINK_MS     = 340

# Realtime cannot normalise against the whole clip's 95th percentile the way the
# offline path does, and adaptive gain pumps audibly. The voice and TTS settings
# are fixed, so the reference is calibrated once and pinned. --calibrate prints
# the value for a new voice or a changed FISH_VOLUME.
REF_DB   = -17.0
RANGE_DB = 25.0

VOICE_ID = '33311739f2214b82b7ad64e46f077164'
OS1_ENV  = os.path.expanduser('~/code/OS1/.env')


class Envelope:
    """Incremental version of tools/envelope.py, fed PCM as it arrives."""

    def __init__(self, ref_db=REF_DB, range_db=RANGE_DB, corner=CORNER_HZ):
        self.ref, self.base = ref_db, ref_db - range_db
        fs = 1000.0 / FRAME_MS
        self.a_rel = 1.0 - np.exp(-2 * np.pi * corner / fs)
        self.a_att = min(1.0, self.a_rel * 3.0)
        self.y = 0.0
        self.tail = np.zeros(0, dtype=np.int16)
        self.n = int(SAMPLE_RATE * FRAME_MS / 1000.0)
        self.since_blink = 0
        self.blink_left = 0

    def feed(self, pcm_bytes):
        """PCM s16le in, list of (upper, lower, eyes) frames out."""
        a = np.frombuffer(pcm_bytes, dtype=np.int16)
        buf = np.concatenate((self.tail, a)) if len(self.tail) else a
        nf = len(buf) // self.n
        self.tail = buf[nf * self.n:].copy()
        if nf == 0:
            return []

        f = buf[:nf * self.n].reshape(nf, self.n).astype(np.float64) / 32768.0
        rms = np.sqrt((f ** 2).mean(axis=1)) + 1e-12
        db = 20 * np.log10(rms)
        norm = np.clip((db - self.base) / max(self.ref - self.base, 1e-6), 0, 1)
        norm[db < self.base] = 0.0

        out = []
        for v in norm:
            self.y += (self.a_att if v > self.y else self.a_rel) * (v - self.y)
            lower = int(round(self.y * LOWER_MAX))
            upper = int(round(self.y * LOWER_MAX * USCALE / 100.0))
            out.append((upper, lower, self._eyes(lower)))
        return out

    def _eyes(self, lower):
        """Blink in the gaps, never on level. No lookahead is available live, so
        a blink starts when the mouth has been shut a moment rather than being
        centred in a gap whose length we cannot yet know."""
        half = int(BLINK_MS / FRAME_MS)
        self.since_blink += 1
        if self.blink_left == 0 and lower == 0 and self.since_blink > int(2500 / FRAME_MS):
            self.blink_left = 2 * half
            self.since_blink = 0
        if self.blink_left == 0:
            return EYES_NEUTRAL
        k = abs(self.blink_left - half)
        self.blink_left -= 1
        f = 1.0 - k / float(half)
        return int(round(EYES_NEUTRAL + (EYES_SHUT - EYES_NEUTRAL) * f))


def load_key():
    if os.environ.get('FISH_API_KEY'):
        return
    try:
        for ln in open(OS1_ENV):
            if ln.startswith('FISH_API_KEY='):
                os.environ['FISH_API_KEY'] = ln.split('=', 1)[1].strip().strip('"\'')
                return
    except OSError:
        pass
    sys.exit('FISH_API_KEY not set and not found in %s' % OS1_ENV)


def open_board(port):
    import serial
    s = serial.Serial(port, 115200, timeout=0.2)
    time.sleep(2.5)                       # opening the port resets the board
    s.reset_input_buffer()
    s.write(b'\n')
    t0, buf = time.time(), ''
    while time.time() - t0 < 10:
        buf += s.read(256).decode('utf-8', 'replace')
        if '>' in buf:
            break
    else:
        sys.exit('board did not reach a prompt')
    s.write(b'envstream 4000\n')
    t0, buf = time.time(), ''
    while time.time() - t0 < 10:
        buf += s.read(256).decode('utf-8', 'replace')
        if 'streaming' in buf:
            return s
    sys.exit('board did not enter envstream: %r' % buf[-160:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('text', nargs='+')
    ap.add_argument('--port', default='/dev/ttyUSB0')
    ap.add_argument('--voice', default=VOICE_ID)
    ap.add_argument('--lead', type=int, default=140,
                    help='ms the jaw is commanded ahead of the audio')
    ap.add_argument('--no-board', action='store_true')
    ap.add_argument('--calibrate', action='store_true',
                    help='print level stats instead of animating')
    a = ap.parse_args()
    text = ' '.join(a.text)

    load_key()
    from fishaudio import FishAudio
    from fishaudio.types import TTSConfig

    fish = FishAudio()
    cfg = TTSConfig(format='pcm', sample_rate=SAMPLE_RATE,
                    latency=os.environ.get('FISH_WS_LATENCY', 'balanced'),
                    chunk_length=int(os.environ.get('FISH_WS_CHUNK', '200')))

    def gen():
        yield text

    if a.calibrate:
        dbs = []
        n = int(SAMPLE_RATE * FRAME_MS / 1000.0)
        for chunk in fish.tts.stream_websocket(gen(), reference_id=a.voice,
                                               config=cfg):
            if not chunk:
                continue
            x = np.frombuffer(chunk, dtype=np.int16).astype(np.float64) / 32768.0
            k = len(x) // n
            if k:
                r = np.sqrt((x[:k * n].reshape(k, n) ** 2).mean(axis=1)) + 1e-12
                dbs.extend(20 * np.log10(r))
        d = np.array(dbs)
        print('  frames %d' % len(d))
        print('  dB  p5=%.1f  p50=%.1f  p95=%.1f  max=%.1f'
              % tuple(np.percentile(d, [5, 50, 95, 100])))
        print('  -> set REF_DB = %.1f' % np.percentile(d, 95))
        return

    board = None if a.no_board else open_board(a.port)
    env = Envelope()
    player = subprocess.Popen(
        ['ffplay', '-hide_banner', '-loglevel', 'quiet', '-nodisp', '-autoexit',
         '-f', 's16le', '-ar', str(SAMPLE_RATE), '-ac', '1', '-'],
        stdin=subprocess.PIPE)

    # Fish streams audio faster than realtime, so frames must be paced against a
    # wall clock, not against arrival. Producing at arrival rate overruns the
    # board's ring -- a jitter buffer, not a spool for a whole utterance -- and
    # drops frames. The reader thread fills the queues; the main loop drains
    # them on the 20ms grid, which is also exactly realtime for the audio, so
    # ffplay stays correctly fed with no rate arithmetic.
    frames, audio, err = deque(), deque(), []
    done = threading.Event()

    def reader():
        try:
            for chunk in fish.tts.stream_websocket(gen(), reference_id=a.voice,
                                                   config=cfg):
                if not chunk:
                    continue
                for f in env.feed(chunk):
                    frames.append(f)
                audio.append(chunk)
        except Exception as e:
            err.append(e)
        finally:
            done.set()

    threading.Thread(target=reader, daemon=True).start()

    t_wait = time.time()
    while len(frames) < 15 and not done.is_set() and time.time() - t_wait < 20:
        time.sleep(0.005)

    pcm = bytearray()
    bpf = int(SAMPLE_RATE * 2 * FRAME_MS / 1000.0)
    lead_frames = max(0, int(a.lead / FRAME_MS))
    nframes = under = 0
    t0 = time.time()
    next_t = t0

    try:
        while True:
            while audio:
                pcm += audio.popleft()
            if done.is_set() and not frames and len(pcm) < bpf:
                break

            now = time.time()
            if now < next_t:
                time.sleep(min(0.004, next_t - now))
                continue
            next_t += FRAME_MS / 1000.0

            if frames:
                u, l, e = frames.popleft()
                nframes += 1
                if board:
                    board.write(b'%d,%d,%d\n' % (u, l, e))
                    board.flush()
            elif not done.is_set():
                under += 1

            if nframes > lead_frames and len(pcm) >= bpf:
                player.stdin.write(bytes(pcm[:bpf]))
                player.stdin.flush()
                del pcm[:bpf]

            if time.time() - next_t > 1.0:
                next_t = time.time()

        if err:
            raise err[0]
        if pcm:
            player.stdin.write(bytes(pcm))
        player.stdin.flush()
        el = time.time() - t0
        print('  %d frames in %.2fs (%.1f fps)  starved %d'
              % (nframes, el, nframes / max(el, 1e-9), under))
    finally:
        if board:
            board.write(b'end\n')
            board.flush()
            time.sleep(0.6)
            sys.stdout.write(board.read(4096).decode('utf-8', 'replace'))
            board.close()
        try:
            player.stdin.close()
        except Exception:
            pass
        player.wait()


if __name__ == '__main__':
    main()
