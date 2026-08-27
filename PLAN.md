# Teddy Ruxpin — Conversational Animatronic

Working plan. Last updated 2026-08-23 (TTS choice + song path added).

## Goal

Reuse the original Teddy Ruxpin mechanism as a battery-powered conversational
toy: ask a question, the bear searches, answers aloud, and the
mouth and eyes animate in sync with the speech.

## Architecture — thin client + server

```
  BEAR (ESP32-WROOM-32, battery)          SERVER (always-on box)
  ------------------------                ----------------------
  trigger (undecided) -> wake from deep sleep
  I2S mic ---- raw PCM upstream -------->  endpointing (VAD)
                                           -> STT (streaming)
                                           -> Claude (streaming + web search)
                                           -> sentence chunker
                                           -> TTS (streaming)
                                           -> envelope + cue track
  I2S amp <--- frame stream ------------  [seq][jaw][cue][PCM]
  TB6612 x2 <- local gesture table
```

**Why split:** battery. The ESP32 idles at ~10uA in deep sleep against the
Pi Zero 2 W's ~120mA, and the bear is idle 99.9% of the time. The server does
all TLS, orchestration, and DSP; the bear plays PCM and moves motors.

## Frame protocol

Fixed 20ms frames, 16kHz mono s16:

```
[uint16 seq][uint8 jaw_state][uint8 cue_id][320 bytes PCM]
```

- `jaw_state` — 0..255, continuous position across the axis's working range.
  Quantization to 3-4 states was a concession to open-loop timing; the pots
  removed the need for it (see motion control).
- `cue_id` — index into the bear's local gesture table, 0 = none
- Server sends *intent*, bear owns the mechanics. Retune gestures by reflashing
  the bear; never touch the server.

**Buffer ~300ms (15 frames).** It does two jobs: absorbs network jitter, and
gives the jaw its lookahead. The motor reads its target ~4 frames ahead of the
playback pointer so the mechanical lag of the gearmotor lands the movement on
the sound instead of after it. **Measured at ~75ms** on the eyes axis — the
sweep log shows the pot flat for the first three samples after drive begins —
which confirms the 80ms guess this was designed around.

## Motion control — closed-loop on pot feedback

Each original servo carries a **5 kΩ potentiometer** on its output shaft. Five
wires per plug: two to the motor (with suppression caps across them) and three
to the pot, in the plastic body behind the gearbox. This is the first-generation
"controllerless servo" — motor and sensor in the head, amplifier on the main
board — and it is why the original cassette could command absolute positions
with pulse-position modulation on its right track.

The earlier revisions of this plan assumed no sensors existed. That was wrong.

### Measured — eyes axis, 2026-08-24

| Property | Value |
|---|---|
| pot | 5 kΩ, smooth sweep, no dead spots |
| stop to stop | adc 137 → 3154 (3017 counts) |
| hand-tuned working range | adc 146 → 2294 |
| positioning error | within ±25 counts (~1%) |
| repeatability | **1-2 counts** on repeat visits |
| `min_duty` (stiction) | 70, measured automatically |
| mechanical dead time | ~75 ms before motion starts |
| full travel | ~2250 ms at duty 200 |

### What the pots delete

- **Stall-homing.** Was the riskiest open question in phase 1. Homing now drives
  until the reading stops changing, so it terminates on arrival — 471ms when
  near home, 2631ms from the far stop. The old fixed `HOME_MS` of 900ms was
  silently homing to the *middle* of travel, which the sweep log caught.
- **`travel_ms` and dead reckoning.** Position is read, not inferred. No drift,
  so no re-homing between sentences.
- **Movement on boot.** A calibrated axis knows where it is the moment the ADC
  is read. The bear powers up silent.
- **Jaw quantization.** ~2150 usable counts instead of 3-4 discrete states. The
  smooth continuous envelope this plan wanted and gave up on is back.
- **Hand-tuned `min_duty`.** Ramp duty until the pot moves. The old text said
  this was unmeasurable without motion sensing; the pot *is* motion sensing.

### The controller

Two-phase, because the ~75ms of dead time makes a single-phase proportional loop
either undershoot or overshoot depending on how it is tuned — both were observed:

1. **Coarse**, error > `band/2`: drive continuously, duty tapering with error
   from `RUN_DUTY` down to a floor of `min_duty + 20`.
2. **Fine**, inside that: 30ms burst, brake, 70ms settle, re-measure. Momentum
   cannot carry past the target because the drive is already off.

Tolerance ±25 counts. `band` is span/8.

### Never command the mechanical limits

The calibrated endpoints *are* the stops, so 0% and 100% would drive into them
and stall — audible as a winding whine. Percentages map onto a range inset from
each endpoint by `margin`, per axis and NVS-backed. With endpoints hand-tuned to
the *visual* limits rather than the mechanical ones, a small margin (3-5%) is
enough.

### Calibration, all retunable over serial

`calib <axis>` drives to both stops and records them. `tune <axis>` is
interactive — arrow keys nudge, `h`/`f` capture the current position as an
endpoint — for setting limits by eye where the linkage has compliance and the
pot keeps reading past where the mechanism visibly moves. Everything lands in
NVS; nothing needs a reflash.

### Still open

