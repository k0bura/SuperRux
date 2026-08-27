# How the mouth is driven

Why the jaw follows loudness alone, why that turns out to be enough, and how the
original 1985 cassette did it the other way round.

`how-it-works.html` in this folder is the same content as a self-contained page
with four measured diagrams — the waveform-to-motor trace, the mechanism's
frequency response, one cassette control frame, and the two pipelines side by
side. Open it locally or serve it with GitHub Pages; GitHub shows the source, not
the render.

## There is no letter-matching step

Nothing in the pipeline detects a letter, a phoneme, or a sound. There is no
speech recognition, no phoneme table, no viseme mapping, and no text involved at
any point — **the jaw would behave identically if you fed it a cello.** The
entire input is a loudness measurement taken 50 times a second.

Four steps, no cleverness:

1. **Chop the audio into 20 ms frames.** Each gets one number, its RMS level in dB.
2. **Gate the silence.** More than 25 dB below the voice's reference counts as
   silence and forces the mouth shut. This closure between phrases does more
   visible work than any other single step.
3. **Smooth it.** One-pole, opening faster than it closes. Mouths snap open and
   relax shut, and that asymmetry is most of what separates speech from a hinge.
4. **Map to a jaw percentage,** scaled into the lower half of travel. Commanding
   the full range asks for motion the motor cannot deliver and the loop saturates.

## Why that is enough

The mechanism is a first-order low-pass with its **−3 dB corner at 2 Hz**:

| Speech structure | Rate | Rendered |
|---|---|---|
| Phrasing and prosody | 1–3 Hz | 42–105% |
| Syllables | 3–8 Hz | 14–42% |
| Phonemes — individual sounds | 10–15 Hz | ~5% |

A perfect phoneme-accurate animation would be filtered away to about 5% of its
intended motion. The gearmotor physically cannot move that fast, so the effort
buys almost no visible difference. Loudness is not an approximation of the right
answer here — at the rates this mechanism can render, it is most of the way to it.

The cases where loudness genuinely misleads are real but fast: an `m` is voiced
and loud with the mouth shut, an `s` is quiet with the mouth open. Both are
phoneme-rate events, and both get smoothed into their neighbours before the jaw
could have shown them.

## The tape did the opposite

The original tapes are stereo and the channels are unrelated — measured
correlation 0.02. Left is the story; right is a digital control track, framed at
58 Hz, six discrete data fields per frame (a sync burst, seven fixed ~0.4 ms
markers alternating with six variable fields, then a 4.1 ms gap).

That track is **not an envelope.** Nothing in it correlates with how loud the
voice is — across a whole 14-minute story no field reaches even a weak
correlation with the audio in any 10-second window. Its values are discrete, not
continuous: one field resolves into three cleanly separated levels with nothing
in the gaps between them.

So the tape was not deriving motion from sound. It was replaying **motion
somebody authored**, stored alongside the sound and synchronised to it only
because they shared a piece of tape.

| | 1985 cassette | This build |
|---|---|---|
| Source of motion | Authored by hand | Computed from loudness |
| Stored on disk | The animation itself | Nothing |
| Frame rate | 58 Hz | 50 Hz |
| Channels per frame | 6, discrete | 3, continuous |
| Position feedback | None — open loop | Pot on every shaft |
| Knows the words | The animator did | Never |
| New material | Author and press a tape | Say anything, instantly |

The trade is honest. Hand-authored animation can hold a shape through a pause and
close on an `m` — it can act. What it cannot do is respond to a sentence nobody
wrote in advance, which is the entire point of this build.

## Two motors, not one

The mouth has two motors and the original drove them separately. A mouth whose
halves move as one reads as a hinge opening, not a face speaking, so the upper
jaw gets the same envelope scaled down and each half closes its own loop against
its own pot.

The scaling is not linear: at speech rates both halves are slew-limited, so a
smaller command does not produce a proportionally smaller movement. Commanding
40% yields about 65% visually — **command roughly half the split you want to see.**

| | Value |
|---|---|
| Upper slew @ duty 255 | 4.93 counts/ms |
| Lower slew @ duty 255 | 4.76 counts/ms |
| Dead time | ~65 ms |
| Total lag, command to motion | 120–140 ms |

That last figure is why the jaw is commanded *ahead* of the sound, by exactly
that much.

## The eyes are driven by rhythm, not level

Amplitude-driven eyes look wrong — they read as a meter, not a face. Blinks are
placed on phrase structure instead: in the gaps where the mouth is already shut,
topped up to a natural idle rate when the speech does not supply enough pauses.

| Channel | Command vs achieved | Lag |
|---|---|---|
| Jaw | 0.878 | 140 ms |
| Eyes | 0.997 | 60 ms |

The eyes track almost perfectly. That is not a better algorithm — it is the same
constraint from the other side. A 340 ms blink ramp sits well inside the
mechanism's bandwidth, while the jaw's 0.878 is speech-rate detail the motor
cannot follow. **Eye animation needs no cleverness to look right; the jaw will
always be an approximation.**

## Still unknown

The control track's framing is decoded, its payload is not: six discrete fields,
three motors, no mapping between them. Rejected hypotheses, so they are not
retried — a field as a position proportional to loudness, a simple pulse count,
and biphase-mark encoding as in SMPTE timecode.

Cracking it would not change how the bear speaks. It would tell us what the
original animators thought the right answer was — a ground truth to check the
envelope against, from people who solved this once already on the same three
motors.

---

All figures measured on the mechanism, 2026-08-27. Slew and dead time from
stop-to-stop sweeps at full duty; frequency response from sine sweeps at 1–5 Hz;
lag and correlation from a Fish Audio clip played through the real jaw.
Control-track figures from five original story tapes, 81 minutes, decoded to
200,785 frames. See PLAN.md for the raw measurements.
