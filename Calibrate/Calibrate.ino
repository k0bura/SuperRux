/******************************************************************************
Calibrate.ino
Teddy Ruxpin - phase 1: stall-home, travel measurement, gesture table.

Target: ESP32 ESP-WROOM-32 (Hosyond devkit, CP2102)
FQBN:   esp32:esp32:esp32

Phase 1 of PLAN.md: motors only, no audio, no network. Answers the riskiest
open question - does stall-homing hold up on the real mechanism?

Calibration constants live in NVS so they are retunable over serial without a
reflash. Console at 115200, type "help".
******************************************************************************/

#include <SparkFun_TB6612.h>
#include <Preferences.h>

// Pin map for ESP-WROOM-32. Avoids GPIO 12 (strapping - high at boot selects
// the wrong flash voltage and the board will not come up), GPIO 0 and 2 (also
// strapping), and 6-11 (flash). See PLAN.md.
#define AIN1  32
#define AIN2  33
#define PWMA  25
#define BIN1  26
#define BIN2  27
#define PWMB  14
#define STBY  21

#define CIN1  13
#define CIN2  23
#define PWMC  22
#define STBYC 19

// Potentiometer feedback, one per axis. Must be ADC1 - ADC2 is unusable while
// WiFi is up. 34/36/39 are input-only pins, which suits an analog input exactly
// and costs no output GPIO, so nothing already wired has to move. The I2S mic
// data line moves to GPIO 15 when the audio phase arrives.
#define POT_UPPER 34
#define POT_LOWER 36
#define POT_EYES  39

// Keep external pulldowns on STBY and STBYC. Between reset and the first line
// of setup() these float, and a floating standby pin can twitch a motor.

const int offsetA = 1;
const int offsetB = 1;
const int offsetC = 1;

Motor upperMouth = Motor(AIN1, AIN2, PWMA, offsetA, STBY);
Motor lowerMouth = Motor(BIN1, BIN2, PWMB, offsetB, STBY);
Motor eyes       = Motor(CIN1, CIN2, PWMC, offsetC, STBYC);

// The bear's motors are 3 V. With a 1S LiPo on VM (3.7-4.2 V) and ~0.4 V lost
// across the bridge, duty 200/255 puts roughly 2.6-3.0 V on the winding, which is
// the target. Raise VM and this must come down to match, or the motors cook.
//
// travel_ms is only meaningful against a fixed duty, so calibration and playback
// must agree on RUN_DUTY.
static const int      RUN_DUTY  = 200;

// Homing deliberately stalls into a hard stop and holds there for the remainder
// of HOME_MS, so it runs softer than RUN_DUTY: stall current scales with applied
// voltage, and this is the only place the motor is stalled on purpose. Still far
// above min_duty, so it always reaches the stop.
static const int      HOME_DUTY = 150;

// Timed homing needs HOME_MS to exceed full travel at HOME_DUTY from anywhere.
// Measured travel on the eyes is ~2250ms at duty 200, so ~3000ms at duty 150 -
// the old 900ms silently homed to the middle. Only used for axes with no pot.
static const uint16_t HOME_MS   = 4000;

// Pot-based homing: drive until the reading stops changing. STOP_EPS is above
// the ADC noise floor (~9 counts observed), STOP_HOLD is how long it must sit
// still to count as arrived.
static const int      STOP_EPS      = 12;
static const uint16_t STOP_HOLD     = 180;
static const uint16_t HOME_TIMEOUT  = 8000;

// Never command the mechanical limits. The calibrated endpoints ARE the stops,
// so 0% and 100% would drive into them and stall. Percentages map onto a range
// inset from each stop. Per-axis and NVS-backed, because each linkage binds at a
// different point and this is exactly the sort of constant you want to tune by
// ear without a reflash.
static const uint8_t  MARGIN_DEFAULT = 8;

enum { AX_UPPER = 0, AX_LOWER, AX_EYES, AX_JAW, AX_COUNT };

struct Axis {
  const char *name;
  char        tok;       // console token
  int8_t      closeDir;  // sign that drives toward the home stop
  uint16_t    minDuty;   // stiction threshold, hand-tuned
  uint16_t    travelMs;  // home stop -> far stop at RUN_DUTY
  int32_t     posMs;     // dead-reckoned position, ms from datum
  int8_t      potPin;    // ADC1 pin for position feedback, -1 if none
  int16_t     adcHome;   // reading at the home stop
  int16_t     adcFar;    // reading at the far stop
  uint8_t     marginPct; // inset from each stop, percent of span
};