- ~~The jaw's mechanical bandwidth is unmeasured~~ — **measured 2026-08-27, see
  below.** It can track speech, with compressed excursion.
- The jaw is a *compound* axis (upper and lower opposed): two pots, one logical
  position. Implemented as `AX_JAW` — `mouthPct()` drives both to matching
  percentages and lets each close its own loop. Written, but its behaviour under
  a fast-changing speech envelope is untested.
- PWM carrier is 1 kHz (`analogWrite` default). Inaudible now that nothing
  stalls, but it sits in the speech band and the mic will hear it during motion.
  Raise to ~20 kHz via `analogWriteFrequency()` before phase 4.

## The original cassette control track — a ground-truth corpus

`ref/the-third-crystal.zip` holds five original story tapes ripped to stereo
FLAC, 44.1 kHz/16-bit, ~81 minutes total (provenance in Reference). They are not
just content: the right channel is the **original servo control track**, and it
survived the rip in all five files.

That makes this a labeled dataset for the one thing this plan otherwise guesses
at — what the jaw should do given the audio. Same three motors, same mechanism,
so once the payload is decoded the positions should land on the calibrated ADC
ranges directly. Phase A got the framing but not the payload; see below.

### Measured — 2026-08-27

| Property | Value |
|---|---|
| files | 5, stereo 44.1 kHz/16, 14.2–17.5 min each (~81 min) |
| L/R correlation | +0.017 to +0.037 — the channels are unrelated |
| left channel | story audio |
| right channel | control track, peak 7080–10199 (healthy in all five) |
| frame period | **17.1–17.3 ms (~58 Hz)** |
| autocorrelation at that lag | 0.98 on a clean passage |

Frame rate was measured two independent ways that agree: FFT autocorrelation
over a 4 s window (17.08 ms / 58.6 Hz) and direct autocorrelation over 30 s
(17.26 ms / 58.0 Hz). Lags of 5, 10, 12.5, 20, 25, 33.3, 40 and 50 ms all score
at or below zero, so ~17.3 ms is the true frame period, not a harmonic.

### Signal structure

Per frame, from a sample-level dump:

1. a **high-amplitude sync burst**, clearly above everything else in the frame
2. a body of **variable-width half-cycles**
3. a quiet inter-frame gap

Information is carried in the **intervals between zero crossings** — pulse
position, as the controllerless-servo design implies. It is *not* a bit-clocked
binary stream: at fine resolution the intervals are continuous, not bimodal, and
the count per frame varies 8–13. An early read of the gap histogram suggested
self-clocking binary; higher resolution disproved it. Don't re-derive that.

Tape level varies enough that a fixed-threshold burst detector silently fails on
some passages — it found no sync at all in two of the five files at one sample
point, while their correlation and peak amplitude matched the other three.
**Adaptive gain is a decoder requirement, not an optional refinement.**

### Measured — mouth axes, 2026-08-27

Both mouth axes wired and calibrated. `sweep u 255` / `sweep l 255`, then the
streaming tick against a synthetic envelope.

| Property | upper | lower | eyes (for scale) |
|---|---|---|---|
| stop to stop | 752 → 4082 | 747 → 3959 | 146 → 2294 |
| span | 3330 | 3212 | 2148 |
| `min_duty` | 85 | 85 | 70 |
| dead time | ~65 ms | ~65 ms | ~75 ms |
| open-loop slew @ duty 255 | **4.93 counts/ms** | **4.76 counts/ms** | 0.95 @ duty 200 |

**At equal duty all three axes are comparable.** The eyes measure **4.06
counts/ms at duty 255** (136 → 3184 in ~750 ms, dead time ~25–50 ms), against
the mouth's 4.9. The earlier "eyes are 5x slower" reading came from comparing
duty 200 against duty 255 — at 200 the eyes take 2250 ms for the same travel, so
**duty matters far more than the axis does.** Only measuring at matched duty
settled it.

Note the eyes' *mechanical* range runs to 3184 while the stored `adcFar` is
2294: the endpoints were hand-tuned to the visual limits, as intended, so the
usable range is 2148 counts and a blink is ~340 ms each way.

**Closed-loop, streaming, duty 255.** Driving `stream j square 2 3 10 55` (a
45-point commanded swing at 2 Hz) the jaw achieves **665–805 counts per 250 ms
half-cycle — 24–29% of the usable range, about 60% of what was commanded.**
Closed-loop slew runs 3.0–3.7 counts/ms, below the open-loop 4.9 because duty
tapers as error closes.

So: **syllable tracking works, at compressed excursion.** Roughly 25% of range at
2 Hz, proportionally less as rate rises. The mechanism is a low-pass filter and
will compress whatever it is handed.

What this fixes in the design:

- **Stream at duty 255, not `RUN_DUTY` 200.** At 200 the same test achieved only
  ~1.9 counts/ms. `RUN_DUTY` exists so `travel_ms` means something against a
  fixed reference; streaming has no such constraint. `STREAM_DUTY` is now 255.
- **Compress the envelope before sending it.** Commanding 0–100% is wasted — the
  jaw cannot get there and the loop just saturates. Map `jaw_state` into roughly
  the lower half of the range and let the mechanism compress the rest.
