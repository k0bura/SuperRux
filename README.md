# Teddy Ruxpin — Conversational Animatronic

Turning a 1985 Worlds of Wonder Teddy Ruxpin into a battery-powered
conversational toy: ask a question, the bear searches, answers aloud, and the
mouth and eyes animate in sync with the speech.

The original mechanism is reused as-is. No servos added, no sensors retrofitted,
no gears replaced.

## Status

**Phase 1 — bench calibration: done on the eyes axis.** Closed-loop positioning
to ±25 ADC counts, repeatable to 1–2 counts on repeat visits. Mouth axes are
wired for drive but their pots are not yet connected.

Full design, build phases, and open questions live in [PLAN.md](PLAN.md).

## The thing that changed the design

The plan was written around **open-loop** control: no position feedback, home by
stalling into the mechanism's hard end stops, and quantize the jaw to 3–4
discrete states because timed moves can't hold a smooth envelope.

That premise was wrong. Each original servo has **five wires**, not two:

```
2 × motor  (with suppression caps across them)
3 × 5 kΩ potentiometer  (in the plastic body behind the gearbox)
```

These are first-generation *controllerless servos* — motor and position sensor
in the head, amplifier on the main board. It's why the original cassette could
command absolute positions using pulse-position modulation on its right-hand
track while audio played on the left.

Reading those pots deletes most of the original plan's compromises:

| Was going to be | Turned out |
|---|---|
| stall-home into the stops on every boot | pot reads position at power-up; bear never moves on boot |
| `travel_ms` dead reckoning, re-homed between sentences | position is read, not inferred — no drift |
| jaw quantized to 3–4 states | ~2150 usable counts, continuous envelope |
| `min_duty` hand-tuned (assumed unmeasurable) | ramp duty until the pot moves — 70, measured automatically |
| ~80 ms mechanical lag (a guess) | **~75 ms, confirmed** from the sweep log |

## Hardware

- **ESP32-WROOM-32** devkit (CP2102)
- **2 × TB6612FNG** motor drivers
- Original Teddy Ruxpin servos — upper mouth, lower mouth, eyes
- 3.7–4.5 V pack for `VM` (the motors are 3 V rated; **not** the ESP32's 3V3 or VIN)

### Pin map

| Signal | GPIO | | Signal | GPIO |
|---|---|---|---|---|
| `AIN1` / `AIN2` / `PWMA` | 32 / 33 / 25 | | upper pot wiper | 34 |
| `BIN1` / `BIN2` / `PWMB` | 26 / 27 / 14 | | lower pot wiper | 36 |
| `CIN1` / `CIN2` / `PWMC` | 13 / 23 / 22 | | eyes pot wiper | 39 |
| `STBY` (drivers A/B) | 21 | | pot ends | `3V3` / `GND` |
| `STBYC` (eyes) | 19 | | I2S mic SD | 15 |

Pot wipers must be on **ADC1** (GPIO 32–39) — ADC2 is unusable while WiFi is up.
34/36/39 are input-only, which suits an analog input and costs no output pin.

**GPIO 12 is unusable**: it's a strapping pin, and high at boot selects the wrong
flash voltage — the board simply won't start. Keep pulldowns on `STBY`/`STBYC` so
the drivers stay disabled through boot.

## Layout

```
PLAN.md              full design: architecture, frame protocol, TTS, power
docs/wiring.md       wiring reference with diagrams
docs/wiring.html     the same as a self-contained page, with a tick-off checklist
Calibrate/           phase 1 firmware — motors, pots, calibration console
TeddyRuxbin.ino      original Uno test jig (reference only)
```

Wiring, including the pot dividers and the power topology, is in
**[docs/wiring.md](docs/wiring.md)**.

## Build

```sh
arduino-cli core install esp32:esp32
arduino-cli compile -u -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32 Calibrate
```

Needs the `SparkFun_TB6612` library. It declares `architectures=avr` but compiles
unmodified on ESP32 — `analogWrite` is LEDC-backed on core 3.x.

## Console

115200 baud. Use `miniterm` rather than `arduino-cli monitor` if you want the
arrow keys in `tune` to work:

```sh
python3 -m serial.tools.miniterm --echo --rts 0 --dtr 0 /dev/ttyUSB0 115200
```

`--echo` gives you local echo. The firmware never echoes what you type — it
buffers each character and prints only the `> ` prompt — so without this you
type blind. It is terminal-side only and sends nothing extra to the board, so it
cannot trip the firmware's TX-into-RX guard. In `tune`, arrow keys echo as their
raw escape sequences; that is cosmetic.

`--rts 0 --dtr 0` is not optional. On this devkit RTS drives EN and DTR drives
GPIO 0, so a terminal that asserts them on open holds the board in reset or in
download mode. You then read a stale USB-serial buffer instead of the console —
fast repeating garbage on an exact 32-byte period, sometimes binary, sometimes
mangled slices of the help text with the `> ` prompt spliced mid-line. It looks
like a firmware fault and is not one.

Axes: `u` upper mouth, `l` lower mouth, `e` eyes, `j` jaw (upper+lower opposed).

| Command | Does |
|---|---|
| `show` | constants, live ADC, endpoints, margin |
| `pot [axis]` | read pots; with an axis, stream 15 s and report range |
| `sweep <axis> [duty] [ms]` | home, then drive stop-to-stop logging `ms,adc` |
| `calib <axis>` | drive to both stops, record endpoints |
| `tune <axis>` | interactive — arrows nudge, `h`/`f` capture endpoints |
| `end <axis> [home\|far] [adc]` | set an endpoint; no value captures current position |
| `margin <axis> [pct]` | inset from the stops so limits never stall |
| `stiction <axis>` | ramp duty until the pot moves; saves `min_duty` |
| `pos <axis> <pct>` | move to % of range — closed-loop when calibrated |
| `home <axis>` | go to inset zero |
| `jog <axis> <o\|c> <ms> [duty]` | raw timed drive |
| `g <NAME>` / `demo` | gestures / run them all |
| `save` | force-write NVS |

Gestures (eyes): `NEUTRAL` `BLINK` `SLOWBLINK` `LOOK_UP` `WIDE` `SLEEPY` `PEEK` `DART`.
They're target positions with dwells, not timed drives, so they look the same
regardless of where the axis started.

All calibration lives in NVS and is retunable over serial without a reflash.

## Notes from the bench

- **Never power `VM` from the ESP32's `3V3` or `VIN`.** The question is current,
  not voltage. Phase 1 stalls motors deliberately, and USB can't carry it.
- **Motors couple EMI into the console UART.** The first stiction ramp showed
  framing errors mid-transmission. The firmware drops indented input (its own
  output can never parse as a command) and flushes RX after every move.
- **The gearbox can't be back-driven by hand** — high reduction. Use `sweep`,
  which drives the motor and samples the pot together.
- Seized servos are the classic failure on these bears — forty years of hardened
  grease. Working the mechanism by hand frees them.

## License

MIT