// AX_JAW is virtual: it drives the two mouth motors in opposition, the way
// openMouth()/closeMouth() do in TeddyRuxbin.ino.
Axis axes[AX_COUNT] = {
  { "upper", 'u', -1, 60, 0, 0, POT_UPPER, -1, -1, MARGIN_DEFAULT },
  { "lower", 'l', +1, 60, 0, 0, POT_LOWER, -1, -1, MARGIN_DEFAULT },
  { "eyes",  'e', -1, 60, 0, 0, POT_EYES,  -1, -1, MARGIN_DEFAULT },
  { "jaw",   'j', -1, 60, 0, 0, -1,        -1, -1, MARGIN_DEFAULT },  // virtual axis, no sensor
};

// The ADC is noisy enough that a single sample jitters by tens of counts.
// Averaging 16 costs ~1ms and makes the reading steady enough to compare.
static int readPot(uint8_t ax) {
  int pin = axes[ax].potPin;
  if (pin < 0) return -1;
  uint32_t acc = 0;
  for (uint8_t i = 0; i < 16; i++) acc += analogRead(pin);
  return acc / 16;
}

Preferences nvs;

// ---------------------------------------------------------------- primitives

static void rawDrive(uint8_t ax, int dir, int duty) {
  if (duty > 255) duty = 255;
  if (duty < 0)   duty = 0;
  switch (ax) {
    case AX_UPPER: upperMouth.drive(dir * duty); break;
    case AX_LOWER: lowerMouth.drive(dir * duty); break;
    case AX_EYES:  eyes.drive(dir * duty);       break;
    case AX_JAW:   upperMouth.drive(dir * duty);
                   lowerMouth.drive(-dir * duty); break;
  }
}

// Motors couple noise into the console RX line - the first stiction ramp showed
// framing errors mid-transmission. Garbage bytes cannot execute (indented input
// is dropped) but they still generate prompts, so discard whatever arrived while
// the motors were running.
static void flushInput() {
  delay(30);
  while (Serial.available()) Serial.read();
}

static void rawBrake(uint8_t ax) {
  switch (ax) {
    case AX_UPPER: upperMouth.brake(); break;
    case AX_LOWER: lowerMouth.brake(); break;
    case AX_EYES:  eyes.brake();       break;
    case AX_JAW:   upperMouth.brake(); lowerMouth.brake(); break;
  }
}

// A duty that will actually move this axis. min_duty is the measured breakaway;
// base is the nominal duty for the operation. The mouth motors break away near
// the top of the range, so a fixed nominal below that silently does nothing.
// Axes whose breakaway is low (the eyes) keep the nominal duty untouched.
static const int DUTY_HEADROOM = 25;

static int dutyFor(uint8_t ax, int base) {
  int d = axes[ax].minDuty + DUTY_HEADROOM;
  if (d < base) d = base;
  if (d > 255)  d = 255;
  return d;
}

// Movement smaller than this is noise, not travel.
static const int      STOP_MIN_TRAVEL = 40;   // counts
static const uint16_t START_GRACE     = 900;  // ms allowed to break away
static const int      NO_MOTION       = -1;   // driveToStop: never left the start

// Drive to a stop and terminate on arrival rather than on a timer. Returns the
// reading at the stop, or NO_MOTION if the axis never moved at all.
// dir +1 drives away from home, -1 toward it.
static int driveToStop(uint8_t ax, int dir) {
  Axis &a = axes[ax];
  rawDrive(ax, dir, dutyFor(ax, HOME_DUTY));
  int start = readPot(ax);
  int last  = start;
  uint32_t t0 = millis(), stable = millis();
  bool moved = false;
  while (millis() - t0 < HOME_TIMEOUT) {
    delay(30);
    int v = readPot(ax);
    if (abs(v - last) > STOP_EPS) { last = v; stable = millis(); }
    if (abs(v - start) > STOP_MIN_TRAVEL) moved = true;
    // "Arrived" only counts once the axis has actually left where it started.
    // A motor below its breakaway duty holds a dead-steady reading too, and
    // without this that is indistinguishable from sitting against the stop.
    if (moved) { if (millis() - stable > STOP_HOLD) break; }
    else if (millis() - t0 > START_GRACE) break;
  }
  rawBrake(ax);
  delay(200);
  return moved ? readPot(ax) : NO_MOTION;
}

// The datum. With a pot this terminates on arrival; without one it falls back to
// stalling for HOME_MS, which must exceed full travel.
static void gotoPctPot(uint8_t ax, uint8_t pct, int tolerance = 25, bool verbose = true);