- **Lookahead ~120 ms, i.e. 6 frames at 20 ms.** The bare dead time is ~65 ms —
  after a target flip the reading travels the wrong way that long before
  reversing — but end-to-end against a real speech envelope the best
  command-to-position correlation sits at **120–140 ms** of lag, because the
  loop also needs time to close. The frame protocol's ~80 ms was too optimistic;
  budget 6 frames, not 4.
- **Pre-smooth the envelope to ~2 Hz** — measured, not estimated. Anything faster
  is duty spent on motion the mechanism cannot execute.

**Frequency response**, `stream j sine <hz> 3 10 55`, commanded swing 1253 counts:

| Rate | Achieved | Ratio | Peak slew |
|---|---|---|---|
| 1 Hz | 1319 | 105% | 4.50 c/ms |
| 2 Hz | 879 | **70%** | 5.55 |
| 3 Hz | 526 | 42% | 5.75 |
| 4 Hz | 303 | 24% | 4.55 |
| 5 Hz | 173 | 14% | 3.85 |

A clean first-order rolloff with the **−3 dB corner at 2 Hz**, falling as ~1/f
above it. This is the single most important number for the audio→jaw mapping,
and it bounds what any amount of cleverness upstream can achieve:

| Speech structure | Rate | What the jaw renders |
|---|---|---|
| prosody, phrase rhythm | 1–3 Hz | 42–105% — **fully** |
| syllables | 3–8 Hz | 14–42% — partially |
| phonemes | 10–15 Hz | ~5% — **not at all** |

**The mechanism renders prosody and syllable rhythm. It physically cannot render
phoneme detail.** Viseme-accurate animation would be low-passed into nothing, so
effort spent there buys almost no visible motion.

### Upper and lower move separately

The original cassette drove the two halves independently, and a mouth whose
halves move as one reads as a hinge rather than as speech. `jawTickSplit(upper,
lower)` takes two percentages; `jawTick(pct)` is now a wrapper for gestures.
`stream` takes a trailing `uscale` — the upper's swing as a percentage of the
lower's.

Measured at 2 Hz, commanded vs achieved upper/lower ratio:

| `uscale` | upper p-p | lower p-p | achieved |
|---|---|---|---|
| 100% | 896 | 923 | 97% |
| 60% | 792 | 938 | 84% |
| 40% | 585 | 895 | 65% |

Achieved ratio runs higher than commanded because at 2 Hz both halves are
slew-limited, so a smaller command does not shrink excursion proportionally.
**Command roughly half the split you want to see** — ~20% for a visual 40%.

**ADC headroom — not saturating, but thin.** Upper's far stop reads 4083 against
a 4095 ceiling, which looked like clipping. It is not: both axes decelerate into
their plateau over 2–3 samples and then sit with ±3 counts of noise rather than
pinned at 4095, which is a mechanical stop, not a rail. But upper has ~12 counts
of headroom, and **the ESP32's ADC reference is the internal bandgap, not the
3V3 rail** — so a supply sag under motor inrush shifts the pot reading without
shifting the reference, and the loop reads a false position. This is a second,
independent reason for the separate regulator the Power section already calls
for.

### The envelope path — built and running on hardware

The audio→jaw path exists end to end, off the bench, with no server, network or
frame protocol involved.

- **`tools/envelope.py`** — audio in, jaw envelope out. Tier 1 of the mapping:
  20 ms frame RMS, gated, low-passed at the measured 2 Hz corner, with a faster
  attack than release. `--csv` to inspect, `--send PORT` to play it on the bear.
- **`envload <n>` / `envplay [loops] [log]`** — the board buffers a whole
  envelope in RAM (1500 frames, 3 KB) and plays it on the frame grid. Streaming
  it live would put serial jitter on that grid for no benefit.
- **`--play PORT`** — same, but plays the audio through the desktop at the same
  time so the sync can be judged by eye. The bear has no audio of its own until
  phase 3, and waiting for that to see whether the mapping looks right would be
  the wrong order to build in. `--lead MS` (default 120) is how far ahead of the
  audio the jaw is commanded; tune it by eye, since the audio path has its own
  start latency on top of the mechanism's lag.

Verified on 12 s of real speech: **600 frames, mean frame interval 20.0 ms**,
commanded lower 0–55% / upper 0–11%, achieved 1453 counts p-p on the lower and
319 on the upper (a 22% ratio against 20% commanded), and **r = +0.76 between
commanded percentage and achieved position** — the shortfall being the lag and
rolloff already measured.

Two things this shook out, both worth not rediscovering:

- **The console's axis gate.** Every command below `int ax = axisFromTok(...)`
  in `handle()` must take an axis as its first argument, or it dies with "bad or
  missing axis". `envload`/`envplay` take a frame count, so they sit above it.
- **Never send payload to a board that has not acknowledged.** Opening the port
  resets the ESP32; anything written during boot is parsed as console commands.
  `envelope.py` waits for the prompt, then for the `envload` acknowledgement,
  before sending a single frame.

**Do not pre-smooth to the mechanism's corner.** The obvious move — low-pass the
envelope at the jaw's measured 2 Hz — is wrong, because the mechanism low-passes
regardless and the two filters compound into ~1 Hz. Measured on the real jaw
against a Fish Audio clip:

| Software corner | Achieved p-p | Commanded closure | Corr | Lag |
|---|---|---|---|---|
| 2 Hz | 1501 | 5% | +0.920 | 120 ms |
| 5 Hz | **1550** | **16%** | +0.881 | 140 ms |

