# Teddy Ruxpin — Conversational Animatronic

Working plan. Last updated 2026-08-23 (TTS choice + song path added).

## Goal

Reuse the original Teddy Ruxpin mechanism as a battery-powered conversational
toy: squeeze the paw, ask a question, the bear searches, answers aloud, and the
mouth and eyes animate in sync with the speech.

## Architecture — thin client + server

```
  BEAR (ESP32-WROOM-32, battery)          SERVER (always-on box)
  ------------------------                ----------------------
  paw button -> wake from deep sleep
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

- Only the **eyes** axis is wired and calibrated. Upper and lower mouth pots are
  on GPIO 34 and 36 and read 0 — not yet connected.
- The jaw is a *compound* axis (upper and lower opposed). Two pots, one logical
  position. Needs thought: probably drive both to matching percentages and let
  each close its own loop.
- PWM carrier is 1 kHz (`analogWrite` default). Inaudible now that nothing
  stalls, but it sits in the speech band and the mic will hear it during motion.
  Raise to ~20 kHz via `analogWriteFrequency()` before phase 4.

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

- **Squeeze-to-talk, not wake word.** WakeNet costs ~30mA continuous, which
  becomes the entire budget. A paw button on an RTC GPIO waking from deep sleep
  gets months of standby, and it's better privacy for a kid's toy.
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
- **34-39 are input-only with no internal pull-ups.** Good for the mic data line
  and the paw button; the button needs an external 10k pull-up.
- WROVER modules use 16/17 for PSRAM. WROOM-32 is fine.

| Signal | GPIO | | Signal | GPIO |
|---|---|---|---|---|
| `AIN1` / `AIN2` / `PWMA` | 32 / 33 / 25 | | I2S out BCLK | 18 |
| `BIN1` / `BIN2` / `PWMB` | 26 / 27 / 14 | | I2S out LRCLK | 5 |
| `CIN1` / `CIN2` / `PWMC` | 13 / 23 / 22 | | I2S out DIN | 17 |
| `STBY` (drivers A/B) | 21 | | I2S mic SCK / WS | 16 / 4 |
| `STBYC` (eyes) | 19 | | I2S mic SD (input-only) | 35 |
| | | | Paw button (ext0 wake) | 34 |

Free: 2, 12, 15, 36, 39, plus UART0 (1/3) for the console. RTC-capable GPIOs are
0, 2, 4, 12-15, 25-27, 32-39, which is why 34 works as the wake pin.

**That leaves exactly one safe spare output — GPIO 15.** 36 and 39 are input
only, 12 is untouchable, and 2 straps. Anything added later has a one-pin budget.
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
   Remaining: wire and calibrate the two mouth axes.
2. **Server pipeline, desktop only** — mic in, endpointing, STT, Claude with
   search, sentence chunking, Fish Audio TTS with timestamps, play to laptop
   speakers. Print the envelope and cues to console. No bear involved.
   *Also the point where Fish Audio gets its quality check — hosted vs
   self-hosted, and whether the cloned voice is good enough to commit to.*
3. **Frame protocol + playback** — server emits frames, ESP32 buffers and plays
   PCM through the MAX98357A. Motors ignored. Verify buffer depth and jitter.
4. **Join them** — jaw from `jaw_state` with lookahead, eyes from `cue_id`.
   Tune the lookahead until movement lands on the sound.
5. **Power** — deep sleep, paw wake, separate rails, battery. Measure real
   standby draw.
6. **Enclosure** — fit it all back in the bear.

**Songs** are a parallel track, not a phase. Once phase 4 works, a song is just a
pre-rendered audio file plus a pre-computed track played through the same code
path — nothing new to build on the bear, only content to produce. Do it whenever.

## Open questions

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

- Existing sketch: `TeddyRuxbin.ino` (Uno, TB6612 test jig, open-loop)
- Motor library: `SparkFun_TB6612` — pure `pinMode`/`digitalWrite`/`analogWrite`,
  compiles unmodified on ESP32/STM32 despite `architectures=avr`.
- Boards evaluated and rejected: STM32F103C8T6 (no WiFi, no I2S),
  STM32F411CEU6 (no WiFi), Pi Zero 2 W (~120mA idle kills battery).