static void homeAxis(uint8_t ax) {
  Axis &a = axes[ax];
  // Once calibrated, the pot already says where we are - drive to the inset
  // home position under control instead of stalling into the stop.
  if (a.potPin >= 0 && a.adcHome >= 0 && a.adcFar >= 0) {
    gotoPctPot(ax, 0);
    a.posMs = 0;
    return;
  }
  if (a.potPin >= 0) {
    uint32_t t0 = millis();
    int at = driveToStop(ax, a.closeDir);
    if (at == NO_MOTION) {
      Serial.printf("  %-5s DID NOT MOVE - not homed. Run 'stiction %c' first.\n",
                    a.name, a.tok);
      return;
    }
    a.posMs = 0;
    Serial.printf("  %-5s homed in %lums, adc %d\n",
                  a.name, (unsigned long)(millis() - t0), at);
  } else {
    rawDrive(ax, a.closeDir, dutyFor(ax, HOME_DUTY));
    delay(HOME_MS);
    rawBrake(ax);
    a.posMs = 0;
    Serial.printf("  %-5s homed (timed), datum = 0\n", a.name);
  }
}

// dir: +1 away from the home stop, -1 toward it.
static void moveMs(uint8_t ax, int dir, uint16_t ms, int duty) {
  Axis &a = axes[ax];
  duty = dutyFor(ax, duty);
  rawDrive(ax, dir, duty);
  delay(ms);
  rawBrake(ax);
  a.posMs += dir * (int32_t)ms;
  if (a.posMs < 0) a.posMs = 0;
  if (a.travelMs && a.posMs > a.travelMs) a.posMs = a.travelMs;
}

// Closed-loop move: drive until the pot reaches the target. Duty tapers with
// error, which matters because the mechanism has ~75ms of dead time before it
// responds - full duty right up to the target overshoots every time.
// Percent of the working range -> raw ADC target, insets applied. Factored out
// so a second caller can land on exactly the same numbers gotoPctPot would.
static int pctTarget(uint8_t ax, uint8_t pct, int32_t *spanOut) {
  Axis &a = axes[ax];
  int32_t rawSpan = (int32_t)a.adcFar - a.adcHome;
  int32_t inset   = rawSpan * a.marginPct / 100;
  int32_t safeHome = a.adcHome + inset;      // 0% lands here, clear of the stop
  int32_t safeFar  = a.adcFar  - inset;      // 100% lands here
  int32_t span    = safeFar - safeHome;
  if (spanOut) *spanOut = span;
  return safeHome + (int)(span * pct / 100);
}

static void gotoPctPot(uint8_t ax, uint8_t pct, int tolerance, bool verbose) {
  Axis &a = axes[ax];
  int32_t span;
  int     target = pctTarget(ax, pct, &span);
  int     sign   = (span > 0) ? +1 : -1;      // direction that raises the reading
  // Every observed error was an undershoot, so the taper was bottoming out below
  // what keeps the axis moving. min_duty is breakaway-from-rest; sustaining
  // motion under load needs more, and a narrower band holds duty up for longer.
  int     topD   = dutyFor(ax, RUN_DUTY);
  int     floorD = a.minDuty + 20; if (floorD > topD - 20) floorD = topD - 20;
  int     band   = abs(span) / 8;  if (band < 60) band = 60;
  int     fine   = band / 2;       // inside this, pulse instead of driving on
  const int TOL  = tolerance;

  uint32_t t0 = millis();
  while (millis() - t0 < 8000) {
    int err = target - readPot(ax);
    if (abs(err) <= TOL) break;
    int mag  = abs(err); if (mag > band) mag = band;
    int duty = floorD + (int)((int32_t)(topD - floorD) * mag / band);
    int dir  = (err > 0) ? sign : -sign;
    if (abs(err) < fine) {
      // Close in: drive a short burst, brake, let it settle, then re-measure.
      // Continuous drive here overshoots because the mechanism keeps moving for
      // ~75ms after the duty goes away.
      rawDrive(ax, dir, floorD);
      delay(30);
      rawBrake(ax);
      delay(70);
    } else {
      rawDrive(ax, dir, duty);
      delay(20);
    }
  }
  rawBrake(ax);
  delay(150);
  if (verbose) {
    int landed = readPot(ax);
    Serial.printf("  %s -> %d%%  target %d  landed %d  err %+d\n",
                  a.name, pct, target, landed, landed - target);
  }
}