The lighter filter gets *more* excursion and three times the inter-phrase
closure — and closure between phrases is most of what reads as speech. Let the
mechanism be the filter; `CORNER_HZ` is 5 Hz. The lower correlation is the
command carrying detail the jaw cannot follow, which costs nothing.

**Eyes on the same path.** `envload` takes an optional third column and
`envplay` ticks the eyes with it; a two-column envelope leaves them at neutral.
The bench eye track is placed on **phrase structure, never on level** — blinks
sit in the gaps where the mouth is shut, topped up to a natural idle rate when
the phrasing does not supply enough. Amplitude-driven eyes look wrong, and
nothing here is tempted by them.

Measured on the Fish Audio clip, the two channels behave very differently:

| Channel | Corr | Lag | Achieved p-p |
|---|---|---|---|
| jaw lower | +0.878 | 140 ms | 1553 |
| eyes | **+0.997** | 60 ms | 993 |

The eyes track almost perfectly because a 340 ms blink ramp sits well inside the
mechanism's bandwidth; the jaw's 0.88 is the speech-rate detail it cannot
follow. This is the clearest statement of the whole constraint: **commands inside
the bandwidth are rendered faithfully, and only those.** It also means eye
animation needs no cleverness to look right, while the jaw will always be an
approximation.

What the bench track cannot do is the part that needs meaning — look up on a
question, widen on surprise. That needs the cue markup from Claude, and it is
the one piece of the eye design still waiting on the server pipeline.

**Known limitation of the tier-1 mapping:** it normalises against the 95th
percentile with a floor *relative* to that, not an absolute dBFS gate. An
absolute gate never fires on material with anything underneath it and the mouth
hangs open for the whole clip. Even so, a story tape with music under the
narration has only ~15 dB of frame-level dynamic range and yields almost no
closure — clean TTS, with real silence between phrases, is the material this is
for.

### Realtime TTS → servos, running 2026-08-27

Live speech animates the bear. Fish Audio streams PCM over its websocket, the
envelope is computed per chunk as it arrives, and frames feed the board's
`envstream` on the 20 ms grid while the audio plays on the desktop.

    tools/speak.py "Hello Jeff! Do you want to read a story with me today?"

Measured: **339 frames at 49.1 fps against a 50 fps grid, 0 starved, 0 underrun,
0 dropped.** Voice id `33311739f2214b82b7ad64e46f077164`; credentials read from
OS1's `.env`. Interpreter is `~/.venv/teddy` (numpy, soundfile, pyserial,
fish-audio-sdk, python-dotenv) — OS1's own venv lacks numpy and pyserial and is
left untouched.

**`envstream` on the bear.** A 128-frame ring decouples serial jitter from the
frame grid, prefilling 15 frames (~300 ms, as the frame protocol specifies)
before the clock starts. On underrun the axes **hold** — a dropout is a missing
command, not an instruction to move, and a jaw that snaps shut on every network
hiccup looks far worse than one that pauses.

Three things this shook out, all of which cost a run to find:

- **Fish streams faster than realtime.** Pacing frame production off arrival
  overran the ring and dropped 130 of 341 frames on the first attempt. The ring
  is a jitter buffer, not a spool for a whole utterance. A reader thread now
  fills queues and the main loop drains them on a wall clock — which is also
  exactly realtime for the audio, so ffplay stays correctly fed with no rate
  arithmetic anywhere.
- **The idle timer must watch bytes, not ring occupancy.** Keyed off occupancy,
  it stalls precisely when the ring is full, so the board timed out and
  re-entered `envstream` mid-utterance — which presented as the banner printing
  ~58 times, not as anything resembling a timeout.
- **Realtime cannot normalise against the clip's 95th percentile** the way the
  offline path does, and adaptive gain pumps. The voice and TTS settings are
  fixed, so `REF_DB` is calibrated once and pinned (`--calibrate` prints it;
  currently −17.0 dB for this voice at `FISH_VOLUME=6`). Recalibrate on a voice
  or volume change, not per utterance.

**Eyes, live.** The offline track centres a blink in a gap whose length it can
measure. Live there is no lookahead, so a blink starts once the mouth has been
shut briefly. Still placed on phrase structure, never on level.

**Audio playback: aplay, and pin its buffer.** Three findings, each of which
presented as "the audio is delayed":

- **ffplay is the wrong player here.** It buffers raw PCM before starting, and
  because this loop feeds at exactly realtime it never catches that up — the
  startup buffer becomes permanent delay, audible as the voice trailing the jaw
  even at `--lead 10`. aplay writes near-straight to ALSA.
- **ALSA needs a cushion, so account for it rather than fighting it.** Fed
  exactly 20 ms per 20 ms tick it underruns on the first scheduling hiccup.
  `--cushion` (default 200 ms) is written up front; since that is a known
  constant latency, the frames are delayed by `cushion - lead` to match. So
  `--lead` means what it should — **ms the jaw leads the sound** — and stays
  tunable without the cushion leaking into it.
- **Pin `--buffer-time` and `--period-time`.** On aplay's defaults the cushion
  does not correspond to what ALSA actually holds, and it reported multi-second
  underruns. Pinned, they disappear. Also spawn aplay *after* the cushion
  exists: started earlier it holds the device open through the second before
  Fish returns any audio and calls that wait an underrun.

