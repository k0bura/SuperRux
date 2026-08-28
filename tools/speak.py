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

    def __init__(self, ref_db=REF_DB, range_db=RANGE_DB, corner=CORNER_HZ,
                 eye_gain=1.0):
        self.ref, self.base = ref_db, ref_db - range_db
        fs = 1000.0 / FRAME_MS
        self.a_rel = 1.0 - np.exp(-2 * np.pi * corner / fs)
        self.a_att = min(1.0, self.a_rel * 3.0)
        self.y = 0.0
        self.tail = np.zeros(0, dtype=np.int16)
        self.n = int(SAMPLE_RATE * FRAME_MS / 1000.0)
        self.since_blink = 0
        self.blink_left = 0
        self.blink_depth = EYES_SHUT
        self.blink_half = int(BLINK_MS / FRAME_MS)
        self.eye_gain = eye_gain
        self.i = 0
        self.rng = np.random.default_rng(0)
        self.ph1 = float(self.rng.uniform(0, 6.28))
        self.ph2 = float(self.rng.uniform(0, 6.28))
        self.was_shut = 0
        self.widen = 0
        self.loud = 0.0
        self.loud_thr = LOWER_MAX * 0.55

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
        """Lid position. Placed on phrase structure and slow time, never on
        level. Offline the blink can be centred in a gap whose length is known;
        live there is no lookahead, so a blink starts once the mouth has been
        shut a moment. Everything else works fine streaming: drift is a function
        of time, the phrase-onset widen only needs the shut->open edge, and the
        loud settle only needs a running mean."""
        g = self.eye_gain
        fps = 1000.0 / FRAME_MS
        self.i += 1
        t = self.i / fps
        v = float(EYES_NEUTRAL)

        if g > 0:
            # Two incommensurate sines, so the idle never visibly loops.
            v += g * 3.0 * np.sin(2 * np.pi * t / 9.3 + self.ph1)
            v += g * 2.0 * np.sin(2 * np.pi * t / 14.7 + self.ph2)

            # Widen on the shut -> speaking edge: drawing breath to speak.
            if lower > 0 and self.was_shut >= int(0.18 * fps):
                self.widen = int(0.18 * fps)
            self.was_shut = self.was_shut + 1 if lower == 0 else 0
            if self.widen > 0:
                v += g * 9.0 * (self.widen / (0.18 * fps))
                self.widen -= 1

            # Settle the lids through sustained loud passages.
            self.loud += 0.02 * ((1.0 if lower > self.loud_thr else 0.0) - self.loud)
            v -= g * 5.0 * min(max(self.loud, 0.0), 1.0)

        half = int(BLINK_MS / FRAME_MS)
        self.since_blink += 1
        if self.blink_left == 0 and lower == 0 and self.since_blink > int(2500 / FRAME_MS):
            r = self.rng.random()
            if g <= 0 or r < 0.62:
                self.blink_depth, self.blink_half = EYES_SHUT, half
            elif r < 0.85:
                self.blink_depth, self.blink_half = EYES_NEUTRAL - 26, int(half * 0.7)
            else:
                self.blink_depth, self.blink_half = EYES_SHUT, int(half * 0.8)
            self.blink_left = 2 * self.blink_half
            self.since_blink = 0
        if self.blink_left > 0:
            k = abs(self.blink_left - self.blink_half)
            self.blink_left -= 1
            f = 1.0 - k / float(max(self.blink_half, 1))
            v += (self.blink_depth - v) * f

        return int(round(min(100.0, max(0.0, v))))


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
                    help='ms the jaw leads the sound; negative puts it behind')
    ap.add_argument('--eye-gain', type=float, default=1.0, metavar='G',
                    help="scale the eyes' expressive motion; 0 = blinks only")
    ap.add_argument('--cushion', type=int, default=200, metavar='MS',
                    help='ALSA prefill; lower is tighter but risks underruns')
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
    env = Envelope(eye_gain=a.eye_gain)
    # aplay, not ffplay. ffplay buffers raw PCM before it starts, and because
    # this loop feeds at exactly realtime it never catches that up -- the startup
    # buffer becomes permanent delay, audible as the voice trailing the jaw even
    # at --lead 0. aplay writes near-straight to ALSA.
    #
    # Spawned later, once the cushion exists: started here it holds the device
    # open through the second or so before Fish returns any audio, and reports
    # that wait as a multi-second underrun.
    player = None

    # Fish streams audio faster than realtime, so frames must be paced against a
    # wall clock, not against arrival. Producing at arrival rate overruns the
    # board's ring -- a jitter buffer, not a spool for a whole utterance -- and
    # drops frames. The reader thread fills the queues; the main loop drains
    # them on the 20ms grid, which is also exactly realtime for the audio, so
    # aplay stays correctly fed with no rate arithmetic.
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

    # ALSA needs a cushion. Fed exactly 20ms per 20ms tick it underruns on the
    # first scheduling hiccup, so the loop writes `cushion` ms up front. That
    # cushion is latency -- the audio you hear is that far behind the write --
    # but it is a known constant, so the frames are delayed to match instead of
    # leaving it for the ear to fight. Net: the jaw leads the *sound* by --lead.
    cushion_bytes = int(SAMPLE_RATE * 2 * a.cushion / 1000.0)
    net = a.cushion - a.lead
    delay_frames = max(0, int(round(net / FRAME_MS)))     # hold frames back
    lead_frames  = max(0, int(round(-net / FRAME_MS)))    # hold audio back
    nframes = under = ticks = 0
    ashort = 0
    adeficit = 0
    byte_rate = SAMPLE_RATE * 2
    written = 0
    audio_t0 = None

    t_fill = time.time()
    while len(pcm) < cushion_bytes and not done.is_set() and time.time() - t_fill < 20:
        while audio:
            pcm += audio.popleft()
        time.sleep(0.004)
    while audio:
        pcm += audio.popleft()

    # Buffer and period pinned rather than left to aplay's defaults, so the
    # cushion above actually corresponds to what ALSA holds.
    #
    # stderr is discarded because aplay's underrun warning here is a false
    # signal, and a noisy one. It prints *after* playback completes, reporting a
    # "length" equal to the whole stream, while the loop's own instrumentation
    # shows the write target was met on every single tick (0 short, 0 deficit)
    # and standalone aplay fed by this exact pacing never produces it. Real
    # starvation is still reported, by measurement rather than by hearsay: the
    # `audio:` line below counts ticks where the target could not be met, and
    # the board counts its own frame underruns independently.
    player = subprocess.Popen(
        ['aplay', '-q', '-f', 'S16_LE', '-r', str(SAMPLE_RATE), '-c', '1',
         '--buffer-time=%d' % (a.cushion * 1000 * 3),
         '--period-time=%d' % (FRAME_MS * 1000), '-'],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    prefill = 0
    if pcm:
        prefill = min(len(pcm), cushion_bytes)
        player.stdin.write(bytes(pcm[:prefill]))
        player.stdin.flush()
        del pcm[:prefill]
        written += prefill
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

            ticks += 1
            if frames and ticks > delay_frames:
                u, l, e = frames.popleft()
                nframes += 1
                if board:
                    board.write(b'%d,%d,%d\n' % (u, l, e))
                    board.flush()
            elif not done.is_set() and ticks > delay_frames:
                under += 1

            # Audio is kept topped up against a target depth rather than
            # written one chunk per tick. Writing 20ms per 20ms tick is open
            # loop: over hundreds of ticks a single late one drains ALSA and it
            # underruns. Targeting elapsed-time + cushion is self-correcting.
            if ticks > lead_frames and audio_t0 is None:
                audio_t0 = time.time()
            if audio_t0 is not None:
                target = int((time.time() - audio_t0) * byte_rate) + cushion_bytes
                while written < target and len(pcm) >= bpf:
                    player.stdin.write(bytes(pcm[:bpf]))
                    del pcm[:bpf]
                    written += bpf
                player.stdin.flush()
                if written < target and not done.is_set():
                    ashort += 1
                    adeficit = max(adeficit, target - written)

            if time.time() - next_t > 1.0:
                next_t = time.time()

        if err:
            raise err[0]
        if pcm:
            player.stdin.write(bytes(pcm))
        # Pad the tail with a cushion's worth of silence. Without it ALSA's
        # buffer is still half full of audio when stdin closes, the device runs
        # dry mid-buffer, and aplay reports a spurious underrun whose "length"
        # is the whole stream. The audio was fine; the boundary was not.
        player.stdin.write(b'\x00' * cushion_bytes)
        player.stdin.flush()
        el = time.time() - t0
        print('  %d frames in %.2fs (%.1f fps)  starved %d'
              % (nframes, el, nframes / max(el, 1e-9), under))
        print('  audio: short on %d ticks, worst deficit %d ms, prefill %d ms'
              % (ashort, int(adeficit / byte_rate * 1000),
                 int(prefill / byte_rate * 1000)))
    finally:
        if board:
            board.write(b'end\n')
            board.flush()
            time.sleep(0.6)
            sys.stdout.write(board.read(4096).decode('utf-8', 'replace'))
            board.close()
        if player:
            try:
                player.stdin.close()
            except Exception:
                pass
            player.wait()


if __name__ == '__main__':
    main()