// Dead reckoning. pct 0 re-homes rather than reckoning, which is what keeps
// drift from accumulating past one sentence (PLAN.md: re-home in the gaps).
static void gotoPct(uint8_t ax, uint8_t pct) {
  Axis &a = axes[ax];
  // A calibrated pot beats dead reckoning outright - no drift, no travel_ms.
  if (a.potPin >= 0 && a.adcHome >= 0 && a.adcFar >= 0) {
    if (pct == 0) { homeAxis(ax); return; }
    gotoPctPot(ax, pct);
    return;
  }
  if (!a.travelMs) { Serial.printf("  %s: travel_ms unset\n", a.name); return; }
  if (pct == 0) { homeAxis(ax); return; }
  if (pct > 100) pct = 100;
  int32_t target = ((int32_t)a.travelMs * pct) / 100;
  int32_t delta  = target - a.posMs;
  if (delta == 0) return;
  moveMs(ax, delta > 0 ? +1 : -1, (uint16_t)abs(delta), RUN_DUTY);
}

// ------------------------------------------------------------- gesture table
// Named primitives as (axis, dir, duty, ms) sequences, const in flash.

// Gestures are now target positions, not timed drives. Each step says where the
// axis should be as a percentage of its working range, and how long to dwell
// once it arrives. Timing is a consequence of the mechanism, not a guess.
struct GStep { uint8_t ax; uint8_t pct; uint16_t dwell; };

// Eyes only for now - the mouth motors are not wired or calibrated yet.
// pct 0 is eyelids down (closed), 100 is fully up.
static const GStep G_NEUTRAL[]   = { {AX_EYES,65,0} };
static const GStep G_BLINK[]     = { {AX_EYES,0,70},  {AX_EYES,65,0} };
static const GStep G_SLOWBLINK[] = { {AX_EYES,0,320}, {AX_EYES,65,0} };
static const GStep G_LOOK_UP[]   = { {AX_EYES,100,600},{AX_EYES,65,0} };
static const GStep G_WIDE[]      = { {AX_EYES,100,750},{AX_EYES,65,0} };
static const GStep G_SLEEPY[]    = { {AX_EYES,38,450},{AX_EYES,62,250},
                                     {AX_EYES,22,650},{AX_EYES,48,350},
                                     {AX_EYES,6,900}, {AX_EYES,58,0} };
static const GStep G_PEEK[]      = { {AX_EYES,6,500}, {AX_EYES,40,260},
                                     {AX_EYES,6,420}, {AX_EYES,65,0} };
static const GStep G_DART[]      = { {AX_EYES,95,180},{AX_EYES,25,180},
                                     {AX_EYES,95,180},{AX_EYES,60,0} };

struct Gesture { const char *name; const GStep *steps; uint8_t n; };

#define G(x) { #x, G_##x, sizeof(G_##x) / sizeof(GStep) }
static const Gesture GESTURES[] = {
  G(NEUTRAL), G(BLINK), G(SLOWBLINK), G(LOOK_UP),
  G(WIDE), G(SLEEPY), G(PEEK), G(DART),
};
#undef G
static const uint8_t N_GESTURES = sizeof(GESTURES) / sizeof(Gesture);

// Gestures want to look right, not to land precisely, so they run with a loose
// tolerance - that skips the slow pulse-and-settle phase and keeps them snappy.
static void runGesture(uint8_t gi) {
  const Gesture &g = GESTURES[gi];
  for (uint8_t i = 0; i < g.n; i++) {
    const GStep &s = g.steps[i];
    Axis &a = axes[s.ax];
    if (a.potPin >= 0 && a.adcHome >= 0 && a.adcFar >= 0) gotoPctPot(s.ax, s.pct, 90, false);
    if (s.dwell) delay(s.dwell);
  }
}

// Quantized jaw, 0..3 - closed / narrow / mid / wide. PLAN.md: open-loop
// timing cannot hold a smooth envelope but is solid for discrete moves.
static const uint8_t JAW_PCT[4] = { 0, 35, 70, 100 };

// -------------------------------------------------------------------- config

static void loadConfig() {
  nvs.begin("teddy", false);
  char key[8];
  for (uint8_t i = 0; i < AX_COUNT; i++) {
    snprintf(key, sizeof(key), "min%u", i);
    axes[i].minDuty = nvs.getUShort(key, axes[i].minDuty);
    snprintf(key, sizeof(key), "trv%u", i);
    axes[i].travelMs = nvs.getUShort(key, 0);
    snprintf(key, sizeof(key), "ah%u", i);
    axes[i].adcHome = nvs.getShort(key, -1);
    snprintf(key, sizeof(key), "af%u", i);
    axes[i].adcFar = nvs.getShort(key, -1);
    snprintf(key, sizeof(key), "mg%u", i);
    axes[i].marginPct = nvs.getUChar(key, MARGIN_DEFAULT);
  }
}