Verified: 469 frames at 49.8 fps over a 9.4 s utterance, 0 starved, 0 underrun,
0 dropped, no ALSA complaints. `--lead 140` is the current default; retune by eye
on a line with hard consonants, which give a crisp visual edge.

**Still missing for a real conversation:** the audio comes out of the desktop,
not the bear — the MAX98357A is phase 3 — and the text is a command-line
argument rather than Claude's streamed reply. Neither blocks the other; the
`speak.py` structure is already the shape the server needs, with `gen()`
standing in for the token stream it will eventually be handed.

### Phase A results — 2026-08-27

The physical layer is decoded. `tools/decode_control_track.py` locks onto the
frame structure of all five tapes and emits per-frame data as CSV. The payload
*semantics* are not decoded — see below.

    tools/decode_control_track.py "ref/<story>.flac" -o "ref/decoded/<story>.csv"

Columns: `t_s, frame_ms, speech_energy, d5_ms … d15_ms`. All five stories are
decoded to `ref/decoded/` — **200,785 frames**, gitignored with the rest of
`ref/`. `speech_energy` is the left channel's envelope at each frame time,
carried alongside so any analysis can correlate against the audio without
re-reading the FLAC.

| Story | Lock | Period | Frames |
|---|---|---|---|
| The Airship New | 93% | 17.98 ms | 34,461 |
| Autumn Adventure New | 90% | 19.71 ms | 33,203 |
| Lost in Boggley Woods | 84% | 16.71 ms | 50,900 |
| The Third Crystal | 84% | 17.62 ms | 38,036 |
| Wooly and the Giant Snowzos | 82% | 17.85 ms | 44,585 |

**Sync and framing.** AGC-normalising against a 200 ms envelope makes one fixed
threshold valid across a whole tape (envelope percentiles match to two decimals
at t=120/300/500 s). The burst threshold still has to be picked per file — tape
balance between burst and body varies — so it is calibrated on three short
windows and then applied in one full pass. Frame lock: **82–95% on all five
files.**

**Frame period is per-tape, not universal:** 16.7, 17.6, 17.8, 17.9 and 19.7 ms
across the five. Within one file, tape speed wanders 3.4% (frame duration
17.979 ± 0.615 ms). Both mean the decoder must lock per file and normalise
intervals by frame duration; nothing may assume a fixed 17.2 ms.

**Frame layout**, from zero-crossing intervals. 76% of frames have exactly 18
zero crossings, the rest 20/22/24 — always even:

| Cols | Content |
|---|---|
| 0–3 | the sync burst's own crossings |
| 4,6,8,10,12,14,16 | fixed markers, ~0.4 ms, std ~0.02 |
| 5,7,9,11,13,15 | **six variable data intervals** |
| 17, 18 | trailer, then the ~4.1 ms inter-frame gap |

Seven fixed markers bracket six data fields. Six, not the three the earlier
draft of this section predicted — so it is not simply one interval per motor.

**The data is discrete, not continuous.** Column 9 resolves into three cleanly
separated, *evenly spaced* levels — 0.62 / 1.07 / 1.52 ms, steps of 0.45 — with
literally zero density in the gaps between them. The other columns show a sharp
primary mode plus shoulders. These are symbol alphabets, not analogue positions.

**Nothing here tracks speech amplitude.** Correlated against the story channel's
energy, speed-normalised and detrended, no data column reaches r > 0.5 in *any*
10 s window of a whole file; the best is 0.41. Detrending *lowers* the
correlation (0.31 → 0.14), so what little exists lives in multi-second trends,
not syllable-rate tracking. A direct "interval = jaw openness ∝ loudness"
reading is therefore ruled out.

The decode is nevertheless real: detrended lag-1 autocorrelation of the data
columns is 0.90–0.96, so these are genuine smooth slowly-varying signals, not
noise from a mis-locked detector.

**Hypotheses tested and rejected**, so they are not re-tried:

| Hypothesis | Result |
|---|---|
| Interval = servo position ∝ loudness | No 10 s window in a file reaches r > 0.5 |
| Simple pulse count, `n × base unit` | Best residual 0.16 over units 0.20–0.70 ms (0.25 = random) |
| Biphase mark / FM, as SMPTE timecode | Only 73% of frames give a consistent bit count; a correct decode would give ~99% |

Biphase was the best of the three and is worth revisiting with a smarter cell
threshold — the even zero-crossing count is still suggestive of it. The obstacle
is that the wide intervals span 0.6–1.5 ms, too broad for one cell width, which
hints at three symbol widths rather than two.

### Not yet established

- **The payload semantics.** Six discrete fields, three motors. Likely either a
  digital word spanning several frames, or per-frame commands with channel
  addressing. This is the remaining work, and it is real reverse engineering,
  not a tuning pass.
- Which field drives which motor, and absolute scaling onto the pot ADC ranges.
- A caution for whoever picks this up: two scoring bugs already produced
  convincing-looking wrong answers here. A lock score of "fraction of intervals
  inside [13, 22] ms" is gamed by the re-trigger guard, which forces every
  interval above 13 ms — it reported 100% lock on a detector that was simply
  firing the moment the guard expired. Score *tightness around the median*
  instead. And correlating without removing tape-speed common mode inflates
  everything.

### What the corpus is for

**Phase A — decode.** *Physical layer done (see Phase A results); payload not.*
`tools/decode_control_track.py` emits `t_s, frame_ms, speech_energy, d5..d15`
per story. Of the three validations this section originally demanded, two passed
— the fields evolve smoothly, and the frame structure is consistent across all
five tapes — and **the third failed**: no field correlates with left-channel
speech energy. That failure is the finding. It means the track is not an
amplitude envelope in disguise, so the remaining work is decoding a digital
payload rather than rescaling an analogue one.

The fallback this section anticipated is now the live path: the raw interval
vector *is* a usable feature vector even before the semantics are known.

**Phase B — measure what the originals actually did.** This is the payoff:

- **Lead/lag** — cross-correlate jaw against audio energy. This calibrates the
  frame-protocol lookahead with evidence. The current ~80 ms is inferred from the
  mechanism's 75 ms dead time, not from anything about speech.
- **Rest position** — does the jaw fully close between words or hover open?
- **Attack/release asymmetry** — almost certainly opens faster than it closes.
- **How much jaw variance plain RMS explains.** The R² decides whether an
  envelope suffices or spectral features are needed.
- **Eyes vs. amplitude.** This plan asserts eyes follow meaning, not sound. Low
  eye/energy correlation is direct evidence for the cue-track design; high
  correlation would mean rethinking it.

**Phase C — fit and evaluate.** Write the audio → `jaw_state` function using the
measured attack/release, rest and gain; score against held-out stories.

**Phase D — play an original through the pipeline.** Resample L to 16 kHz mono,
resample the decoded track onto the 20 ms grid, emit through the frame protocol.
The ideal phase 3/4 test article: the animation is known-good, so anything that
looks wrong is firmware, lookahead or mechanism, never the envelope algorithm.
It unconfounds two bugs that would otherwise mask each other.

A and B are desktop-only and need no bear, so they run in parallel with wiring
the mouth axes.

## Eyes follow the words, not the sound

Amplitude-driven eyes look wrong. Eyes follow meaning and rhythm: blink at
sentence boundaries and every 3-5s idle (~15-20/min is natural), look up on
questions, widen on surprise.

Have Claude emit the performance markup with the text via structured output:

```json
{"speech": "That's a great question! Let me think about that.",
 "cues": [{"after_word": 0, "cue": "WIDE"},
          {"after_word": 4, "cue": "LOOK_UP"},
          {"after_word": 8, "cue": "BLINK"}]}
```

Server maps word indices to time offsets using TTS timing data, then interleaves
`cue_id` into the frames. Jaw from envelope, eyes from cues — two independent
tracks that never fight.

This is what makes Fish Audio the leading TTS candidate: its streaming endpoint
returns audio chunks *and* a cumulative word-level alignment snapshot in the same
SSE payload, which is exactly the input the cue mapper needs. See the TTS section
below.

## TTS — Fish Audio (leading candidate)

Checked 2026-08-23. Requirements were streaming *and* word-level timing, and Fish
Audio has both in one endpoint.

- **`text-to-speech-stream-with-timestamps`** — SSE stream where each payload
  carries an audio chunk plus a cumulative word-level alignment snapshot. Feeds
  the cue mapper directly; no forced alignment needed on the server.
- **Voice cloning** from 10-30s of clean speech. Gives the bear a consistent
  voice without fine-tuning.
- **Inline tags** in `[brackets]` for emotion, tone, and pacing. Free-form
  natural language, not a fixed vocabulary. Useful for performance markup —
  Claude can emit these alongside the eye cues.
- **`fish-speech` is open source** (github.com/fishaudio/fish-speech). Self-hosting
  on the server means no per-use cost, no cloud dependency, and no audio from a
  kid's toy leaving the house. Strongly preferred if quality holds up.
- **Free S2.1 Pro API window ends 2026-08-31.** Cheap way to A/B it against
  ElevenLabs before committing. That window is nearly closed.

Fallbacks if it disappoints: ElevenLabs (character-level timing, better voices,
paid) or Azure (emits word-boundary and viseme events natively).

### What it will not do: sing

Fish Audio has **no singing, melody, or pitch tags** — the documented set is
emotions, tone markers, and audio effects, all speech. Tags are free-form so
`[singing]` is accepted without error, but what comes out is sing-songy *speech*,
not melody on pitch and in time. Real singing synthesis needs note and duration
conditioning, which the API doesn't expose. Don't design around it.

## Songs — a separate, pre-rendered path

Songs are **fixed content**. The bear is not improvising a song in response to a
question, it is playing one of N songs chosen in advance. So songs skip the live
pipeline entirely:

1. Render each song offline — dedicated singing-voice-synthesis model, a real
   recording, or a licensed track. Voice engine can differ from the TTS voice.
2. Pre-compute the full jaw envelope and eye cue track from the finished
   waveform. No streaming budget, no latency constraint, and *better* sync than
   live speech because the whole waveform is available in advance.
3. Store the audio + track pair on the server. (SD card on the bear is *not*
   reachable on the WROOM-32 — SPI needs 3 outputs + 1 input and only GPIO 15 is
   safely spare. See the pin map.)