static void saveConfig() {
  char key[8];
  for (uint8_t i = 0; i < AX_COUNT; i++) {
    snprintf(key, sizeof(key), "min%u", i);
    nvs.putUShort(key, axes[i].minDuty);
    snprintf(key, sizeof(key), "trv%u", i);
    nvs.putUShort(key, axes[i].travelMs);
    snprintf(key, sizeof(key), "ah%u", i);
    nvs.putShort(key, axes[i].adcHome);
    snprintf(key, sizeof(key), "af%u", i);
    nvs.putShort(key, axes[i].adcFar);
    snprintf(key, sizeof(key), "mg%u", i);
    nvs.putUChar(key, axes[i].marginPct);
  }
  Serial.println("  saved to NVS");
}

static void showConfig() {
  Serial.println("  axis   tok  min_duty  travel_ms  pos_ms   adc   home    far  marg");
  for (uint8_t i = 0; i < AX_COUNT; i++) {
    Axis &a = axes[i];
    Serial.printf("  %-5s   %c   %8u  %9u  %6ld  ",
                  a.name, a.tok, a.minDuty, a.travelMs, (long)a.posMs);
    if (a.potPin < 0) Serial.printf("   -      -      -  %3u%%\n", a.marginPct);
    else Serial.printf("%4d  %5d  %5d  %3u%%\n",
                       readPot(i), a.adcHome, a.adcFar, a.marginPct);
  }
}

// ------------------------------------------------------------------- console

static int axisFromTok(char c) {
  for (uint8_t i = 0; i < AX_COUNT; i++) if (axes[i].tok == c) return i;
  return -1;
}

static void help() {
  Serial.println(
    "\ncommands (axis = u upper, l lower, e eyes, j jaw)\n"
    "  home [axis]           stall-home; no axis = all\n"
    "  jog <axis> <o|c> <ms> [duty]   drive and brake; use to find travel_ms\n"
    "  travel <axis> <ms>    record travel_ms\n"
    "  duty <axis> <v>       record min_duty\n"
    "  margin <axis> [pct]   inset from the stops; no pct just reports it\n"
    "  end <axis> [home|far] [adc]   set an endpoint; no adc captures where it is now\n"
    "  tune <axis>           interactive: arrows nudge, h/f capture endpoints\n"
    "  stiction <axis>       ramp duty until it moves; any key records it\n"
    "  pos <axis> <pct>      dead-reckon to pct of travel (0 = re-home)\n"
    "  jaw <0-3>             quantized jaw state\n"
    "  pot [axis]            read pots; with an axis, stream 15s and report range\n"
    "  sweep <axis> [duty] [ms]   home, then drive stop-to-stop logging the pot\n"
    "  calib <axis>          visit both stops, record adc endpoints to NVS\n"
    "  g <NAME>              run a gesture\n"
    "  demo                  run every gesture in turn\n"
    "  show / save / help");
}

// With a pot this needs no operator: home, then ramp duty driving away from the
// stop until the reading actually changes. PLAN.md calls this unmeasurable
// without motion sensing - which is exactly what the pot provides.
static void stictionPot(uint8_t ax) {
  Axis &a = axes[ax];
  Serial.println("  homing first...");
  driveToStop(ax, a.closeDir);
  int away = -a.closeDir;
  for (int d = 40; d <= 255; d += 5) {
    int before = readPot(ax);
    rawDrive(ax, away, d);
    delay(200);
    rawBrake(ax);
    delay(150);
    int after = readPot(ax);
    int moved = abs(after - before);
    Serial.printf("    duty %3d  moved %d\n", d, moved);
    if (moved > STOP_EPS * 2) {
      a.minDuty = d;
      Serial.printf("  %s min_duty = %d\n", a.name, d);
      saveConfig();
      return;
    }
  }
  Serial.println("  reached 255 without moving - check the mechanism");
}

// Ramp duty upward in short pulses until the operator sees movement. Fallback
// for axes with no pot; the operator is the sensor.
static void stiction(uint8_t ax) {
  if (axes[ax].potPin >= 0 && axes[ax].adcHome >= 0) { stictionPot(ax); return; }
  Serial.println("  ramping - send any character when it first moves");
  while (Serial.available()) Serial.read();
  for (int d = 40; d <= 255; d += 5) {
    Serial.printf("    duty %d\n", d);
    rawDrive(ax, +1, d); delay(140); rawBrake(ax); delay(120);
    rawDrive(ax, -1, d); delay(140); rawBrake(ax); delay(250);
    if (Serial.available()) {
      while (Serial.available()) Serial.read();
      axes[ax].minDuty = d;
      Serial.printf("  %s min_duty = %d\n", axes[ax].name, d);
      saveConfig();
      return;
    }
  }
  Serial.println("  reached 255 without a stop - check wiring");
}