4. Play through the **same frame protocol and the same firmware path** as speech.

The singing voice not matching the speaking voice is a real cost, but the
original toy had that seam too, and it is a far smaller problem than trying to
make a TTS model sing.

## Storage on the bear

- **NVS (`Preferences`)** — per axis: `adcHome`, `adcFar`, `marginPct`,
  `min_duty`, and the now-vestigial `travel_ms`. Survives reboot, retunable over
  serial at runtime.
- **`const` array in flash** — gesture table. Named primitives as
  `(motor, dir, duty, ms)` sequences: `NEUTRAL`, `BLINK`, `LOOK_UP`, `WIDE`,
  `JAW_0..3`.

## Claude configuration

Web search is a server-side tool — no scraping, no separate search API.

```python
client.beta.messages.stream(
    model="claude-opus-5",
    max_tokens=1024,
    speed="fast",                          # up to 2.5x output tok/s
    betas=["fast-mode-2026-02-01"],
    output_config={"effort": "low"},       # short spoken answers
    tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
    system="You are a teddy bear talking to a child. Answer in 1-3 short "
           "spoken sentences. No lists, no markdown, no URLs.",
    messages=[...],
)
```

- **Fast mode** is Opus 5 / Opus 4.8 only, Claude API only, premium pricing.
- **`effort: "low"`** — brief spoken answers, less thinking, lower latency.
  Keep thinking on (default on Opus 5); disabling it has failure modes.
- The system prompt matters more than usual — anything that renders as markdown
  or a URL becomes garbage when spoken aloud.
- **Chunk the token stream at sentence boundaries** and fire TTS on sentence one
  while Claude is still writing sentence two. Roughly halves perceived latency.

### Latency budget (target < 1.5s to first sound)

| Stage | Typical |
|---|---|
| Endpointing | 200-400ms |
| STT (streaming) | 150-300ms |
| Claude first token | 300-800ms |
| TTS first chunk | 100-300ms |
| **Total** | **~0.8-1.8s** |

## Power

- **The trigger is undecided.** Squeeze-to-talk is out, and nothing replaces it
  yet - which matters, because the deep-sleep argument this whole architecture
  rests on assumed the bear is idle 99.9% of the time. Options and their costs:
  - **Wake word** (ESP-SR WakeNet) - ~30mA continuous. That *is* the budget;
    months of standby collapses to days, and it listens constantly.
  - **Always streaming** - worse on power, and everything said in the room
    leaves the house.
  - **A physical switch** - the original toy's model. Cheap, honest, and
    dissolves the problem entirely at the cost of one deliberate action.

  Settle this before sizing the pack. GPIO 35 is the pin left for it.
- ~2000mAh LiPo -> several hundred interactions.
- **Give the ESP32 its own regulator off the pack.** Motor inrush on a shared
  rail browns out the MCU and resets it mid-sentence. Add bulk capacitance at the
  motor supply. This is the #1 failure mode for battery animatronics and it
  presents as random reboots.

## Parts

| Part | Note |
|---|---|
| ESP32-WROOM-32 devkit (Hosyond, CP2102) | in hand; bare module for phase 5 |
| MAX98357A | I2S amp, drives the original speaker |
| INMP441 or ICS-43434 | I2S MEMS mic |
| 2x TB6612FNG | already in hand, already wired |
| LiPo ~2000mAh + charger | |
| Separate 3.3V regulator | for the ESP32, off the pack |

## Pin map (ESP32-WROOM-32)

A Hosyond ESP-WROOM-32 devkit (CP2102) is the target. It meets every requirement
the architecture leans on: WiFi, two I2S peripherals, LEDC on any GPIO, ~10uA
deep sleep, RTC wake pins. The S3 features the plan was originally written around
turn out not to matter — PSRAM (the frame buffer is 15 x 324 B ~ 5 KB,
comfortably in DRAM), native USB (flash over the CP2102), and vector
instructions (the server does the DSP). Phase 1 builds at 22% flash, 6% RAM.

Any GPIO can do PWM via LEDC, so there's no PWM-pin constraint like on STM32 —
but the pin budget is 16 outputs + 2 inputs against ~19 clean GPIO, which needs
care:

- **GPIO 12 is unusable.** Strapping pin — high at boot selects the wrong flash
  voltage and the board won't come up. Presents as a dead board.
- **GPIO 0 and 2** also strap; leave them alone. **6-11** are flash.
- **34-39 are input-only with no internal pull-ups.** Ideal for the pot wipers,
  which need no drive and must sit on ADC1 anyway.
- WROVER modules use 16/17 for PSRAM. WROOM-32 is fine.

| Signal | GPIO | | Signal | GPIO |
|---|---|---|---|---|
| `AIN1` / `AIN2` / `PWMA` | 32 / 33 / 25 | | I2S out BCLK / LRCLK / DIN | 18 / 5 / 17 |
| `BIN1` / `BIN2` / `PWMB` | 26 / 27 / 14 | | I2S mic SCK / WS | 16 / 4 |
| `CIN1` / `CIN2` / `PWMC` | 13 / 23 / 22 | | I2S mic SD | 15 |
| `STBY` (drivers A/B) | 21 | | pot wipers: upper / lower / eyes | 34 / 36 / 39 |
| `STBYC` (eyes) | 19 | | pot ends | `3V3` / `GND` |

Pot wipers must be on **ADC1** (32-39) - ADC2 is dead while WiFi is up. 34/36/39
are also input-only, so they cost no output pin.

**Free: GPIO 35 (input-only, ADC1, RTC) and GPIO 2**, plus UART0 (1/3) for the
console. 12 is untouchable. That is the entire remaining budget - and whatever
replaces the trigger has to come out of it.
Tying `STBY` and `STBYC` to a single GPIO buys one more, at the cost of being
able to standby the eyes driver independently.

**Keep external pulldowns on `STBY` and `STBYC`.** Between reset and the first
line of `setup()` those pins float, and a floating standby pin can twitch a
motor on every boot.

**Phase 5 caveat:** a devkit measures 5-15mA in deep sleep, not ~10uA — the
AMS1117 LDO and the CP2102 never sleep. True of S3 devkits too; the microamp
figures assume a bare module. Real standby measurement needs a bare WROOM
soldered down, or the regulator lifted.

## Build phases

Each phase is independently testable. Don't skip ahead.

1. **Bench calibration sketch** — ESP32-WROOM-32 + the two TB6612s + the bear's
   mechanism. Stall-home on boot, measure `travel_ms`, store in NVS, run the
   gesture table. No audio, no network. *Answers the riskiest open question:
   does stall-homing hold up on the real mechanism?*
   Written: `Calibrate/Calibrate.ino`, serial console at 115200.
   **Done on the eyes axis 2026-08-24** — closed-loop, ±25 counts, repeatable to
   1-2. Answered better than expected: the pots made stall-homing unnecessary.
   All three axes are wired and driven by the firmware. Remaining: measure the
   jaw's slew rate (see Still open).
2. **Server pipeline, desktop only** — mic in, endpointing, STT, Claude with
   search, sentence chunking, Fish Audio TTS with timestamps, play to laptop
   speakers. Print the envelope and cues to console. No bear involved.
   *Also the point where Fish Audio gets its quality check — hosted vs
   self-hosted, and whether the cloned voice is good enough to commit to.*
3. **Frame protocol + playback** — server emits frames, ESP32 buffers and plays
   PCM through the MAX98357A. Motors ignored. Verify buffer depth and jitter.
4. **Join them** — jaw from `jaw_state` with lookahead, eyes from `cue_id`.
   Tune the lookahead until movement lands on the sound.
5. **Power** — deep sleep, the wake trigger (undecided, see Power), separate
   rails, battery. Measure real standby draw.
6. **Enclosure** — fit it all back in the bear.

**Songs** are a parallel track, not a phase. Once phase 4 works, a song is just a
pre-rendered audio file plus a pre-computed track played through the same code
path — nothing new to build on the bear, only content to produce. Do it whenever.

## Open questions

- **What wakes the bear?** The paw button is gone and nothing replaces it. This
  gates the power budget and phase 5 - see Power.

- ~~Which TTS?~~ Fish Audio, pending a phase-2 quality check. Decide hosted vs
  self-hosted `fish-speech` at the same time.
- Where do songs come from? Dedicated SVS model, real recordings, or licensed
  tracks — see the songs section. Not needed until after phase 4.
- Which STT? Whisper on the server (free, ~300ms) vs Deepgram (faster, paid).
  Fish Audio also has an ASR endpoint with word-level timestamps — worth testing
  while evaluating their TTS, since it would mean one vendor and one SDK.
- Does the original speaker sound acceptable off the MAX98357A, or does it need
  replacing?
- Server host — always-on desktop, Pi, or small VPS? Affects whether the bear
  works away from home.

## Reference

- **How the mouth is driven** — `docs/how-it-works.md`, and the same with four
  measured diagrams as `docs/how-it-works.html`. The explainer for why the jaw
  follows loudness alone and how that differs from the tape's authored track.
- Existing sketch: `TeddyRuxbin.ino` (Uno, TB6612 test jig, open-loop)
- Motor library: `SparkFun_TB6612` — pure `pinMode`/`digitalWrite`/`analogWrite`,
  compiles unmodified on ESP32/STM32 despite `architectures=avr`.
- Boards evaluated and rejected: STM32F103C8T6 (no WiFi, no I2S),
  STM32F411CEU6 (no WiFi), Pi Zero 2 W (~120mA idle kills battery).
- **Original story tapes** — `ref/the-third-crystal.zip`, five stereo FLACs whose
  right channel is the original servo control track. See the ground-truth corpus
  section. 432 MB, gitignored.

  Source: **"Teddy Ruxpin Tapes w/ Signals"**, World of Wonder 1986, digitised
  from @Reminaprod's collection and uploaded to the Internet Archive
  2023-04-22 by "Decode Document Digitize", described as *"Tapes with movement
  data"* — the control track is the stated point of the upload, not a lucky
  side effect of someone ripping both channels.

  https://archive.org/details/the-third-crystal

  The item offers **FLAC, WAV and VBR MP3** of the same five titles. Take FLAC or
  WAV only. **Never the MP3** — a psychoacoustic codec assumes both channels are
  something a human listens to, and it will quietly mangle a pulse train it
  treats as inaudible noise. The whole corpus rests on that channel surviving
  bit-exact.