static void handle(char *line) {
  char *cmd = strtok(line, " ");
  if (!cmd) return;
  char *a1 = strtok(NULL, " ");
  char *a2 = strtok(NULL, " ");
  char *a3 = strtok(NULL, " ");

  if (!strcmp(cmd, "help")) { help(); return; }
  if (!strcmp(cmd, "show")) { showConfig(); return; }
  if (!strcmp(cmd, "save")) { saveConfig(); return; }

  if (!strcmp(cmd, "home")) {
    if (!a1) { for (uint8_t i = 0; i < AX_COUNT - 1; i++) homeAxis(i); axes[AX_JAW].posMs = 0; }
    else { int ax = axisFromTok(a1[0]); if (ax < 0) { Serial.println("  bad axis"); return; } homeAxis(ax); }
    return;
  }
  if (!strcmp(cmd, "g")) {
    if (!a1) { Serial.println("  need a name"); return; }
    for (uint8_t i = 0; i < N_GESTURES; i++)
      if (!strcasecmp(a1, GESTURES[i].name)) { runGesture(i); flushInput(); return; }
    Serial.print("  gestures:");
    for (uint8_t i = 0; i < N_GESTURES; i++) Serial.printf(" %s", GESTURES[i].name);
    Serial.println();
    return;
  }
  if (!strcmp(cmd, "demo")) {
    for (uint8_t i = 0; i < N_GESTURES; i++) {
      Serial.printf("  %s\n", GESTURES[i].name);
      runGesture(i);
      flushInput();
      delay(700);
    }
    Serial.println("  done");
    return;
  }
  if (!strcmp(cmd, "calib")) {
    if (!a1) { Serial.println("  need an axis"); return; }
    int cax = axisFromTok(a1[0]);
    if (cax < 0 || axes[cax].potPin < 0) { Serial.println("  bad axis"); return; }
    Axis &ca = axes[cax];
    Serial.println("  to the home stop...");
    int cHome = driveToStop(cax, ca.closeDir);
    Serial.printf("  home adc %d\n", cHome);
    Serial.println("  to the far stop...");
    int cFar = driveToStop(cax, -ca.closeDir);
    Serial.printf("  far  adc %d\n", cFar);
    // Never persist an endpoint the axis did not actually travel to. A motor
    // below its breakaway duty reports a rock-steady reading at both "stops",
    // which used to save a zero-span calibration and report success.
    if (cHome == NO_MOTION || cFar == NO_MOTION ||
        abs(cFar - cHome) < STOP_MIN_TRAVEL * 2) {
      Serial.printf("  %s: no real travel - endpoints NOT saved\n", ca.name);
      Serial.printf("  run 'stiction %c' first; duty may be below breakaway\n", ca.tok);
      return;
    }
    ca.adcHome = cHome;
    ca.adcFar  = cFar;
    Serial.printf("  %s span %d counts\n", ca.name, abs(ca.adcFar - ca.adcHome));
    saveConfig();
    Serial.println("  backing off the stop...");
    gotoPctPot(cax, 0);
    return;
  }
  if (!strcmp(cmd, "sweep")) {
    if (!a1) { Serial.println("  need an axis"); return; }
    int sax = axisFromTok(a1[0]);
    if (sax < 0 || axes[sax].potPin < 0) { Serial.println("  bad axis"); return; }
    int      sduty = a2 ? atoi(a2) : RUN_DUTY;
    uint16_t sms   = a3 ? atoi(a3) : 1500;
    Axis &sa = axes[sax];

    Serial.println("  to the home stop...");
    rawDrive(sax, sa.closeDir, HOME_DUTY);
    delay(HOME_MS);
    rawBrake(sax);
    delay(250);
    int atClose = readPot(sax);
    Serial.printf("  home stop adc %d\n", atClose);

    Serial.println("  sweeping - ms,adc");
    int lo = 4095, hi = 0;
    rawDrive(sax, -sa.closeDir, sduty);
    uint32_t t0 = millis();
    while (millis() - t0 < sms) {
      int v = readPot(sax);
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      Serial.printf("  %lu,%d\n", (unsigned long)(millis() - t0), v);
      delay(25);
    }
    rawBrake(sax);
    delay(250);
    int atOpen = readPot(sax);
    Serial.printf("  far stop adc %d\n", atOpen);
    Serial.printf("  %s: %d -> %d, span %d, seen %d..%d\n",
                  sa.name, atClose, atOpen, abs(atOpen - atClose), lo, hi);
    return;
  }
  if (!strcmp(cmd, "pot")) {
    if (!a1) {
      for (uint8_t i = 0; i < AX_COUNT; i++) {
        if (axes[i].potPin < 0) continue;
        Serial.printf("  %-5s gpio %2d   adc %4d\n",
                      axes[i].name, axes[i].potPin, readPot(i));
      }
      return;
    }
    int pax = axisFromTok(a1[0]);
    if (pax < 0 || axes[pax].potPin < 0) { Serial.println("  bad axis"); return; }
    Serial.println("  streaming 15s - turn the mechanism by hand");
    int lo = 4095, hi = 0, last = -999;
    uint32_t t0 = millis();
    while (millis() - t0 < 15000) {
      int v = readPot(pax);
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      // Only print on real movement, so the log shows the sweep rather than
      // hundreds of identical lines.
      if (abs(v - last) > 8) { Serial.printf("    adc %4d\n", v); last = v; }
      delay(50);
    }
    Serial.printf("  %s range %d .. %d  (span %d of 4095)\n",
                  axes[pax].name, lo, hi, hi - lo);
    return;
  }
  if (!strcmp(cmd, "jaw")) {
    if (!a1) { Serial.println("  need 0-3"); return; }
    int s = atoi(a1);
    if (s < 0 || s > 3) { Serial.println("  need 0-3"); return; }
    gotoPct(AX_JAW, JAW_PCT[s]);
    return;
  }

  int ax = a1 ? axisFromTok(a1[0]) : -1;
  if (ax < 0) { Serial.println("  bad or missing axis"); return; }

  if (!strcmp(cmd, "stiction")) { stiction(ax); return; }
  if (!strcmp(cmd, "travel")) {
    if (!a2) { Serial.println("  need ms"); return; }
    axes[ax].travelMs = atoi(a2); saveConfig(); return;
  }
  if (!strcmp(cmd, "tune")) {
    // Interactive endpoint tuning. Arrow keys arrive as ESC [ A / ESC [ B, so
    // this reads characters rather than lines. w/s work too, which keeps it
    // usable from a line-buffered terminal.
    Axis &ta = axes[ax];
    if (ta.potPin < 0) { Serial.println("  axis has no pot"); return; }
    int step = 60;
    Serial.printf("  tuning %s - up/down or w/s nudge, h=set home, f=set far,\n"
                  "  +/- step size, q to quit.  step %dms  adc %d\n",
                  ta.name, step, readPot(ax));
    for (;;) {
      while (!Serial.available()) delay(5);
      int c = Serial.read();
      int act = 0;                       // +1 open, -1 close
      if (c == 0x1B) {
        uint32_t t = millis();
        while (!Serial.available() && millis() - t < 60) delay(1);
        if (!Serial.available()) { Serial.println("  done"); return; }
        if (Serial.read() != '[') continue;
        t = millis();
        while (!Serial.available() && millis() - t < 60) delay(1);
        if (!Serial.available()) continue;
        int c3 = Serial.read();
        if (c3 == 'A') act = +1;
        else if (c3 == 'B') act = -1;
        else continue;
      }
      else if (c == 'w' || c == 'W') act = +1;
      else if (c == 's' || c == 'S') act = -1;
      else if (c == 'q' || c == 'Q') { Serial.println("  done"); return; }
      else if (c == 'h' || c == 'H') {
        ta.adcHome = readPot(ax); saveConfig();
        Serial.printf("  home = %d  (span %d)\n", ta.adcHome, abs(ta.adcFar - ta.adcHome));
        continue;
      }
      else if (c == 'f' || c == 'F') {
        ta.adcFar = readPot(ax); saveConfig();
        Serial.printf("  far  = %d  (span %d)\n", ta.adcFar, abs(ta.adcFar - ta.adcHome));
        continue;
      }
      else if (c == '+' || c == '=') { step *= 2; if (step > 500) step = 500;
        Serial.printf("  step %dms\n", step); continue; }
      else if (c == '-' || c == '_') { step /= 2; if (step < 10) step = 10;
        Serial.printf("  step %dms\n", step); continue; }
      else continue;                     // ignore CR/LF and anything else

      rawDrive(ax, act > 0 ? -ta.closeDir : ta.closeDir, RUN_DUTY);
      delay(step);
      rawBrake(ax);
      delay(90);
      Serial.printf("  %s adc %d\n", act > 0 ? "up  " : "down", readPot(ax));
    }
  }
  if (!strcmp(cmd, "end")) {
    // end <axis>                 report both endpoints
    // end <axis> home|far        capture the CURRENT reading as that endpoint
    // end <axis> home|far <adc>  set it explicitly
    Axis &ea = axes[ax];
    if (!a2) {
      Serial.printf("  %s home %d  far %d  (now %d)\n",
                    ea.name, ea.adcHome, ea.adcFar, readPot(ax));
      return;
    }
    if (ea.potPin < 0) { Serial.println("  axis has no pot"); return; }
    bool far = (a2[0] == 'f');
    int  v   = a3 ? atoi(a3) : readPot(ax);
    if (v < 0 || v > 4095) { Serial.println("  0-4095"); return; }
    if (far) ea.adcFar = v; else ea.adcHome = v;
    saveConfig();
    Serial.printf("  %s %s = %d   (home %d, far %d, span %d)\n",
                  ea.name, far ? "far" : "home", v,
                  ea.adcHome, ea.adcFar, abs(ea.adcFar - ea.adcHome));
    return;
  }
  if (!strcmp(cmd, "margin")) {
    if (!a2) { Serial.printf("  %s margin = %u%%\n", axes[ax].name, axes[ax].marginPct); return; }
    int m = atoi(a2);
    if (m < 0 || m > 40) { Serial.println("  0-40"); return; }
    axes[ax].marginPct = m;
    saveConfig();
    Serial.printf("  %s margin = %d%%\n", axes[ax].name, m);
    return;
  }
  if (!strcmp(cmd, "duty")) {
    if (!a2) { Serial.println("  need a value"); return; }
    axes[ax].minDuty = atoi(a2); saveConfig(); return;
  }
  if (!strcmp(cmd, "pos")) {
    if (!a2) { Serial.println("  need pct"); return; }
    gotoPct(ax, atoi(a2));
    flushInput();
    Serial.printf("  %s pos_ms = %ld\n", axes[ax].name, (long)axes[ax].posMs);
    return;
  }
  if (!strcmp(cmd, "jog")) {
    if (!a2 || !a3) { Serial.println("  need <o|c> <ms>"); return; }
    int dir  = (a2[0] == 'o') ? +1 : -1;
    int ms   = atoi(a3);
    char *a4 = strtok(NULL, " ");
    int duty = a4 ? atoi(a4) : RUN_DUTY;
    moveMs(ax, dir, ms, duty);
    flushInput();
    Serial.printf("  %s pos_ms = %ld\n", axes[ax].name, (long)axes[ax].posMs);
    return;
  }
  Serial.println("  unknown - type help");
}

void setup() {
  Serial.begin(115200);
  delay(300);
  // Full-scale attenuation so the wiper's 0.3-2.6V swing fits the input range.
  analogSetAttenuation(ADC_11db);
  Serial.println("\nTeddy Ruxpin - phase 1 calibration");
  loadConfig();
  showConfig();
  help();
  // Homing assumes each axis's closeDir actually drives toward the closed stop.
  // Until the motor leads are known-good that is a guess, and a wrong guess homes
  // into the *open* stop and calls it zero. So the first boot after wiring can skip
  // it and jog by hand instead.
  Serial.println("\nhoming in 3s - send any key to skip and check directions first");
  uint32_t t0 = millis();
  bool skip = false;
  while (millis() - t0 < 3000) {
    if (Serial.available()) { while (Serial.available()) Serial.read(); skip = true; break; }
    delay(20);
  }
  if (skip) {
    Serial.println("skipped - NOT homed, pos_ms is meaningless until you run home");
  } else {
    for (uint8_t i = 0; i < AX_COUNT - 1; i++) {
      Axis &a = axes[i];
      // A calibrated axis knows where it is the moment the ADC is read. No
      // stall-homing, no movement on boot at all.
      if (a.potPin >= 0 && a.adcHome >= 0 && a.adcFar >= 0) {
        Serial.printf("  %-5s already known, adc %d\n", a.name, readPot(i));
      } else {
        homeAxis(i);
      }
    }
    axes[AX_JAW].posMs = 0;
  }
  Serial.print("> ");
}

void loop() {
  static char buf[64];
  static uint8_t n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[n] = 0;
      // Echo guard. Every line this firmware prints starts with whitespace;
      // nothing typed at a prompt does. If TX ever finds its way back into RX -
      // a wiring fault, a terminal echoing - output would otherwise parse as
      // commands and drive motors on its own. Silently drop indented input.
      if (n && buf[0] != ' ' && buf[0] != '\t') handle(buf);
      n = 0;
      Serial.print("> ");
    }
    else if (n < sizeof(buf) - 1) buf[n++] = c;
  }
}
