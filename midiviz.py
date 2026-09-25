#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""midiviz — the MIDI stream as a picture, not a transcript.

`midimon.py` prints one line per event ("14:0  Control change  ch0, controller
29, value 15"). That is the right shape for a debugger and the wrong shape for a
performer: during a set nobody reads sentences, and a fader sweep emits ~400
events a second, so the words scroll past faster than an eye can land on them.
PLN, 2026-08-28: "midiwatch still naively verbose ... no more console, tiny
borderless window ... no word like 'control change' pure viz".

So this is a *lens*, not a log. Zero English words render: an event's identity is
its POSITION, its type is a GLYPH CLASS, its value is a BAR plus two hex digits,
and its recency is BRIGHTNESS. The layout is the authored surface itself —
`lcxl_grid` rows A..F down, columns 1..8 across, and column N *is* orbit N — so a
glance answers "which orbit did I just touch" without decoding anything. Only
what the grid cannot place (foreign CCs, notes, bend, program) falls as rain in
the right-hand gutter, which is itself the signal "that came from somewhere
else".

    python3 midiviz.py                  # auto-resolve the surface, watch it
    python3 midiviz.py -p 28:0          # pin a source port
    python3 midiviz.py -l               # list ports, exit
    python3 midiviz.py --theme sun      # near-white, heavy ink, for playing outdoors
    python3 midiviz.py --selftest       # 3 s of synthetic events, headless
    python3 midiviz.py --spectro        # + the master-bus spectrum, behind

Keys: q / Esc quit · p pause · c clear · s spectrum · t pin · d theme ·
+/- type density · 0 back to the birth size.
Drag the body to move, the edges and corners to resize (the type follows the
window); RIGHT-CLICK for the menu (also in the system tray).

Design notes worth keeping
--------------------------
* **Mapped-but-idle must not be black.** Every one of the 48 grid cells is
  painted with a floor tint even when nothing has ever arrived on it, because
  "dark = not mapped" is the reading a cockpit trains you into, and a dark cell
  that actually *is* mapped reads as a hardware fault (the LED work settled
  this). On a pale theme the same rule reads **must not be invisible**: the
  floor becomes a wash *below* the page white rather than a glow above the
  black, and `test_mapped_but_idle_is_visible_on_every_theme` holds every
  theme to it.
* **Three themes, and the colours are data.** `dark` is the cockpit and is
  bit-for-bit what it was before themes existed; `light` reads on a pale
  desktop; `sun` is near-white with near-black ink at every decay level, thick
  bars, a bold face and no CRT texture, for a laptop on a table at noon. The
  split that makes this work is INK (recency: digits, bars, pens) versus
  SURFACE (the cell bodies) — see the THEMES block. Everything resolves into
  the same precomputed LUTs, once per theme change; a frame still constructs no
  QColor.
* **One menu, three ways to reach it, one implementation.** Right-click the
  window, or use the tray icon: same QMenu, and every entry calls the method
  the keyboard already called. The menu is discoverability for what exists, so
  no entry gets its own copy of an action and the keys stay the tested path
  (real `setShortcut`s were left out on purpose — Qt would intercept the key
  before `keyPressEvent`, moving every key onto a path `--selftest` does not
  drive).
* **The window's SIZE is the window manager's; its DENSITY is the window's.**
  The scale knob used to be both, so any geometry a tiling WM handed out was
  thrown away by the next theme cycle. Now the type is sized from the real
  `width()`/`height()` (a fit factor against the size the window was born at),
  the knob multiplies that and resizes nothing, and the four edges and corners
  are grab zones. Only startup and "Reset size" ever call `resize()` — see the
  FIT_MIN block.
* **Theme and scale survive a restart**, in `$XDG_CONFIG_HOME/parvagues/
  midiviz.json`. Every read and write is wrapped and the loader cannot hand
  back anything unusable: a choice you have to re-make each launch is a choice
  you do not have, and a window that will not start because that file is
  half-written is worse than one with no memory at all.
* **Two frame rates, not a busy loop.** This machine performs live audio. The
  timer runs at ~30 fps only while events are arriving; 2 s after the last one it
  drops to 5 fps and the picture becomes a slow dim pulse. Colours are a
  precomputed LUT and paint uses the int overloads, so a frame allocates
  essentially nothing.
* **The reader is a thread over `aseqdump`, reusing `midimon.parse_line` +
  `enrich`.** One parsing source of truth for the CLI, the web SSE stream and
  this window. rtmidi cannot subscribe to an arbitrary ALSA source port; the
  subprocess can.
* Port choice reuses `surface.resolve_port` (lowest port number on a matching
  client — the LCXL's second port is the HUI one and carries nothing) tried
  against `WATCH_PREFERENCE` in order.
"""
from __future__ import annotations

import argparse
import json
import math
import fcntl
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
for _p in (str(HERE), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lcxl_grid as grid          # noqa: E402  the authored control grid
import midimon                    # noqa: E402  parse_line / enrich / resolve_port
import midistream                 # noqa: E402  parse_ports
import oscfeed                    # noqa: E402  the Tidal HL feed (SuperDirt mirror)

# ── brand — whose lens this is ─────────────────────────────────────────────
# midiviz was born inside ParVagues (a livecoding rig) and ships wearing it;
# nothing here NEEDS that name. Override the three at launch to wear your own:
#
#     MIDIVIZ_BRAND="Coolnaut" MIDIVIZ_WORDMARK="COOLNAUT" \
#     MIDIVIZ_BRAND_SLUG="coolnaut" python3 midiviz.py
#
# * BRAND is the spoken name (menu labels, tray presence);
# * WORDMARK is the block lettering drawn over row D (upper-case on purpose —
#   that is what the vendored display face is for, see the SYNE_PATH note);
# * BRAND_SLUG is the directory under $XDG_CONFIG_HOME and $XDG_RUNTIME_DIR
#   where THIS instance keeps its memory and latches — one per brand, so two
#   brands never fight over one saved theme.
# The watermarked wave swaps the same way: MIDIVIZ_WAVE=/path/to/logo.png
# (any transparent PNG; it is painted faint enough to never compete with a
# digit — see the WAVE_PATH note below).
# What branding does NOT touch: the wire paths midiviz READS
# (`~/.cache/parvagues/surface-state.json`, `eval-events.jsonl`) are contracts
# with the producers that write them — a driver, an editor — not decoration.
# Where those live is documented in the README's "wire" section.
BRAND = os.environ.get("MIDIVIZ_BRAND", "ParVagues")
WORDMARK = os.environ.get("MIDIVIZ_WORDMARK", "PARVAGUES")
BRAND_SLUG = os.environ.get("MIDIVIZ_BRAND_SLUG", "parvagues")

# ── which port to watch ────────────────────────────────────────────────────
# `midimon.resolve_port()` OWNS this decision and this file delegates to it. Two
# things about it are load-bearing and were nearly re-derived wrong here:
#
#   * the preference ORDER is authored (translated corpus stream first, raw v3
#     DAW port second, raw custom mode third, Midi Through last as a catch-all);
#   * it matches the whole `aseqdump -l` ROW, not just the CLIENT column —
#     python-rtmidi registers the lcxl3 driver's client as 'RtMidiOut Client'
#     and puts 'ParVagues LCXL3' on the PORT, so a client-name match (which is
#     what `surface.resolve_port` does, correctly, for its own purpose) misses
#     exactly the stream we most want to watch.
#
# The fallback below exists only for a tree whose midimon predates that helper;
# it is a faithful copy, so the two can never give DIFFERENT answers to "which
# port", which is the failure mode worth engineering against.
WATCH_PREFERENCE: tuple[str, ...] = (
    "ParVagues LCXL3", "LCXL3 1 DAW", "LCXL3", "Launch Control XL", "Midi Through",
)

# ── unplug/replug survival ─────────────────────────────────────────────────
# 2026-09-05: midiviz resolved its source port ONCE at startup, and when the
# LCXL was unplugged `aseqdump`'s stdout simply hit EOF (the ALSA client it
# was subscribed to vanished), the reader thread returned, and `app.exec()`
# had nothing keeping it alive -- the window closed with exit 0. Because that
# is a CLEAN exit, `Restart=on-failure` never fired: the rig's #1 recurring
# failure mode ("stale binding pattern" -- a binding resolved once, killed by
# a replug, never re-resolved) applied to the window itself, not just to a
# port variable.
#
# The fix mirrors `lcxl-leds.py`'s `find_seq_port`/`invalidate_ports`: never
# trust a resolved port past the moment it might have gone stale. A QTimer
# every RECONNECT_MS (matching `midi-autoconnect.sh`'s own reconcile cadence)
# re-resolves from scratch and swaps the Reader in place; the window and its
# QApplication never see a reason to exit.
#
# The re-resolve is gated on HARDWARE presence, checked independently of
# WATCH_PREFERENCE's name match. Reason: `lcxl3-driver.service` publishes a
# VIRTUAL ALSA port named literally 'ParVagues LCXL3' -- the translated,
# corpus-numbered stream `resolve_watch_port()` prefers ON PURPOSE, because
# that is the numbering the grid and every `.tidal` file actually speak (see
# `tools/lcxl3-driver.py` and the WATCH_PREFERENCE comment above). But that
# virtual client can outlive the physical unplug for a beat if the driver
# hasn't noticed yet, and a pure name match would then report "still
# connected" while the port is a ghost carrying nothing -- exactly the trap
# `midi-autoconnect.sh`'s `DIRECT_LEG_AWK` was written to avoid for its own
# purpose (`hw = ($0 ~ /type=kernel/ && $0 ~ /Launch Control XL|LCXL/)`).
# Reusing that discrimination here: `_hardware_present()` requires a
# `type=kernel` client whose name matches the board, and the rebind tick
# treats the source as gone whenever that is false, regardless of what
# WATCH_PREFERENCE would otherwise resolve to. Content still comes from the
# preferred (possibly virtual/translated) port; liveness is judged by
# hardware, so a lingering ghost can no longer read as "connected".
RECONNECT_MS = 2000
HW_NAME_RE = re.compile(r"launch\s*control\s*xl|\blcxl\d*\b", re.I)


def _hardware_present() -> bool:
    """True iff a REAL (kernel-backed) LCXL client is on the ALSA seq bus.

    Independent of `resolve_watch_port()` on purpose -- see the module note
    above. `aconnect -l`'s client header line carries both the type tag and
    the name, e.g. `client 20: 'LCXL3 1' [type=kernel]`.
    """
    try:
        out = subprocess.run(["aconnect", "-l"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.splitlines():
        if (line.startswith("client ") and "type=kernel" in line
                and HW_NAME_RE.search(line)):
            return True
    return False


def _port_listed(pid: str) -> bool:
    """Is this exact CLIENT:PORT address still in `aseqdump -l`'s listing?

    Used only for a user-pinned `-p` port: a replug can renumber the address
    (20:0 -> 24:0 is the recorded history -- see reference_lcxl_led_stall), so
    a pin surviving a replug is not guaranteed, but this at least notices when
    the pinned address itself has gone away rather than silently reading a
    dead port forever.
    """
    return any(p["addr"] == pid for p in list_ports())


def list_ports() -> list[dict]:
    """[{addr, client, port}] from `aseqdump -l`, or [] if aseqdump/ALSA is absent."""
    try:
        r = subprocess.run(["aseqdump", "-l"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return midistream.parse_ports(r.stdout)


def resolve_watch_port() -> tuple[str | None, str]:
    """(port_id, human label) of the most interesting source present."""
    delegate = getattr(midimon, "resolve_port", None)
    if callable(delegate):
        try:
            return delegate()
        except TypeError:            # a different signature is a different concept
            pass
    try:
        out = subprocess.run(["aseqdump", "-l"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None, "aseqdump unavailable"
    rows = []
    for line in out.splitlines():
        tok = line.split()
        if tok and ":" in tok[0] and tok[0].split(":")[0].isdigit():
            rows.append((tok[0], line))
    for want in WATCH_PREFERENCE:
        for pid, line in rows:
            if want in line:
                return pid, "%s · %s" % (pid, want)
    return None, "no MIDI source found"


# ── event classes → glyphs (no words, ever) ────────────────────────────────
# Restricted to glyphs with reliable coverage in DejaVu Sans Mono, the
# guaranteed fallback face on this box.
HEX = "0123456789ABCDEF"
CLASS_GLYPH: tuple[tuple[str, str], ...] = (
    ("note on", "◆"),               # ◆ solid  = a note exists
    ("note off", "◇"),              # ◇ hollow = it stopped
    ("control", "▪"),               # ▪ (only reached for un-gridded CCs)
    ("pitch", "≈"),                 # ≈
    ("channel aftertouch", "▴"),    # ▴
    ("poly aftertouch", "▴"),
    ("aftertouch", "▴"),
    ("program", "≡"),               # ≡
    ("sysex", "◈"),                 # ◈
    ("system exclusive", "◈"),
    # NOTE: the four below are currently UNREACHABLE, and that is a midimon bug,
    # not a midiviz one. `parse_line`'s row regex is `addr EVENT <2+ spaces> DATA`,
    # and aseqdump prints Start/Stop/Continue/Clock with no data column at all, so
    # those lines match nothing and are dropped before they ever get here. Kept
    # (and pinned by test_dataless_transport_messages_do_not_parse_at_all) so the
    # day the shared parser learns them, transport lights up for free.
    ("clock", "·"),                 # ·
    ("start", "▶"),                 # ▶
    ("continue", "▷"),              # ▷
    ("stop", "■"),                  # ■
    ("song", "·"),
    ("active", "·"),
    ("reset", "■"),
)
OTHER_GLYPH = "▫"                   # ▫

# Colour families. Every hue stays inside the violet→magenta arc PLN asked for;
# only the hue *within* that arc separates roles, so the window reads as one
# colour while eight things stay distinguishable.
FAM_LEVEL, FAM_FX, FAM_FX2, FAM_GATE, FAM_GATE2, FAM_FAMILY, FAM_OTHER, FAM_NOTE = range(8)
FAM_HUE = (275, 305, 258, 288, 320, 334, 246, 292)
ROLE_FAM = {"level": FAM_LEVEL, "fx": FAM_FX, "fx2": FAM_FX2, "gate": FAM_GATE,
            "gate2": FAM_GATE2, "family_filter": FAM_FAMILY, "family_mute": FAM_FAMILY}
LEVELS = 16                         # decay resolution of the colour LUT

BG = (0x0a, 0x06, 0x12)

# ── themes ─────────────────────────────────────────────────────────────────
# PLN, 2026-09-22: "midimon unusable in the sun in light mode :')".
#
# The window was drawn once, for a dark room, and it is beautiful there. On a
# terrace at 14:00, or on a pale desktop, a cockpit is a black mirror. So the
# colours leave the paint loop and become DATA: three themes, resolved into the
# same precomputed LUTs `_build_palette` always built, exactly once per theme
# change. A frame still constructs no QColor -- that budget is what lets this
# thing run beside live audio and it is not up for renegotiation over legibility.
#
# Two ramps, and the split is the whole idea:
#
#   * **INK** (`self.lut`, `self.chrome`) is everything the eye reads: digits,
#     bars, pens, rain. Its axis is RECENCY, so on a dark theme it climbs from
#     near-black to bright, and on a light one it DARKENS from pale to heavy --
#     "hot" means "further from the page" either way.
#   * **SURFACE** (`self.tint`) is the four cell-body shades. It exists because
#     of the design invariant below, and because sampling the ink ramp for a
#     panel is exactly what makes a light theme unreadable: a panel wants to
#     stay near the page while the ink on it walks away from it.
#
# **Mapped-but-idle must not be black** generalises to **must not be invisible**.
# `tint[fam][0]` is the floor every one of the 48 mapped cells gets before
# anything has ever arrived; on `dark` it is a faint glow above the background,
# on `light`/`sun` a pale wash BELOW the page white. Either way it is a panel you
# can see, because a cell you cannot see reads as a control that is not wired.
#
# `dark` is bit-for-bit the pre-theme look. Its ramps are the literal
# expressions that used to be inlined in `_build_palette`, in that exact form,
# and `test_dark_theme_is_bit_identical_to_the_pre_theme_look` pins it against
# `git show HEAD~`-era values. This is the palette PLN has trained his eye on
# over a dozen sets; a rounding difference in it is a regression nobody can put
# into words and everybody would feel.
class Ramp(NamedTuple):
    """One (value, saturation) curve in HSV over a 0..1 axis.

    `v0 + vspan * t**vg`, which is the shape every colour in the file already
    had. A span is signed on purpose: a light theme's value ramp runs downhill.
    """
    v0: float
    vspan: float
    vg: float
    s0: float
    sspan: float
    sg: float

    def at(self, t: float) -> tuple[float, float]:
        return (self.v0 + self.vspan * (t ** self.vg),
                self.s0 + self.sspan * (t ** self.sg))


class SpecRamp(NamedTuple):
    """The spectrum wash: hue drift, one saturation, a value ramp and an alpha
    ramp. Deliberately not one of the eight families -- see `spec_lut`."""
    h0: float
    hspan: float
    sat: float
    v0: float
    vspan: float
    a0: float
    aspan: float


class Theme(NamedTuple):
    name: str
    bg: tuple[int, int, int]
    ink: Ramp                     # LEVELS shades per family hue
    tint: Ramp                    # the cell-body surface ramp
    tint_t: tuple[float, ...]     # where on `tint`'s axis the 4 shades sit
    chrome: "Ramp | None"         # None = chrome IS the FAM_OTHER ink ramp
    chrome_hue: float
    spec: SpecRamp
    spec_peak: tuple[int, int, int, int]
    # (horizontal, vertical) scanline colours, or None for no CRT texture at
    # all: on near-white a violet haze is dirt on the screen, not atmosphere.
    scan: "tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None"
    bar_frac: float               # value-bar height as a fraction of the cell
    bold: bool                    # a heavier face, for a screen in daylight


# The dark ink ramp, sampled at levels 1..4, IS the dark surface ramp -- which
# is how `tint[fam][i] == lut[fam][1 + i]` comes out bit-identical instead of
# merely close.
_INK_DARK = Ramp(0.055, 0.945, 1.25, 0.96, -0.50, 1.6)
_TINT_T_DARK = tuple(lv / (LEVELS - 1) for lv in (1, 2, 3, 4))
_TINT_T_LINEAR = (0.0, 1 / 3, 2 / 3, 1.0)

THEMES: dict[str, Theme] = {
    "dark": Theme(
        name="dark", bg=BG,
        ink=_INK_DARK, tint=_INK_DARK, tint_t=_TINT_T_DARK,
        chrome=None, chrome_hue=FAM_HUE[FAM_OTHER],
        spec=SpecRamp(0.68, -0.055, 0.80, 0.30, 0.70, 12, 84),
        spec_peak=(0xc8, 0xa8, 0xff, 90),
        scan=((0xa0, 0x60, 0xff, 16), (0xa0, 0x60, 0xff, 9)),
        bar_frac=0.22, bold=False,
    ),
    # Indoors on a pale desktop. Still a lens, still the violet arc, still a
    # decay gradient the eye can follow -- the ink simply runs the other way.
    "light": Theme(
        name="light", bg=(0xf2, 0xf0, 0xf6),
        # `vg` below 1 puts the floor of the decay ramp lower than a linear
        # one would: a cell nobody has touched for six seconds still has to be
        # readable on a pale desktop, and the first pass let it drift into a
        # mid-tone that was fine on the fresh screenshot and thin on the
        # decayed one. Recency reads as 0.54 -> 0.24, which is plenty.
        ink=Ramp(0.64, -0.40, 0.70, 0.50, 0.45, 0.80),
        tint=Ramp(0.90, -0.14, 1.00, 0.14, 0.34, 1.00),
        tint_t=_TINT_T_LINEAR,
        chrome=Ramp(0.66, -0.50, 0.80, 0.08, 0.30, 1.00),
        chrome_hue=258.0,
        spec=SpecRamp(0.62, -0.05, 0.55, 0.62, -0.28, 34, 120),
        spec_peak=(0x4a, 0x2a, 0x78, 110),
        scan=((0x50, 0x3a, 0x78, 10), (0x50, 0x3a, 0x78, 6)),
        bar_frac=0.22, bold=False,
    ),
    # Direct sunlight, and built for nothing else. Near-white page, heavy ink
    # at every decay level (a "dim" state outdoors is a blank state), thicker
    # value bars, and no scanline texture to eat the little contrast there is.
    # It is not meant to be pretty; it is meant to be the one that still works
    # when the laptop is the brightest thing on a table at noon.
    "sun": Theme(
        name="sun", bg=(0xfc, 0xfc, 0xfa),
        # Ink is near-black at EVERY decay level (`vg` below 1 spends the whole
        # ramp in the first tenth of it), because outdoors a dim state is not a
        # faint state, it is a blank one. Recency still reads -- 0.46 down to
        # 0.18 is a real 2.5x -- but nothing on this page is ever pale.
        ink=Ramp(0.46, -0.28, 0.50, 0.86, 0.12, 1.00),
        # And the panels stay near the page, deliberately flatter than
        # `light`'s. The first version let the heat fill climb to a saturated
        # mid-tone and the digits printed on it lost the fight: a cell body is
        # a surface for ink, and in sunlight that is all it gets to be. The
        # value still reads, twice -- the bar and the digits.
        tint=Ramp(0.92, -0.10, 1.00, 0.14, 0.26, 1.00),
        tint_t=_TINT_T_LINEAR,
        chrome=Ramp(0.40, -0.32, 0.50, 0.05, 0.18, 1.00),
        chrome_hue=258.0,
        spec=SpecRamp(0.60, -0.04, 0.72, 0.48, -0.26, 48, 150),
        spec_peak=(0x18, 0x0c, 0x38, 190),
        scan=None,
        bar_frac=0.34, bold=True,
    ),
}
# Cycle order for the `d` key and the menu: darkest to brightest page, so the
# key has a direction and "one more press" means "one step sunnier".
THEME_ORDER: tuple[str, ...] = ("dark", "light", "sun")
DEFAULT_THEME = "dark"
# What each page is FOR, for the menu only. `dark`/`light`/`sun` stay the
# vocabulary of the config file, the `--theme` flag and every test; a menu row
# read in a hurry outdoors gets to say the thing the word is short for.
THEME_MENU: dict[str, str] = {
    "dark": "Dark  ·  the cockpit, indoors",
    "light": "Light  ·  a pale desktop",
    "sun": "Sun  ·  daylight, maximum ink",
}

TAU = 0.85                          # seconds; brightness e-folding time
# PLN, 2026-09-24: the column glow dies noticeably faster than the cells do —
# at 0.85 s it is gone while the last-moved cell is still lit. The column is
# the "this orbit is live" signal, so it keeps its own, 30 % slower curve.
TAU_COL = TAU * 1.3
IDLE_AFTER = 2.0                    # seconds of silence before the slow pulse
# 40 ms = 25 fps. Profiling a frame (see MAX_DROPS) put the cost squarely in the
# number of drawText calls, and at this size 25 fps is indistinguishable from 33
# while costing a fifth less of the core PLN needs for audio.
FPS_ACTIVE, FPS_IDLE = 40, 200      # timer intervals, ms
FPS_SPECTRO = 54                    # ms; ~18 fps, the spectrum's own rate
# Rain is decoration, and it was the single most expensive thing on screen: a
# 96-drop cap with a trail glyph each meant up to 192 drawText per frame, 3.1 ms
# of an 8.9 ms frame. 32 heads with a trail only on the brightest few costs ~0.6.
MAX_DROPS = 32
# One drop per control per DROP_EVERY seconds.
#
# PLN, 2026-09-22: "can the matrix show even the ^42 e.g. as we move it would be
# rad". So a move on a control the grid OWNS now rains that control's corpus
# token -- literally the string a `.tidal` file types, the same one the header
# prints -- where before only CCs outside the grid rained at all.
#
# Rate-gated per CC, and that is not decoration-tidiness: rows B and C are
# relative, so one turn of an encoder is a stream of messages, and ungated a
# single sweep fills all 32 heads with the same token inside half a second. The
# column then reads as a solid block of one word instead of rain, which is the
# opposite of what was asked for.
DROP_EVERY = 0.2
BASE_W, BASE_H = 420, 260

# ── the two backdrops that are pictures, not measurements ─────────────────
# PLN, 2026-09-24: "can we do a test (option menu toggle) with ParVagues
# imagery (transparent parvagues wave) behind the midimon to style it
# definitely ParVagues-y?" — then, after the first cut painted it UNDER the
# matrix: "i dont even see the parvagues wave its behind a grid of grids
# somehow?" He was right, and it was structural, not a bug: the cells' floor
# tints are OPAQUE on purpose (readability first), so anything painted behind
# them survives only in the margins. A backdrop needs translucent layers
# under it; this window has none.
#
# So the wave is a WATERMARK: painted over the finished reading, under the
# scanline texture, faint enough to never compete with a digit. Brand magenta
# is a RESERVED colour in the Ship's Bridge design language, which is exactly
# why it works as atmosphere — nothing else on this canvas may use it, so the
# wash says ParVagues and nothing else. Vendored from the SSOT asset
# (Perso/www/public/images/parvagues/logo_transparent.png, downscaled to
# 640px) because midiviz must run standalone from a checkout with no www
# tree on the box at all.
WAVE_PATH = (Path(os.environ["MIDIVIZ_WAVE"]) if os.environ.get("MIDIVIZ_WAVE")
             else Path(__file__).resolve().parent / "ui" / "parvagues_wave.png")
WAVE_ALPHA = 0.12
# …and its wordmark, over D1-D4 (PLN, 2026-09-24: "PARVAGUES in nice our
# usual site font bloc letters over D1/2/3/4 alongside the wave same
# brighter pink highlight"). The site's display face is Syne (next/font,
# --font-syne, weight 800, tracking 0.15em, uppercase — Hero.js is the
# recipe) and the pink is --neon-high. The variable font's latin subset is
# vendored out of the built site for the same standalone reason as the wave.
SYNE_PATH = Path(__file__).resolve().parent / "ui" / "syne-extrabold.ttf"
WORD_ALPHA = 0.8

# ── the HL feed ────────────────────────────────────────────────────────────
# PLN, 2026-09-24: "please do indeed sub from the events, to indeed feed
# into each cell related, so people get intuitively what controls affect".
# Tidal → SuperDirt /play packets, mirrored by BootTidal.hs to oscfeed's
# port, say which ORBIT the music is triggering right now. The MIDI feed
# says what the surface is doing; this says what the set is doing. On by
# default at runtime (--no-hl for the off switch): a lens that only shows
# the controls was half the picture.
HL_ON_BY_DEFAULT = True

# ── the size belongs to the window manager, the density to the window ──────
# PLN, 2026-09-22: "only drag-> grab atm, can we have corner grabs to make it
# bigger, and have it you know squeeze and expand properly as we make it taller
# or wider, for proper tiling WM integration ?"
#
# `_apply_scale` used to end in `self.resize(BASE_W * scale, BASE_H * scale)`,
# which made the scale knob and the window's SIZE the same number. Under a
# tiling WM that is a fight the compositor loses at the worst moment: it picks
# a geometry, and the next theme cycle snaps the window back to BASE × scale
# (`d` re-measures the fonts whenever the bold weight changes) and throws the
# tile away. Nothing announces it; the window just jumps mid-set.
#
# So the two are separate concerns now:
#
#   * the TYPE follows the real window — a fit factor, min(w/ref, h/ref), so a
#     taller AND wider window gets proportionally bigger glyphs and a squeezed
#     one keeps its layout instead of clipping it. `min` and not an average on
#     purpose: the tight axis is the one that clips, so it is the one that
#     decides;
#   * the SCALE knob is an explicit multiplier on top of that, and resizes
#     nothing. Only the startup geometry and "Reset size" ever call resize().
#
# The reference is the size the window was BORN at (BASE × the remembered
# scale), captured once and never moved: at birth the fit is exactly 1.0, so
# the type is pixel-for-pixel what this file has always drawn at that scale,
# and every reading after that is relative to the size PLN himself chose.
# FIT_MIN is deliberately well below the smallest window the knob can ask for
# (MIN_W/MIN_H is 0.57 of BASE, so at 1.0× the floor is never reached). It earns
# its keep at the OTHER end: at 2.4× with the window crushed into a narrow tile,
# a shallower floor is what lets the squeeze keep winning over the knob, instead
# of printing six rows of 15 px digits on top of each other in 150 px of height.
# Measured at 240×150 × 2.4: 1.32× before, 0.84× after, and `-` or `0` was
# always the way back either way.
FIT_MIN, FIT_MAX = 0.35, 2.4        # how far the window's own size may push the type
DENS_MIN, DENS_MAX = 0.5, 3.0       # and the absolute bounds once the knob is in
# A tile can be narrow; it must not be able to crush the lens into nothing.
# Below BASE × 0.6 (the smallest scale the knob offers) so the startup resize
# is never clamped — a clamped startup would make the density reference
# disagree with the window, and the fit would not be 1.0 at birth.
MIN_W, MIN_H = 240, 150
GRAB_PX = 7                         # the edge resize band, in pixels
CORNER_PX = 20                      # …widened to this at the four corners


def _glyph_for(event: str) -> tuple[str, int]:
    """Event name → (glyph, colour family). The only place a name is inspected."""
    e = (event or "").lower()
    for kw, g in CLASS_GLYPH:
        if e.startswith(kw):
            return g, (FAM_NOTE if kw.startswith("note") else FAM_OTHER)
    return OTHER_GLYPH, FAM_OTHER


def _norm_value(ev: dict) -> int:
    """Any event → 0..127, so one bar/brightness scale serves every type."""
    for key in ("value", "velocity", "program"):
        if key in ev:
            v = ev[key]
            if key == "value" and (v < 0 or v > 127):     # pitch bend, ±8192
                return max(0, min(127, int((v + 8192) * 127 / 16383)))
            return max(0, min(127, int(v)))
    return 64


# ── the reader ─────────────────────────────────────────────────────────────
STATE_FILE = Path(os.path.expanduser("~/.cache/parvagues/surface-state.json"))

# How close to the centre detent still reads as ZERO.
#
# A GUESS, and the one number on this layer that wants a real hand on the board.
# Rows B/C are relative: the driver integrates `v - 64` and CLAMPS
# (`max(0, min(127, cur + d))`), so a knob swept to an end and brought back
# loses every click it spent against the clamp and settles somewhere near 64
# rather than on it. Too tight and a filter his hand calls ZERO draws a sliver
# of LOW; too loose and a real, audible nudge off centre shows as bypass — which
# is the worse error, since the whole layer exists to stop the picture lying
# about rest. Start narrow, widen only if the board says to.
ZERO_ZONE = 2


class SurfaceState:
    """The resting position of the surface, which the event stream cannot carry.

    MIDI is edge-triggered, and this module makes recency the third visual axis
    — so between two movements every cell decays to the same floor tint and a
    DJF resting at zero looks identical to one parked at the top. No palette
    fixes that: an event stream carries CHANGES, and rest is the absence of one.

    Worse, a consumer of the wire alone *cannot* derive it. The LCXL3 has no
    readback, and rows B/C run relative (the surface sends `v - 64`), so the
    hardware holds no absolute position either. Exactly one process integrates
    those deltas and therefore owns the value: `lcxl3-driver`. This reads what
    it publishes.

    Cost at rest is one `stat()` per frame: an unchanged `seq` means zero
    parsing and zero repainting. No socket, no broker, no connection state —
    the payload is ~32 integers with no history worth queueing.

    Absent file, unreadable file, half-written file: all mean "no state", and
    the viewer simply draws what it always drew. The driver may be an older
    build, or not running at all, and neither is an error here.
    """

    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.values: dict[int, int] = {}
        self.seq = -1
        self.track = ""
        self._mtime = -1.0
        self.reads = 0

    def poll(self) -> bool:
        """True when the snapshot changed. Cheap enough to call every frame."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self.values:
                self.values, self.seq = {}, -1
                return True
            return False
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            d = json.loads(self.path.read_text())
            # `null` and `[]` are VALID json of the wrong shape, and the shape
            # check has to come before the first .get — otherwise an
            # AttributeError escapes the handler and takes the window down.
            # A hand-edited file or an older driver is enough to produce one.
            if not isinstance(d, dict):
                return False
            seq = int(d.get("seq", -1))
        except (OSError, ValueError, TypeError):
            return False
        if seq == self.seq:
            return False
        self.seq = seq
        self.track = str(d.get("track", ""))
        vals = d.get("values") or {}
        try:
            self.values = {int(k): int(v) for k, v in vals.items()}
        except (ValueError, TypeError):
            self.values = {}
        self.reads += 1
        return True


class Reader:
    """`aseqdump` in a daemon thread; a bounded deque the GUI drains each frame.

    Bounded, because a live monitor wants the PRESENT: the same lesson
    `midistream.DEPTH` records — a deep buffer that fills stays full, and then
    every frame you draw is a fixed lag behind the surface, forever.
    """

    DEPTH = 512

    def __init__(self, port: str | None):
        self.port = port
        self.error: str | None = None
        self.total = 0
        self._q: deque[dict] = deque(maxlen=self.DEPTH)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()

    def start(self) -> "Reader":
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self) -> None:
        cmd = ["aseqdump"] + (["-p", self.port] if self.port else [])
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL,
                                          text=True, bufsize=1)
        except OSError as exc:
            self.error = type(exc).__name__
            return
        try:
            for line in self._proc.stdout or []:
                if self._stop.is_set():
                    break
                ev = midimon.parse_line(line)
                if ev is None:
                    continue
                with self._lock:
                    self._q.append(midimon.enrich(ev))
                    self.total += 1
        except (OSError, ValueError):
            self.error = "eof"
        finally:
            # The loop also ends when `aseqdump` exits on its own -- which is
            # exactly what happens when the ALSA client it was subscribed to
            # disappears (unplug). No exception fires for that: stdout just
            # reaches EOF. Distinguish it from an intentional `close()` so the
            # rebind tick knows this reader is dead and needs replacing.
            if not self._stop.is_set() and self.error is None:
                self.error = "closed"

    def alive(self) -> bool:
        """False once the `aseqdump` child has exited, for any reason."""
        return self._proc is not None and self._proc.poll() is None

    def drain(self) -> list[dict]:
        with self._lock:
            if not self._q:
                return []
            out = list(self._q)
            self._q.clear()
        return out

    def close(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except OSError:
                pass
            self._proc = None


# ── Qt ─────────────────────────────────────────────────────────────────────
def _qt():
    from PySide6 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


_QUIT = threading.Event()


def _install_signals() -> None:
    """Ctrl-C must close the window, but a Python handler only runs between
    bytecodes — Qt's event loop would swallow it. The always-running frame timer
    polls this flag, which is why there is no separate wakeup pipe."""
    def bye(_sig, _frm):
        _QUIT.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, bye)
        except (OSError, ValueError):
            pass


# The latch rig_units.ensure() reads. Duplicated as four lines here rather
# than imported: midiviz must run standalone (`python3 midiviz.py`) from a
# checkout with no rig on the box at all, and importing tools/rig_units.py
# from tools/bridge/ would make the lens depend on the supervisor it is only
# leaving a note for. The path is the contract; rig_units.latch_path() is the
# same expression and the test asserts they agree.
UNIT_NAME = "midiviz"

# Exit code for "the human closed this on purpose", paired with
# RestartPreventExitStatus=78 in midiviz.service.
#
# The unit is Restart=always, and rightly so: an unplugged LCXL used to make
# this process exit CLEANLY, which Restart=on-failure ignores, so PLN's window
# stayed gone for the rest of the session. But Restart=always cannot tell that
# exit from a person clicking the X -- so the X closed the window and systemd
# put it back five seconds later, which is PLN reporting "its too sticky" a
# SECOND time after the latch was already written.
#
# A distinct exit status is how systemd is told the difference. Crash, EOF, or
# any unplanned exit still comes back; a deliberate close does not. 78 is
# EX_CONFIG from sysexits.h, chosen only because it is well outside the range
# anything here returns on its own (0/1) and outside the 128+N signal range.
USER_CLOSE_EXIT = 78


# ── what survives a restart ────────────────────────────────────────────────
# A theme you have to re-pick every launch is a theme you do not have: PLN is
# in the sun, he reaches for the menu, the set ends, and tomorrow at the same
# terrace the window is black again. So theme and scale go to one small JSON
# file and come back.
#
# Every read and every write here is wrapped, and that is the whole feature.
# This file is on the critical path of a window PLN opens two minutes before a
# set: a monitor that refuses to start because a config file is half-written,
# or owned by root, or names a theme that was renamed, is strictly worse than a
# monitor with no memory at all. So `load_config` cannot raise and cannot
# return anything the rest of the file has to re-check -- it hands back a theme
# that IS in THEMES or None, and a scale that is a finite float in range or
# None. Notably a NaN survives `float()` and `min`/`max` untouched and only
# detonates later, inside `round()` in `_apply_scale`, where the traceback
# points at the font code and not at the config.
#
# Writes go through a temp file and `os.replace`, so a crash mid-save leaves
# the previous choice rather than the corrupt file this paragraph is about.
CONFIG_DIR = BRAND_SLUG          # one config dir per brand (see the brand block)
CONFIG_NAME = "midiviz.json"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / CONFIG_DIR / CONFIG_NAME


def _clean_scale(raw) -> "float | None":
    try:
        s = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(s):
        return None
    return max(0.6, min(2.4, round(s * 100) / 100))


def load_config() -> dict:
    """{'theme': name-in-THEMES or None, 'scale': float or None}. Never raises."""
    out: dict = {"theme": None, "scale": None}
    try:
        with open(config_path(), "r") as fh:
            raw = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return out
    if not isinstance(raw, dict):            # a list, a string, a bare number
        return out
    theme = raw.get("theme")
    if isinstance(theme, str) and theme in THEMES:
        out["theme"] = theme
    out["scale"] = _clean_scale(raw.get("scale"))
    return out


def save_config(theme: str, scale: float) -> bool:
    """Best effort; False (never an exception) when the disk says no."""
    path = config_path()
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.new")
        with open(tmp, "w") as fh:
            json.dump({"theme": theme, "scale": scale}, fh)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError):
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def latch_close_path():
    rt = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(rt, CONFIG_DIR, f"{UNIT_NAME}.closed")


def _latch_close():
    """Record that the human closed this, best-effort and never fatal."""
    try:
        path = latch_close_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    except OSError as e:
        print(f"midiviz: could not write the close latch ({e}); "
              f"the rig may start me again on its next converge",
              file=sys.stderr)


# ── the spectrum backdrop ──────────────────────────────────────────────────
# PLN, 2026-09-05: "can we overlay behind a basic spectro? that id remove
# spectro VSTs from ardour and wed have our super perf one as overlay on
# single win?"
#
# So: a coarse spectrum of the MASTER BUS, painted BEHIND the glyph rain, in
# one window that is already on top of everything. Four things are
# load-bearing and each of them is a lesson already paid for:
#
#   * **OFF BY DEFAULT, both ways.** This machine performs live. A new
#     always-on audio consumer is not something the rig gets to acquire
#     silently, so the capture subprocess does not exist until either
#     `--spectro` or the runtime `s` key asks for it, and `s` again takes it
#     away (process killed, not merely hidden).
#   * **Never on the GUI thread.** Capture and FFT live in one thread whose
#     only contact with Qt is a single `_latest` tuple swapped under a lock.
#     paintEvent reads that tuple; it can never wait on audio.
#   * **Drop-oldest, depth one.** There is no queue. Each analysed frame
#     OVERWRITES the last, so a GUI at 5 fps reading an analyser at 18 fps
#     shows the newest picture and discards the rest. (A 512-deep SSE queue
#     was itself the HUD's 1 s lag; depth is latency, and a backdrop that is
#     one frame late is invisible while a backdrop that is a second late is a
#     lie.)
#   * **Decimated on purpose.** 18 Hz, 40 log bands off a 2048-point FFT.
#     This is scenery, not an analyser: the glyphs stay temporally and
#     visually primary and the spectrum is a dim wash under them. If PLN ever
#     wants to *measure* something, `tidal-ears` is the tool and it is the
#     right one.
#
# The tap is the DEFAULT SINK's monitor, re-resolved on every (re)launch
# rather than captured once -- the same "never trust a resolved binding past
# the moment it might have gone stale" rule as the MIDI port above. The sink
# is what the audience hears after Ardour's master, which is the whole point:
# it is the mix, not one orbit. `pw-record`'s default 100 ms latency is asked
# for EXPLICITLY here: a monitor client requesting a tight buffer is how you
# talk the graph's quantum down and buy xruns for a decoration.
SPEC_BANDS = 40                     # log-spaced bars across the full width
SPEC_FFT = 2048                     # 23.4 Hz bins at 48k -- coarse on purpose
SPEC_HOP = 2560                     # 18.75 analysis frames a second
SPEC_RATE = 48000
SPEC_FMIN, SPEC_FMAX = 32.0, 16000.0
SPEC_DB_FLOOR, SPEC_DB_CEIL = -80.0, -6.0
SPEC_ATTACK, SPEC_RELEASE = 0.60, 0.12   # rise fast, fall slow
SPEC_RELAUNCH_S = 2.0               # backoff after the capture ends unasked
SPEC_LEVELS = 20                    # colour LUT depth for the wash


def _spec_band_edges(bands: int, fft_n: int, rate: int):
    """rfft bin index boundaries for `bands` log-spaced bands.

    Below ~750 Hz a 2048-point FFT has fewer bins than we have bands, so the
    low edges collapse onto consecutive bins. That is fine and deliberate --
    the alternative (empty bands, painted as silence) would read as a hole in
    the bass, which is exactly the misreading the "mapped-but-idle must not be
    black" note above is about.
    """
    import numpy as np
    nyq = rate * 0.5
    fmax = min(SPEC_FMAX, nyq * 0.98)
    ratio = np.arange(bands + 1) / float(bands)
    freqs = SPEC_FMIN * (fmax / SPEC_FMIN) ** ratio
    idx = np.rint(freqs / rate * fft_n).astype(int)
    top = fft_n // 2
    idx = np.clip(idx, 1, top)
    for i in range(1, idx.size):            # strictly increasing, so no band is empty
        if idx[i] <= idx[i - 1]:
            idx[i] = idx[i - 1] + 1
    if idx[-1] > top:                       # ran out of bins: slide the whole ramp down
        idx -= (idx[-1] - top)
        idx = np.clip(idx, 1, top)
    return idx


def _read_exact(stream, n: int) -> "bytes | None":
    """Exactly `n` bytes, or None at end of stream.

    2026-09-05, and this cost a whole verification round: `Popen(bufsize=0)`
    hands back a RAW `FileIO`, whose `read(n)` is one `os.read` and therefore
    returns whatever the pipe happens to hold. The first version read one hop
    with `stdout.read(need)` and treated a short read as EOF -- so the very
    first partial chunk ended the capture, `blocks=0`, `err=''`, and a green
    selftest sat happily on top of a backdrop that could never receive a
    sample. The pure half proved nothing about the plumbing; only running it
    against real audio did.
    """
    chunks, got = [], 0
    while got < n:
        b = stream.read(n - got)
        if not b:
            return None
        chunks.append(b)
        got += len(b)
    return b"".join(chunks) if len(chunks) > 1 else chunks[0]


def spec_default_target() -> str | None:
    """The default sink's node name, or None if PulseAudio/PipeWire is absent."""
    try:
        out = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                             text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    name = (out.stdout or "").strip()
    return name or None


class SpectrumSource:
    """`pw-record` on the default sink's monitor → one coarse band frame.

    `push()` is the seam: it is the whole analysis path and it does not care
    where its samples came from, so `--selftest` drives the real FFT and the
    real paint with synthetic audio on a box with no sound at all. Everything
    the subprocess adds is in `_run`, and nothing in `_run` computes anything.
    """

    def __init__(self, target: str | None = None, bands: int = SPEC_BANDS,
                 rate: int = SPEC_RATE, fft_n: int = SPEC_FFT,
                 hop: int = SPEC_HOP):
        import numpy as np
        self._np = np
        self.target = target
        self.bands = bands
        self.rate = rate
        self.fft_n = fft_n
        self.hop = hop
        self.edges = _spec_band_edges(bands, fft_n, rate)
        self._win = np.hanning(fft_n).astype(np.float32)
        self._winsum = float(self._win.sum())
        self._buf = np.zeros(fft_n, dtype=np.float32)
        self._env = np.zeros(bands, dtype=np.float32)
        self._lock = threading.Lock()
        self._latest: tuple[float, ...] | None = None
        self.frames = 0                 # analysed frames
        self.blocks = 0                 # blocks read off the pipe
        self.err = ""                   # last thing that went wrong, for the header
        self._stop = threading.Event()
        self._th: threading.Thread | None = None
        self._proc = None

    # ── analysis (thread-agnostic; the tested half) ────────────────────────
    def push(self, block) -> tuple[float, ...]:
        """One hop of mono float samples → the published band frame."""
        np = self._np
        b = np.asarray(block, dtype=np.float32).ravel()
        if b.size >= self.fft_n:
            self._buf[:] = b[-self.fft_n:]
        elif b.size:
            self._buf[:-b.size] = self._buf[b.size:]
            self._buf[-b.size:] = b
        spec = np.abs(np.fft.rfft(self._buf * self._win)) * (2.0 / self._winsum)
        power = spec * spec
        e = self.edges
        # One reduceat over the whole spectrum instead of `bands` slices.
        # power is truncated at the LAST edge: reduceat's final segment runs to
        # the end of its input, so an untruncated array would dump everything
        # from 16 kHz to Nyquist into the top band.
        sums = np.add.reduceat(power[:e[-1]], e[:-1])
        widths = np.maximum(1, e[1:] - e[:-1])
        db = 10.0 * np.log10(sums / widths + 1e-20)
        lvl = (db - SPEC_DB_FLOOR) / (SPEC_DB_CEIL - SPEC_DB_FLOOR)
        np.clip(lvl, 0.0, 1.0, out=lvl)
        rising = lvl > self._env
        a = np.where(rising, SPEC_ATTACK, SPEC_RELEASE).astype(np.float32)
        self._env += a * (lvl.astype(np.float32) - self._env)
        frame = tuple(float(v) for v in self._env)
        with self._lock:
            self._latest = frame
            self.frames += 1
        return frame

    def latest(self) -> "tuple[float, ...] | None":
        with self._lock:
            return self._latest

    # ── capture (the subprocess half) ──────────────────────────────────────
    def _cmd(self, target: str) -> list[str]:
        return ["pw-record", "--target", target, "--rate", str(self.rate),
                "--channels", "1", "--format", "f32", "--latency", "100ms",
                "--raw", "-"]

    def start(self) -> "SpectrumSource":
        if self._th is None:
            self._th = threading.Thread(target=self._run, name="spectro",
                                        daemon=True)
            self._th.start()
        return self

    def _run(self) -> None:
        need = self.hop * 4                      # float32 mono
        while not self._stop.is_set():
            target = self.target or spec_default_target()
            if not target:
                self.err = "no sink"
                if self._stop.wait(SPEC_RELAUNCH_S):
                    return
                continue
            try:
                self._proc = subprocess.Popen(self._cmd(target),
                                              stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL,
                                              bufsize=0)
            except OSError as exc:               # no pw-record on this box
                self.err = type(exc).__name__
                return
            try:
                out = self._proc.stdout
                while not self._stop.is_set():
                    raw = _read_exact(out, need)
                    if raw is None:
                        self.err = "eof"
                        break
                    self.blocks += 1
                    self.err = ""
                    self.push(self._np.frombuffer(raw, dtype="<f4"))
            except (OSError, ValueError) as exc:
                self.err = type(exc).__name__
            finally:
                self._kill()
            # EOF unasked = the sink went away (device switch, replug). Re-resolve
            # from scratch rather than keeping a binding a hotplug just killed.
            if self._stop.wait(SPEC_RELAUNCH_S):
                return

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        for stream in (proc.stdout,):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._kill()
        th, self._th = self._th, None
        if th is not None:
            th.join(timeout=1.5)


def build_widget(port_label: str, reader: "Reader | None", scale: float = 1.0,
                 pinned_port: str | None = None, watch: bool = False,
                 spectro: bool = False, theme: str = DEFAULT_THEME,
                 persist: bool = False, tray: bool = False,
                 hl: "oscfeed.HLFeed | None" = None):
    QtCore, QtGui, QtWidgets = _qt()
    Qt = QtCore.Qt

    class MidiViz(QtWidgets.QWidget):
        def __init__(self):
            super().__init__(None)
            self.setWindowTitle("midiviz")
            self.setWindowFlags(Qt.WindowType.Window
                                | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint)
            # On-top starts on (it is a lens you glance at over the editor) but
            # is now a RUNTIME choice, because PLN asked for the pin and the
            # close to be separate controls. All-desktops is deliberately NOT
            # in here: it is a KWin rule keyed on the app id, it is
            # unconditional, and toggling the pin must not disturb it.
            self.on_top = True
            self._user_closed = False
            self._chrome_hot = False
            self._t_pad = 4               # vertical slack on the hit targets
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setCursor(Qt.CursorShape.SizeAllCursor)

            self.reader = reader
            # The state channel. Constructed unconditionally and cheaply: with
            # no file on disk it reports nothing and _paint_state returns at its
            # first line, so a box without the driver draws exactly what it drew
            # before this layer existed.
            self.state = SurfaceState()
            self.port_label = port_label
            self._pinned = pinned_port   # explicit -p, or None for auto-resolve
            self.last_cc = None          # (cc, value, channel, t) -- header readout
            self.scale = scale
            self.theme = THEMES.get(theme) or THEMES[DEFAULT_THEME]
            # OFF unless main() asks. The suite and --selftest build widgets by
            # the dozen; none of them gets to rewrite PLN's saved theme, and
            # none of them gets to read one either -- a test whose result
            # depends on the developer's config file is not a test.
            self._persist = bool(persist)
            self.paused = False
            self.t0 = time.monotonic()
            self.last_event_t = 0.0
            self.frames = 0
            self.painted = 0
            self.ingested = 0
            self._interval = FPS_ACTIVE
            self._rng = random.Random(0xC0FFEE)
            self._drag_from = None
            self._resize_edges = Qt.Edge(0)   # which edges a press grabbed; 0 = body
            self._resize_from = None          # (press point, geometry) for the manual path
            self._grab_hot = Qt.Edge(0)       # edges under the pointer, for the ticks

            self.cc: dict[int, list] = {}        # cc -> [value, t_last, channel]
            self.col_t = [0.0] * 9               # column 1..8 last-touch, for the glow
            self.stream: deque = deque(maxlen=256)   # bottom ribbon, newest right
            self.raw_port = False                # reading the board, untranslated
            self.v3_dialect = False              # ...and in the one dialect we can fix
            self.drops: list[list] = []          # gutter rain
            self._drop_t: dict[int, float] = {}  # cc -> last token rained
            self.hist: deque = deque([0] * 64, maxlen=64)   # events per 100 ms
            self._bucket_t = self.t0
            self._bucket_n = 0

            # The HL feed: state mirrors of the two things a Tidal trigger
            # lights up. `hl_t` is per-CC last-trigger (an edge/border heat,
            # kept SEPARATE from self.cc's touch heat on purpose — a cell that
            # flashes because the set played through it must not also claim
            # you touched it, or the two questions blur into one lie).
            self.hl = hl
            self.hl_t: dict[int, float] = {}
            self._hl_cells_cache: dict[int, list[int]] = {}
            # orbit -> its level fader's CC (D1-D8 for d1-d8, A1-A4 for
            # d9-d12), for the zero-volume HL gate above.
            self._level_cc = {o: grid.orbit_home(o).get("level")
                              for o in range(1, 15)}
            self.wave = False                 # the ParVagues wave, behind all
            self.word = False                 # …and the PARVAGUES lettering
            self._wave_pm: QtGui.QPixmap | None = None
            # The wordmark's face, registered once. A checkout without the
            # asset leaves the id at -1 and the word simply never draws — the
            # wave is decoration, and decoration never gets to crash the lens.
            try:
                self._syne_id = QtGui.QFontDatabase.addApplicationFont(
                    str(SYNE_PATH))
            except Exception:
                self._syne_id = -1
            self._word_font = None
            self._word_font_key = None
            self._word_pm = None
            self._word_pm_key = None

            # The backdrop. `spec_src` is None until somebody asks: OFF means
            # no thread and no subprocess, not a hidden one.
            self.spectro = False
            self.spec_src: "SpectrumSource | None" = None
            self.spec_err = ""
            self._spec_frame: tuple[float, ...] | None = None
            self._spec_peak: list[float] = []
            self._spec_hot_t = 0.0        # last time the mix was audibly awake

            # The only pixel size this file chooses on its own, and the
            # reference every later density reading is measured against. The
            # fit being exactly 1.0 at birth is what keeps this whole change
            # invisible to an eye trained on the old look.
            #
            # Set BEFORE the resize, because a resize can deliver its own
            # event, and `resizeEvent` re-measures everything: a reference that
            # does not exist yet would be an AttributeError at startup on any
            # platform that sends that event synchronously.
            self._ref_w = max(1, round(BASE_W * self.scale))
            self._ref_h = max(1, round(BASE_H * self.scale))
            self.dens = self.scale
            self._metrics_key = None     # (main px, micro px, bold) last measured
            self.setMinimumSize(MIN_W, MIN_H)
            self.resize(self._ref_w, self._ref_h)
            # …then read it BACK off the widget. If a minimumSize ever clamped
            # the startup geometry, a computed reference would disagree with
            # the real window and the fit would not be 1.0 at birth.
            self._ref_w, self._ref_h = max(1, self.width()), max(1, self.height())
            # Hover must reach mouseMoveEvent with no button held, or neither
            # the resize cursors nor the header controls' own brightening ever
            # happen: without this Qt only delivers moves during a drag.
            self.setMouseTracking(True)

            self._build_palette()
            self._apply_scale()
            # The menu exists even when nothing will ever open it (headless,
            # no tray): it is the single definition of "what this window can
            # do", and building it unconditionally is what lets --selftest and
            # the suite construct it on every run instead of never.
            self._build_menu()
            self.tray = None
            if tray:
                self._build_tray()

            self.timer = QtCore.QTimer(self)
            self.timer.setTimerType(Qt.TimerType.CoarseTimer)
            self.timer.timeout.connect(self.tick)
            self.timer.start(self._interval)

            # Losing the surface must never be fatal -- see the module note
            # by RECONNECT_MS. `watch` is off for --selftest (headless, fed
            # synthetic events by hand) so the suite never shells out to
            # `aconnect`/`aseqdump` on its own clock.
            # Asked for on the command line: acquire it once the widget is
            # otherwise fully built, through the same one door as the `s` key.
            if spectro:
                self.set_spectro(True)

            self.rebind_timer = None
            if watch:
                self.rebind_timer = QtCore.QTimer(self)
                self.rebind_timer.setTimerType(Qt.TimerType.CoarseTimer)
                self.rebind_timer.timeout.connect(self._rebind_tick)
                self.rebind_timer.start(RECONNECT_MS)

        # ── reconnect ──────────────────────────────────────────────────────
        def _rebind_tick(self):
            """Re-resolve the source and swap the Reader if it moved or died.

            Runs every RECONNECT_MS regardless of whether anything is wrong --
            re-asserting is cheaper than detecting drift, the same call this
            rig makes for the LCXL LED port and for `midi-autoconnect.sh`'s
            wiring. Never raises, never closes the window.
            """
            if self._pinned:
                # A pin names an exact address, never an LCXL alias by name,
                # so the ghost distrust below does not apply to it -- only
                # "has this address disappeared from the listing at all".
                pid = self._pinned if _port_listed(self._pinned) else None
                label = self._pinned if pid else "%s (gone)" % self._pinned
            else:
                pid, label = resolve_watch_port()
                # Distrust the match ONLY when it is itself an LCXL alias
                # (the ghost case) -- a "Midi Through" catch-all match is not
                # LCXL-named and needs no hardware to be a legitimate source.
                if pid is not None and HW_NAME_RE.search(label) and not _hardware_present():
                    pid, label = None, "%s (ghost, no hardware) — no MIDI source found" % label

            alive = self.reader is not None and self.reader.alive()
            same = self.reader is not None and pid is not None and self.reader.port == pid

            if pid is None:
                if self.reader is not None:
                    self.reader.close()
                    self.reader = None
                self.port_label = label
                return

            if self.reader is None or not alive or not same:
                if self.reader is not None:
                    self.reader.close()
                self.reader = Reader(pid).start()
                self.port_label = label
            # AM I READING THE TRANSLATED STREAM?
            #
            # The question nobody thought to ask on 2026-09-22, and the answer
            # was no. With `lcxl3-driver` down there is no 'ParVagues LCXL3'
            # port, so the preference falls through to the board's own
            # 'LCXL3 1 DAW' — which speaks v3 numbering, where row C's knobs
            # carry row B's v2 CCs and the faders carry CCs in no cell at all.
            # The window then draws a coherent, confident, WRONG picture, and
            # four separate "bugs" get reported for one missing translation.
            #
            # Scoped to LCXL3 deliberately: an ORIGINAL LCXL has no driver in
            # front of it, so its raw port IS the right port, and a pip there
            # would be a false alarm on a board that is working.
            self.raw_port = ("LCXL3" in label) and ("ParVagues" not in label)
            # TWO questions, deliberately not one flag. `raw_port` asks "is the
            # picture coming from the board instead of the driver" and drives
            # the warning pip -- true in DAW mode AND in custom mode, because in
            # both the numbers are not guaranteed to be the corpus's.
            # `v3_dialect` asks the narrower "may we translate", and only DAW
            # mode may: that is the one dialect whose numbering is fixed and
            # known. WATCH_PREFERENCE's third entry is the raw CUSTOM-mode port
            # ('LCXL3 1 MIDI In'), which speaks user-assigned numbers -- and
            # midiviz was reading exactly that port on 2026-09-23, so
            # translating on `raw_port` would have been a second wrong picture
            # with the opposite sign.
            self.v3_dialect = ("DAW" in label) and ("ParVagues" not in label)

        # ── palette / fonts / geometry ─────────────────────────────────────
        def _build_palette(self):
            """Resolve the whole theme into LUTs. Once per theme change, never
            per frame -- the frame budget is why this method exists at all."""
            th = self.theme
            self.bg = QtGui.QColor(*th.bg)
            # LUT[family][level] — every INK colour this window can ever paint,
            # so a frame constructs no QColor at all.
            self.lut = []
            for hue in FAM_HUE:
                col = []
                for lv in range(LEVELS):
                    val, sat = th.ink.at(lv / (LEVELS - 1))
                    col.append(QtGui.QColor.fromHsvF(hue / 360.0, sat, val))
                self.lut.append(col)
            # tint[family][0..3] — the cell-body SURFACES, and [0] is the floor
            # every mapped-but-idle cell gets. See the THEMES note: on dark
            # these are exactly ink levels 1..4 (that is what `tint_t` says),
            # so nothing about the old look moved; on a light page they are a
            # separate, shallower ramp, because a panel must stay near the page
            # while the ink on it walks away from it.
            self.tint = []
            for hue in FAM_HUE:
                row = []
                for t in th.tint_t:
                    val, sat = th.tint.at(t)
                    row.append(QtGui.QColor.fromHsvF(hue / 360.0, sat, val))
                self.tint.append(row)
            # Chrome (border, row letters, CC reference digits, the two header
            # controls) is the FAM_OTHER ink ramp on dark — one fewer ramp to
            # keep honest — but needs to be its own near-neutral ink on a light
            # page, where a violet at 6/15 is a smudge rather than a hairline.
            if th.chrome is None:
                self.chrome = self.lut[FAM_OTHER]
            else:
                self.chrome = [
                    QtGui.QColor.fromHsvF(th.chrome_hue / 360.0, sat, val)
                    for val, sat in (th.chrome.at(lv / (LEVELS - 1))
                                     for lv in range(LEVELS))
                ]
            # The backdrop's own ramp, and it is deliberately NOT one of the
            # families: the eight families MEAN role, and a wash that borrowed
            # one would read as "orbit N did something". A cold blue-violet
            # that no control ever uses, at alphas low enough that the dimmest
            # glyph still wins the foreground. Precomputed like everything
            # else, so a spectrum frame constructs no QColor.
            sp = th.spec
            self.spec_lut = [
                QtGui.QColor.fromHsvF(sp.h0 + sp.hspan * (lv / (SPEC_LEVELS - 1)),
                                      sp.sat, sp.v0 + sp.vspan * (lv / (SPEC_LEVELS - 1)),
                                      (sp.a0 + sp.aspan * (lv / (SPEC_LEVELS - 1))) / 255.0)
                for lv in range(SPEC_LEVELS)
            ]
            self.spec_peak_col = QtGui.QColor(*th.spec_peak)
            # The scanline texture is a CACHED PIXMAP in this theme's colours.
            # Forgetting this line is the whole bug: the picture would repaint
            # correctly and a violet CRT haze from the previous theme would stay
            # blitted over a white page, once per frame, forever.
            self._scan = None

        # ── the theme axis ─────────────────────────────────────────────────
        def set_theme(self, name: str, save: bool = True):
            """Switch palettes. Idempotent, and safe with an unknown name."""
            th = THEMES.get(name)
            if th is None or th is self.theme:
                return
            was_bold = self.theme.bold
            self.theme = th
            self._build_palette()
            if th.bold != was_bold:
                self._apply_scale()       # new weight, new metrics, new layout
            if getattr(self, "tray", None) is not None:
                # The tray icon is painted from the palette, so it goes stale
                # the moment the palette does -- a dark badge on a light panel
                # is the same complaint one size down.
                self.tray.setIcon(self._tray_icon())
            self._sync_menu()
            if save:
                self._save()
            self.update()

        def cycle_theme(self):
            names = THEME_ORDER
            try:
                i = names.index(self.theme.name)
            except ValueError:            # a theme outside the cycle: rejoin it
                i = -1
            self.set_theme(names[(i + 1) % len(names)])

        def _save(self):
            if self._persist:
                save_config(self.theme.name, self.scale)

        def _font(self, px: int):
            f = QtGui.QFont()
            f.setFamilies(["Geist Mono", "JetBrains Mono", "Fira Code",
                           "DejaVu Sans Mono", "monospace"])
            f.setStyleHint(QtGui.QFont.StyleHint.Monospace)
            f.setPixelSize(max(5, px))
            # Weight is a theme property, and in daylight it is the single
            # cheapest contrast there is: one more stroke pixel beats any
            # amount of hue. Metrics are re-measured from the resulting font in
            # `_apply_scale`, so the ribbon's run concatenation -- which
            # assumes a known advance -- stays correct at either weight.
            f.setBold(self.theme.bold)
            return f

        # ── density, which is NOT the window's size ────────────────────────
        # See the FIT_MIN block up top for why these are two numbers. Both
        # helpers take an explicit (w, h) so the shape of the window can be
        # asked about without resizing it — which is what the tests do, and
        # what "does the type grow with the window" has to mean to be checkable
        # at all.
        def _fit(self, w=None, h=None) -> float:
            """How big the window is, relative to the one it was born at."""
            w = self.width() if w is None else w
            h = self.height() if h is None else h
            f = min(max(1, w) / self._ref_w, max(1, h) / self._ref_h)
            return max(FIT_MIN, min(FIT_MAX, f))

        def _density(self, w=None, h=None) -> float:
            """The type scale: the window's own fit times the explicit knob."""
            return max(DENS_MIN, min(DENS_MAX, self._fit(w, h) * self.scale))

        def _apply_scale(self):
            """Fonts, metrics and layout for the CURRENT size, knob and weight.

            Idempotent, and it never calls resize(): `resizeEvent` runs this,
            and a resize() from inside a resize handler is how a frameless
            window ends up oscillating between two geometries for ever. The
            name stays `_apply_scale` because the knob still comes through
            here; what left is the geometry.
            """
            self.dens = d = self._density()
            px, upx = max(5, round(11 * d)), max(5, round(8 * d))
            key = (px, upx, self.theme.bold)
            if key != self._metrics_key:
                # Re-measured only when the FACE actually changed. The weight
                # belongs in that key because `set_theme` calls this precisely
                # when bold flips at an unchanged pixel size, and the ribbon's
                # run concatenation assumes a measured advance (see `_font`).
                self._metrics_key = key
                self.f_main = self._font(px)
                self.f_micro = self._font(upx)
                fm = QtGui.QFontMetricsF(self.f_main)
                fmm = QtGui.QFontMetricsF(self.f_micro)
                self.mw, self.mh = fm.horizontalAdvance("0"), fm.height()
                self.uw, self.uh = fmm.horizontalAdvance("0"), fmm.height()
                self.m_asc, self.u_asc = fm.ascent(), fmm.ascent()
            self._relayout()

        def reset_size(self):
            """Back to the size the window was born at — BASE × the knob.

            The one deliberate pixel-size action in the file, and the only
            resize() outside startup. It exists so that a tile experiment, or a
            compositor that parked the lens at 3000 px wide, is one menu entry
            (or `0`) away from the shape PLN knows, without the scale knob
            having to secretly mean "size" again.
            """
            self.resize(self._ref_w, self._ref_h)
            self._apply_scale()
            self.update()

        def _relayout(self):
            w, h = max(80, self.width()), max(60, self.height())
            s = self.dens
            self.pad = max(3, round(5 * s))
            gap = max(2, round(4 * s))
            self.hdr_y = self.pad
            self.hdr_h = round(self.uh + 2)
            self.foot_h = round(self.mh + 2)
            self.foot_y = h - self.pad - self.foot_h
            self.gut_w = max(round(18 * s), round(self.uw * 5))
            self.gut_x = w - self.pad - self.gut_w
            self.lab_w = round(self.uw * 1.7)
            self.mx = self.pad + self.lab_w
            # A band of its own for the column digits. Column N *is* orbit N, so
            # that digit is the single most load-bearing glyph in the window;
            # squeezing it into the header (where the sparkline lives) lost it.
            self.col_y = self.hdr_y + self.hdr_h + gap
            self.col_h = round(self.uh)
            self.my = self.col_y + self.col_h
            self.mw_px = max(8, self.gut_x - gap - self.mx)
            self.mh_px = max(8, self.foot_y - gap - self.my)
            self.cw = self.mw_px / 8.0
            self.ch = self.mh_px / 6.0
            self.inset = max(1, round(1.6 * s))  # keeps neighbouring cells distinct
            self.gut_rows = max(1, int(self.mh_px / max(1.0, self.uh)))
            self.stream_n = max(4, int((w - 2 * self.pad) / max(1.0, self.mw)))
            # Bound the ribbon to exactly what fits, so paint iterates the deque
            # in place instead of copying and slicing a 256-list every frame.
            self.stream = deque(self.stream, maxlen=self.stream_n)
            self._scan = None                    # rebuilt lazily at paint time

        def _scanlines(self):
            """One cached pixmap for the CRT texture: a whole frame of it is one blit."""
            if self._scan is not None:
                return self._scan
            w, h = max(1, self.width()), max(1, self.height())
            pm = QtGui.QPixmap(w, h)
            pm.fill(QtGui.QColor(0, 0, 0, 0))
            p = QtGui.QPainter(pm)
            step = max(2, round(3 * self.dens))
            hz, vt = self.theme.scan
            p.setPen(QtGui.QColor(*hz))
            for y in range(0, h, step):
                p.drawLine(0, y, w, y)
            p.setPen(QtGui.QColor(*vt))
            for x in range(0, w, step * 4):
                p.drawLine(x, 0, x, h)
            p.end()
            self._scan = pm
            return pm

        def _scanlines_blit(self, p):
            # A theme may have no texture at all (`sun`): outdoors every point
            # of contrast is spent on the reading, not on the atmosphere.
            if self.theme.scan is None:
                return
            p.drawPixmap(0, 0, self._scanlines())

        def resizeEvent(self, _e):
            # Density follows the window, so a resize is a full re-measure and
            # not just a re-layout -- that is the whole of "squeeze and expand
            # properly". `_apply_scale` is idempotent and calls no resize(),
            # so re-entering here is not a loop.
            self._apply_scale()

        # ── ingest ─────────────────────────────────────────────────────────
        def ingest(self, ev: dict) -> None:
            """One parsed event → visual state. The whole render path hangs off
            this, which is what lets --selftest exercise the drawing for real."""
            now = time.monotonic()
            self.last_event_t = now
            self.ingested += 1
            self._bucket_n += 1
            e = (ev.get("event") or "").lower()
            chn = ev.get("ch") or 0
            v = _norm_value(ev)

            if e.startswith("control") and ev.get("controller") is not None:
                cc = int(ev["controller"])
                # Belt AND suspenders (PLN, 2026-09-23: "in my perspective, we
                # would have always cell and rain?"). The preferred port is the
                # driver's translated one, but when we fall through to the raw
                # board the numbers are v3 DAW indices, and drawing them as if
                # they were corpus CCs is how row C's knobs lit row B, row D lit
                # nothing, and E5-E8 lit E1-E4 for as long as the driver was
                # down. A viewer that owns the translation is right either way,
                # so the picture no longer depends on another process running.
                if self.v3_dialect:
                    cc = grid.V3_TO_V2.get(cc, cc)
                cell = grid.CC_TO_CELL.get(cc)
                if cell is not None:
                    st = self.cc.get(cc)
                    if st is None:
                        self.cc[cc] = [v, now, chn]
                    else:
                        st[0], st[1], st[2] = v, now, chn
                    self.col_t[cell[1]] = now
                    # PLN, 2026-09-05: "would help to see the chan num: 33 or 53
                    # etc so as i press i can wire". The grid shows WHERE a knob
                    # is and HOW FAR it moved, but never WHICH CC it is -- so
                    # wiring a control meant leaving the window for the CC map.
                    # Keep the last one touched; the header prints it as the
                    # exact token the corpus uses, so it can be typed straight
                    # into a pattern.
                    self.last_cc = (cc, v, chn, now)
                    fam = self._fam_for_cc(cc)
                    self.stream.append((HEX[v >> 3], fam, now))
                    # The token, in the gutter, in this control's family colour.
                    # The cell says WHERE and HOW FAR; the ribbon says the shape
                    # of the traffic; this says WHAT TO TYPE, without leaving the
                    # window for the CC map.
                    if now - self._drop_t.get(cc, 0.0) >= DROP_EVERY:
                        self._drop_t[cc] = now
                        self._spawn_drop("^%d" % cc, fam)
                    return
                # a CC the authored grid does not own: rain, not a cell.
                self.stream.append((HEX[v >> 3], FAM_OTHER, now))
                self._spawn_drop(HEX[cc & 0xF], FAM_OTHER)
                return

            g, fam = _glyph_for(ev.get("event") or "")
            self.stream.append((g, fam, now))
            self._spawn_drop(g, fam)

        def _fam_for_cc(self, cc: int) -> int:
            role = grid.CC_ROLE.get(cc)
            return ROLE_FAM.get(role[0], FAM_OTHER) if role else FAM_OTHER

        def _spawn_drop(self, glyph: str, fam: int) -> None:
            if len(self.drops) >= MAX_DROPS:      # hard cap: rain is decoration
                del self.drops[:len(self.drops) - MAX_DROPS + 1]
            self.drops.append([self._rng.randrange(0, 3), 0.0,
                               time.monotonic(), glyph, fam])

        # ── the HL feed ────────────────────────────────────────────────────
        # One Tidal trigger lights exactly two things, and each answers one
        # question: the COLUMN glow (col_t) answers "is this orbit playing",
        # and the cell edges (hl_t) answer "which of these controls does the
        # set actually pass through". The MIDI feed could answer neither —
        # a knob at rest in a live pattern looks identical to a knob nobody
        # uses, which is the confusion this exists to end.
        def _hl_cells(self, orbit: int) -> list[int]:
            """The CCs that AFFECT this orbit: everything it owns, column by
            the grid's law (COLUMN N IS ORBIT N), plus its two family
            controls — a gF sweep and a gM mute act on every trigger in the
            family, so a picture that omitted them would understate what is
            in play. Derived from the grid, never re-hardcoded here.
            """
            cached = self._hl_cells_cache.get(orbit)
            if cached is not None:
                return cached
            ccs = set(grid.orbit_home(orbit).values())
            for role, fam_of in (("family_filter", grid.filter_family),
                                 ("family_mute", grid.mute_family)):
                n = fam_of(orbit)
                for cc, (r, who) in grid.CC_ROLE.items():
                    if r == role and who == n:
                        ccs.add(cc)
            out = sorted(ccs)
            self._hl_cells_cache[orbit] = out
            return out

        def ingest_hl(self, evs: list[dict]) -> None:
            now = time.monotonic()
            self.last_event_t = now        # the set PLAYING is activity: keep
            self.ingested += len(evs)      # the window at the active frame rate
            self._bucket_n += len(evs)
            for ev in evs:
                o = ev["orbit"]
                # PLN, 2026-09-24: "dont HL when d1-d12 controls are at zero
                # volume (so D1 at 0 kills d1 HLs, A1 at 0 kills d9, etc)".
                # A trigger the mix cannot hear is not a picture the lens
                # should paint. Only a KNOWN zero suppresses — a fader never
                # touched on the surface is unknown, not silent, and stays lit.
                lvl = self._level_cc.get(o)
                if lvl is not None:
                    st = self.cc.get(lvl)
                    if st is not None and st[0] == 0:
                        continue
                if 1 <= o <= 8:
                    self.col_t[o] = now
                for cc in self._hl_cells(o):
                    self.hl_t[cc] = now

        def set_hl(self, on: bool):
            """Acquire or release the HL listener. Off genuinely tears the
            socket down — same contract as the spectro tap: OFF is no thread,
            not a hidden one."""
            on = bool(on)
            if on == (self.hl is not None):
                return
            if on:
                self.hl = oscfeed.HLFeed().start()
            else:
                feed, self.hl = self.hl, None
                if feed is not None:
                    feed.close()
                self.hl_t.clear()
            self._sync_menu()
            self.update()

        def toggle_hl(self):
            self.set_hl(self.hl is None)

        # ── the wave ───────────────────────────────────────────────────────
        def set_wave(self, on: bool):
            self.wave = bool(on)
            self._sync_menu()
            self.update()

        def toggle_wave(self):
            self.set_wave(not self.wave)

        def _wave_pixmap(self):
            if self._wave_pm is None:
                pm = QtGui.QPixmap(str(WAVE_PATH))
                if pm.isNull():            # a checkout without the asset draws
                    return None            # without the wave, never crashes
                self._wave_pm = pm
            return self._wave_pm

        def _wave_font(self, target_w: float, target_h: float):
            """Syne ExtraBold at 92% of the row height. The vendored ttf is
            the variable font INSTANCED at 800 — Qt resolves a variable
            font's default instance (Regular) however loudly you ask for a
            weight, which is how the first cut read thin. Width is NOT
            computed from font metrics here: metrics ignored the letter
            spacing and kerned sub-advances disagreed, and that arithmetic is
            exactly how "PARVAGU" lost its tail. The bake measures its own
            ink instead (see _word_pixmap)."""
            key = (round(target_w), round(target_h))
            if self._word_font_key == key:
                return self._word_font
            fam_id = self._syne_id
            if fam_id < 0:
                return None
            fam = QtGui.QFontDatabase.applicationFontFamilies(fam_id)[0]
            f = QtGui.QFont(fam)
            f.setPixelSize(max(6, target_h * 0.92))
            f.setLetterSpacing(QtGui.QFont.SpacingType.PercentageSpacing,
                               106.0)               # +0.06em, as the hero tracks
            self._word_font, self._word_font_key = f, key
            return f

        def _draw_word_layer(self, qp, base, pad, fm, pen):
            """One pass of the word: glyphs drawn one by one with the tracked
            advance, so the letter spacing is OURS and identical under the
            glow rings and the core."""
            word = WORDMARK          # the brand's block lettering, one glyph at a time
            qp.setPen(pen)
            x = float(pad)
            for c in word:
                qp.drawText(QtCore.QPointF(x, base), c)
                x += fm.horizontalAdvance(c)
            return x - pad

        def _word_ink_width(self, img):
            """The rightmost non-transparent column — the METRICS LIE (they
            dropped the letter spacing and kerned differently from the
            rasterizer), the pixels do not. This scan is how 'PARVAGU' lost
            its tail and how it gets it back."""
            w, h = img.width(), img.height()
            right = 0
            for x in range(w - 1, -1, -1):
                col_has = False
                for y in range(h):
                    if img.pixelColor(x, y).alpha() > 8:
                        col_has = True
                        break
                if col_has:
                    right = x
                    break
                right = 0
            return right + 1

        def _word_pixmap(self, span_w: float, span_h: float):
            """The wordmark BAKED — glyphs, tracking and neon glow rendered
            once per geometry, not per frame. PLN's verdicts are all in here:
            the glow is TIGHT ("too blurry"), the type is ExtraBold-800
            ("too light"), the bake measures its own ink and crops ("i see
            PARVAGU"), and it must FIT the D1 cell at the cell's margin —
            which means the TYPE shrinks to the width, honestly, rather than
            a squash that would make the letters strangers to themselves."""
            key = (round(span_w), round(span_h))
            if self._word_pm_key == key:
                return self._word_pm
            f = self._wave_font(span_w, span_h)
            if f is None:
                return None

            def bake(px, canvas_w, glow=True):
                fb = QtGui.QFont(f)
                fb.setPixelSize(max(4, px))
                fmb = QtGui.QFontMetricsF(fb)
                pad_b = max(2, int(px * 0.25))
                img = QtGui.QImage(round(canvas_w), round(span_h) + pad_b * 2,
                                   QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(0)
                qp = QtGui.QPainter(img)
                try:
                    qp.setRenderHint(
                        QtGui.QPainter.RenderHint.TextAntialiasing, True)
                    qp.setFont(fb)
                    base = pad_b + (span_h + fmb.ascent() - fmb.descent()) / 2.0
                    if glow:
                        # the glow: two CLOSE rings, faint — rim of neon, not fog
                        r0 = max(1.0, px * 0.07)
                        for rad, alpha in ((r0, 24), (r0 * 0.5, 38)):
                            glow_pen = QtGui.QPen(QtGui.QColor(217, 0, 255, alpha))
                            glow_pen.setWidthF(max(1.2, px * 0.06))
                            for i in range(10):
                                a = 2 * math.pi * i / 10
                                qp.save()
                                qp.translate(math.cos(a) * rad, math.sin(a) * rad)
                                self._draw_word_layer(qp, base, pad_b, fmb, glow_pen)
                                qp.restore()
                    # the core: --neon-high itself, the site's own pink
                    self._draw_word_layer(qp, base, pad_b, fmb,
                                          QtGui.QPen(QtGui.QColor("#d900ff")))
                finally:
                    qp.end()
                return img, pad_b

            # PASS 1 — probe CORE-ONLY (the glow's ink would inflate the
            # measurement and the fit would undershoot), on a canvas the word
            # can never outrun: the D1-span cut came from a probe narrower
            # than the word, where the ink scan believed the clip was the edge.
            px = max(6.0, span_h * 0.92)
            probe, pad1 = bake(px, int(px * 12), glow=False)
            ink = self._word_ink_width(probe)
            word_w = ink - 2 * pad1
            # PASS 2 — if the word outruns the span, shrink the TYPE to fit;
            # never squash it, never clip it.
            if word_w > span_w:
                fit = max(8.0, span_w * 0.94)
                px = max(4.0, px * fit / word_w)
                img, pad = bake(px, span_w + int(px * 2))
            else:
                # pad1, not pad: pad is not bound on this path. It went unseen
                # behind PARVAGUES (long enough to always take the fit branch
                # at every geometry the selftest tries) — a SHORTER wordmark,
                # i.e. exactly what the brand override invites, lands here.
                # pad1 came from the probe bake at the same px, and bake's
                # margin is a pure function of px, so it is the same number
                # this bake is about to use.
                img, pad = bake(px, word_w + pad1 * 2)
            # crop to the true ink so the margins are the cell's, no more
            left = 0
            w_ = img.width()
            while left < w_ and not self._col_has_ink(img, left):
                left += 1
            right = w_ - 1
            while right > left and not self._col_has_ink(img, right):
                right -= 1
            cropped = img.copy(max(0, left - pad // 2), 0,
                               min(w_, right - left + 1 + pad),
                               img.height())
            pm = QtGui.QPixmap.fromImage(cropped)
            self._word_pm, self._word_pm_key = pm, key
            return pm

        def _col_has_ink(self, img, x):
            return any(img.pixelColor(x, y).alpha() > 8
                       for y in range(img.height()))

        def _paint_word(self, p):
            """PARVAGUES over D1-D3 at the cells' own margin — the span is
            three columns' inner rectangle (PLN: "should fit in d1-d3 not
            just d1 its gtoo tiny atm" — one cell made the type a chip; three
            is the banner)."""
            span_w = max(20.0, 3 * self.cw - self.inset)
            span_h = max(8.0, self.ch - self.inset)
            pm = self._word_pixmap(span_w, span_h)
            if pm is None:
                return
            row_y = self.my + 3 * self.ch          # PHYSICAL_ORDER index of D
            p.setOpacity(WORD_ALPHA)
            try:
                # clamped to the span's outer rect: at the smallest windows
                # the glow ring can reach 1px past the inner margin, and a
                # 1px lean beats 1px of overlap
                p.drawPixmap(max(round(self.mx),
                                 round(self.mx + (span_w - pm.width()) / 2.0)),
                             round(row_y + (self.ch - pm.height()) / 2.0), pm)
            finally:
                p.setOpacity(1.0)

        def _paint_wave(self, p, w, h):
            pm = self._wave_pixmap()
            if pm is None:
                return
            # PLN, 2026-09-24: "right now when i shrink it overlaps with wave,
            # should never, position them as we shrink/grow smartly". The word
            # is BOLTED to D1-D3 — that is its meaning — so the wave is the
            # one that yields: its size clamps to whatever stands right of the
            # word's span. At any window size, no overlap, by construction.
            side = int(h * 0.92)
            if self.word:
                clear = w - (self.mx + 3 * self.cw) - 0.5 * self.cw
                side = min(side, int(clear))
            if side < 24:
                return                  # too narrow to be anything but noise
            scaled = pm.scaled(side, side,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            p.setOpacity(WAVE_ALPHA)
            try:
                # Bottom-right, over the finished reading: the digits live
                # top-left, the brand lives where the eye rests. Under the
                # scanlines, so the texture stays the canvas's own.
                p.drawPixmap(w - scaled.width(), h - scaled.height(), scaled)
            finally:
                p.setOpacity(1.0)

        def set_word(self, on: bool):
            self.word = bool(on)
            self._sync_menu()
            self.update()

        def toggle_word(self):
            self.set_word(not self.word)

        # ── frame ──────────────────────────────────────────────────────────
        def tick(self):
            if _QUIT.is_set():
                self.close()
                return
            now = time.monotonic()
            if self.reader is not None:
                evs = self.reader.drain()
                if evs and not self.paused:
                    for ev in evs:
                        self.ingest(ev)
                elif evs:
                    self.last_event_t = now       # stay awake while paused but fed

            if self.hl is not None:
                hevs = self.hl.drain()
                if hevs and not self.paused:
                    self.ingest_hl(hevs)

            if now - self._bucket_t >= 0.1:
                self.hist.append(self._bucket_n)
                self._bucket_n = 0
                self._bucket_t = now

            if self.drops:                        # rain falls; dead drops go
                dt = self._interval / 1000.0
                rows = self.gut_rows
                self.drops = [d for d in self.drops
                              if (d.__setitem__(1, d[1] + dt * 9.0) or True)
                              and d[1] < rows + 2 and now - d[2] < 4.0]

            self._spec_tick(now)
            # One stat() per frame. Deliberately NOT allowed to hold the window
            # at the active frame rate: resting state is static, so picking a
            # change up on the next paint — within 200 ms even at the idle
            # rate — is the whole budget it deserves.
            self.state.poll()

            idle = (now - self.last_event_t) > IDLE_AFTER and not self.drops
            # Hidden to the tray is the deepest idle there is: nothing is on
            # screen to be behind, so the two-frame-rate rule says take the
            # slow one and give the core back to the audio.
            if not self.isVisible():
                idle = True
            want = FPS_IDLE if idle else FPS_ACTIVE
            # A third rate, and only when earned. The two-rate rule exists so a
            # silent window is not a busy loop; a MOVING backdrop at 5 fps is
            # visibly a slideshow, so while the mix is actually making sound the
            # idle rate becomes the analyser's own 18 Hz -- never the 25 fps
            # active rate, and never at all once the audio goes quiet too.
            if idle and self.spectro and (now - self._spec_hot_t) <= IDLE_AFTER:
                want = FPS_SPECTRO
            if want != self._interval:
                self._interval = want
                self.timer.setInterval(want)
            self.frames += 1
            self.update()

        # ── the backdrop's switch and its clock ────────────────────────────
        def set_spectro(self, on: bool, source: "SpectrumSource | None" = None):
            """Acquire or release the audio tap. Idempotent.

            `source` is an injection point for the tests: pass one and no
            subprocess is ever spawned. Off genuinely tears the capture down --
            "hidden but still consuming" is the state this whole feature is
            not allowed to have.
            """
            on = bool(on)
            if on == self.spectro:
                return
            self.spectro = on
            if not on:
                src, self.spec_src = self.spec_src, None
                if src is not None:
                    src.close()
                self._spec_frame = None
                self._spec_peak = []
                self.update()
                return
            if source is not None:
                self.spec_src = source
            else:
                try:
                    self.spec_src = SpectrumSource().start()
                except ImportError as exc:       # numpy absent: say so, stay off
                    self.spectro = False
                    self.spec_err = "numpy?"
                    print("midiviz: no spectrum backdrop (%s)" % (exc,),
                          file=sys.stderr)
                    return
            self.spec_err = ""
            self._spec_hot_t = time.monotonic()
            self.update()

        def _spec_tick(self, now: float) -> None:
            """Swap in the newest analysed frame. Depth one, never blocking."""
            src = self.spec_src
            if src is None:
                return
            frame = src.latest()
            if frame is None:
                # No frame yet. If the capture already gave a reason, say it
                # ONCE on stderr: a backdrop that silently never appears (no
                # `pw-record`, no sink) is indistinguishable from one that is
                # merely switched off, and the window itself has no words to
                # tell them apart with.
                if src.err and src.err != self.spec_err:
                    self.spec_err = src.err
                    print("midiviz: spectrum backdrop is not getting audio (%s)"
                          % (src.err,), file=sys.stderr)
                return
            self.spec_err = src.err
            self._spec_frame = frame
            if len(self._spec_peak) != len(frame):
                self._spec_peak = list(frame)
            else:
                pk = self._spec_peak
                for i, v in enumerate(frame):
                    pk[i] = v if v > pk[i] else pk[i] * 0.93
            if max(frame) > 0.04:
                self._spec_hot_t = now

        def _lv(self, age: float, floor: int = 0, tau: float = TAU) -> int:
            """Age in seconds → LUT level. The only decay shape in the file;
            the time constant is a parameter so the column can outlive the cells."""
            if age < 0:
                return LEVELS - 1
            lv = int((LEVELS - 1) * math.exp(-age / tau))
            return max(floor, min(LEVELS - 1, lv))

        # ── paint ──────────────────────────────────────────────────────────
        def paintEvent(self, _e):
            p = QtGui.QPainter(self)
            try:
                p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
                p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
                w, h = self.width(), self.height()
                p.fillRect(0, 0, w, h, self.bg)
                now = time.monotonic()
                # the slow dim pulse: the only thing that moves when it is silent
                pulse = 0.5 + 0.5 * math.sin((now - self.t0) * 0.55)
                # FIRST, over the background and under everything else. The
                # ordering IS the feature: PLN asked for it "behind".
                if self.spectro:
                    self._paint_spectrum(p, w, h)
                self._paint_chrome(p, w, h, pulse)
                self._paint_header(p, now, pulse)
                self._paint_controls(p)
                self._paint_matrix(p, now, pulse)
                self._paint_state(p)
                self._paint_gutter(p, now, pulse)
                self._paint_stream(p, now)
                # The wave is a WATERMARK, not a backdrop: over the finished
                # reading, under the texture. See the WAVE_PATH note — behind
                # the opaque cells it survived only in the margins.
                if self.wave:
                    self._paint_wave(p, w, h)
                if self.word:
                    self._paint_word(p)
                self._scanlines_blit(p)
                self.painted += 1
            finally:
                p.end()

        # ── the backdrop ───────────────────────────────────────────────────
        # Named `_paint_spectrum` and checked against every other method on
        # this class before being written: `_paint_chrome` had to become
        # `_paint_controls` because a second method quietly took the first
        # one's name and the selftest went on passing while a whole layer
        # stopped being drawn. A shadowed painter is invisible until a gig.
        #
        # Bars, bottom-anchored, spanning the FULL width -- low left to high
        # right. Not a widget in a corner: a wash the glyphs sit on. `bands`
        # fillRects and `bands` more for the peak hold is the entire cost, and
        # every colour is a LUT hit.
        def _paint_spectrum(self, p, w, h) -> int:
            frame = self._spec_frame
            if not frame:
                return 0
            n = len(frame)
            peak = self._spec_peak
            top = self.hdr_y + self.hdr_h        # never under the header text
            span = max(8, h - top)
            bw = w / float(n)
            drawn = 0
            for i, v in enumerate(frame):
                x0 = int(i * bw)
                x1 = int((i + 1) * bw)
                bar = int(v * span * 0.92)
                if bar >= 1:
                    lv = int(v * (SPEC_LEVELS - 1) + 0.5)
                    p.fillRect(x0, h - bar, max(1, x1 - x0 - 1), bar,
                               self.spec_lut[lv])
                    drawn += 1
                if i < len(peak):
                    pk = int(peak[i] * span * 0.92)
                    if pk >= 2:
                        p.fillRect(x0, h - pk, max(1, x1 - x0 - 1), 1,
                                   self.spec_peak_col)
            return drawn

        def _paint_chrome(self, p, w, h, pulse):
            p.setPen(self.chrome[2 + int(3 * pulse)])
            p.drawRect(0, 0, w - 1, h - 1)
            # The four corner ticks were always here; they are now also the
            # resize affordance, because the alternative is new furniture on a
            # surface that is looked at for two hours. Quiet at rest, brighter
            # and longer while the pointer is in a grab band — an affordance
            # that costs nothing when nobody is reaching for it.
            hot = bool(self._grab_hot)
            p.setPen(self.chrome[13 if hot else 6 + int(5 * pulse)])
            n = max(4, round(8 * self.dens))
            if hot:
                n = round(n * 1.6)
            for cx, cy, dx, dy in ((0, 0, 1, 1), (w - 1, 0, -1, 1),
                                   (0, h - 1, 1, -1), (w - 1, h - 1, -1, -1)):
                p.drawLine(cx, cy, cx + dx * n, cy)
                p.drawLine(cx, cy, cx, cy + dy * n)
            # Three hairlines in the bottom-right, the one corner every desktop
            # has trained a hand to reach for. Dim as the CC reference layer:
            # findable when looked for, never competing with the values.
            p.setPen(self.chrome[11 if hot else 5])
            step = max(3, round(3 * self.dens))
            for k in range(3):
                o = step * (k + 1)
                p.drawLine(w - 2 - o, h - 2, w - 2, h - 2 - o)

        # ── the two controls, top right ────────────────────────────────────
        #
        # PLN: "needs basic menu or at least top right X to close ... maybe X
        # and a [x] where x goes and comes and is the 'stick above' feature".
        # So: no menu. Two glyph-sized targets in the header's right edge, in
        # the same monospace micro font as everything else -- the pin reads as
        # a checkbox whose x is literally the state, and the close is an X.
        #
        # Geometry lives in one place and both the painter and the hit-test
        # call it, because a control drawn at one position and clickable at
        # another is worse than no control at all.
        MIN_TARGET = 18               # px: a trackpad-sized hit target

        # Laid out from the RIGHT EDGE inwards, targets first and glyphs
        # centred inside them -- not the other way round. Sizing the target to
        # the glyph is what produced an 8px-wide X: fine to look at, unhittable
        # mid-set, and at 0.6 scale the two targets then had to overlap or
        # shrink below usable. Deciding the targets first makes "always
        # disjoint and never smaller than MIN_TARGET" true by construction at
        # every zoom level, and the text simply follows.
        def _ctrl_rects(self):
            """(pin_rect, close_rect), disjoint and hittable at any scale."""
            h = max(self.hdr_h + 2 * self._t_pad, self.MIN_TARGET)
            y = self.hdr_y + (self.hdr_h - h) // 2
            cw = max(self.MIN_TARGET, round(self.uw * 2))
            pw = max(self.MIN_TARGET, round(self.uw * 3))
            close = QtCore.QRect(self.gut_x - cw, y, cw, h)
            pin = QtCore.QRect(close.left() - 2 - pw, y, pw, h)
            return pin, close

        def _ctrl_left(self):
            """Where the controls begin — the sparkline must stop here."""
            return self._ctrl_rects()[0].left()

        def _paint_controls(self, p):
            p.setFont(self.f_micro)
            # Dim like the CC reference layer: findable, never competing with
            # the values the eye tracks while playing. Brighter under the
            # pointer, which is the only affordance a frameless window has.
            p.setPen(self.chrome[12 if self._chrome_hot else 6])
            pin, close = self._ctrl_rects()
            base = round(self.hdr_y + self.u_asc)
            pin_txt = "[x]" if self.on_top else "[ ]"
            p.drawText(pin.left() + max(0, (pin.width()
                                            - round(self.uw * 3)) // 2),
                       base, pin_txt)
            p.drawText(close.left() + max(0, (close.width()
                                              - round(self.uw)) // 2),
                       base, "X")

        def _paint_header(self, p, now, pulse):
            p.setFont(self.f_micro)
            lv = 5 + int(4 * pulse) if now - self.last_event_t > IDLE_AFTER else 12
            p.setPen(self.chrome[lv])
            total = self.reader.total if self.reader else self.ingested
            # port address digits + a hex event counter. No words.
            head = "%s│%06X" % (self.port_label, total & 0xFFFFFF)
            # The readout is DECIMAL and carries the corpus's own caret prefix:
            # what shows up here is literally what gets typed in a .tidal file
            # (^53), not a hex value needing conversion mid-set.
            if self.last_cc is not None:
                cc, v, chn, t = self.last_cc
                head += "│^%d=%d" % (cc, v)
                if chn:
                    head += "/%d" % chn
            y = self.hdr_y + self.u_asc
            p.drawText(self.pad, round(y), head)

            # sparkline of events/100 ms, most recent at the right
            hx = self.pad + round(self.uw * (len(head) + 1))
            bw = max(1, round(self.dens))
            # stop short of the chrome, or the bars draw under the controls
            right = self._ctrl_left() - round(self.uw)
            avail = max(4, int(max(0, right - hx) / (bw + 1)))
            bars = list(self.hist)[-avail:]
            top = self.hdr_y
            bot = self.hdr_y + self.hdr_h - 1
            hh = max(2, bot - top)
            peak = max(4, max(bars) if bars else 4)
            x = hx
            for n in bars:
                if n:
                    bh = max(1, round(hh * min(1.0, n / peak)))
                    p.fillRect(x, bot - bh, bw, bh, self.lut[FAM_FX][6 + min(9, n)])
                x += bw + 1

            # state pips instead of words: paused, stream broken
            pw = max(2, round(2 * self.dens))
            if self.paused:
                p.fillRect(self.gut_x, top + 1, pw, hh - 1, self.lut[FAM_GATE2][14])
            if self.reader is not None and self.reader.error:
                p.fillRect(self.gut_x + pw * 2, top + 1, pw, hh - 1,
                           self.lut[FAM_FAMILY][13])
            # The third pip: an LCXL3 read RAW. Every cell on screen is then in
            # the wrong place, and until this existed the only thing saying so
            # was the port label two inches to the left. No words, like the
            # other two — but it points at the label, which is the fix.
            if self.raw_port:
                p.fillRect(self.gut_x + pw * 4, top + 1, pw, hh - 1,
                           self.lut[FAM_FX2][14])

        def _col_geo(self, now):
            """Uniform columns. The width experiment is RETIRED, and its
            verdict is recorded so nobody rebuilds it in a month: PLN asked
            for a 2× focus column (2026-09-24), then, seeing it — "imo the
            fact you can see a C1 move overlap C2 is bad taste" and "can we
            fix this for a single sized thing for each?" He is right, and the
            reason generalizes: a bar whose width depends on WHEN you look at
            it is not a reading. A cell owns a fixed rectangle; a bar at 50%
            must mean 50% against the same ruler every time.
            """
            out, x = [], self.mx
            for _ in range(8):
                out.append((x, self.cw))
                x += self.cw
            return out

        def _paint_matrix(self, p, now, pulse):
            mx, my, ch = self.mx, self.my, self.ch
            cw = self.cw                          # the resting width, for chrome
            geo = self._col_geo(now)
            mx, my, cw, ch = self.mx, self.my, self.cw, self.ch
            # per-orbit charge glow behind each column: "this orbit is live"
            for col in range(1, 9):
                if not self.col_t[col]:
                    continue
                lv = self._lv(now - self.col_t[col], tau=TAU_COL)
                if lv > 1:
                    x, wcol = geo[col - 1]
                    # A SURFACE, not ink: this is the slab a live column stands
                    # on. Sampled from the ink ramp it would paint a dark bar
                    # over a white page and hide the very column it advertises.
                    p.fillRect(round(x), round(my),
                               max(1, round(wcol) - self.inset), round(self.mh_px),
                               self.tint[FAM_LEVEL][min(2, lv // 6)])

            p.setFont(self.f_micro)
            # column digits = orbit numbers, brightening with that orbit's activity
            col_base = self.col_y + self.u_asc
            for col in range(1, 9):
                lv = self._lv(now - self.col_t[col], floor=4, tau=TAU_COL) \
                    if self.col_t[col] \
                    else 4 + int(2 * pulse)
                x, wcol = geo[col - 1]
                p.setPen(self.lut[FAM_LEVEL][min(LEVELS - 1, lv)])
                p.drawText(round(x + (wcol - self.uw) / 2),
                           round(col_base), HEX[col])
            # row letters: the grid's own single-glyph names, on the digits' baseline
            p.setPen(self.chrome[4 + int(3 * pulse)])
            for r, row in enumerate(grid.PHYSICAL_ORDER):
                p.drawText(self.pad, round(my + r * ch + self.u_asc + 1), row)

            # CC numbers, one dim decimal per mapped cell, top-right.
            #
            # Drawn HERE, in its own pass, because f_micro is already the current
            # font: a per-cell setFont inside the main loop would add 96 font
            # switches a frame to a render path this file deliberately keeps to a
            # counted number of drawText calls.
            #
            # Dim on purpose -- this is a reference layer, not a reading. It is
            # the map you consult while wiring; the value digits stay the thing
            # your eye tracks while playing. Skipped entirely when the cell is
            # too narrow to hold the digits without colliding with the value.
            # Room is judged on the NARROWEST column, so a reference digit
            # that fits in one cell fits in all of them — a mixed-width pass
            # would read as different maps per row.
            cc_room = min(round(geo[i][1]) for i in range(8)) - self.inset \
                >= self.uw * 2 + 8
            if cc_room:
                p.setPen(self.chrome[5])
                for r, row in enumerate(grid.PHYSICAL_ORDER):
                    for col in range(1, 9):
                        cc = grid.CELL_TO_CC.get((row, col))
                        if cc is None:
                            continue
                        x, wcol = geo[col - 1]
                        txt = "%d" % cc
                        p.drawText(
                            round(x + (round(wcol) - self.inset)
                                  - self.uw * len(txt) - 2),
                            round(my + r * ch + self.u_asc + 1), txt)

            two = min(round(geo[i][1]) for i in range(8)) > self.mw * 2.4
            p.setFont(self.f_main)
            for r, row in enumerate(grid.PHYSICAL_ORDER):
                for col in range(1, 9):
                    cc = grid.CELL_TO_CC.get((row, col))
                    if cc is None:
                        continue
                    x, wcol = geo[col - 1]
                    x = round(x)
                    y = round(my + r * ch)
                    cwi = max(2, round(wcol) - self.inset)
                    chi = max(2, round(ch) - self.inset)
                    fam = self._fam_for_cc(cc)
                    st = self.cc.get(cc)
                    if st is None:
                        # FLOOR TINT, never black and never invisible: a
                        # mapped control that simply has not moved must not
                        # read as unmapped. On a light page that means a wash
                        # BELOW the page white, which is what tint[..][0] is.
                        p.fillRect(x, y, cwi, chi, self.tint[fam][0])
                        p.setPen(self.chrome[3])
                        p.drawText(x + 2, y + chi - 3, "··" if two else "·")
                        continue
                    v, t, chn = st
                    lv = self._lv(now - t, floor=2)
                    # heat: the value tints the cell body even when cold
                    p.fillRect(x, y, cwi, chi, self.tint[fam][int(3 * v / 127)])
                    bar_h = max(2, round(chi * self.theme.bar_frac))
                    if cc in grid.BUTTON_CCS:
                        # buttons are latches: filled above half, hollow below
                        if v >= 64:
                            p.fillRect(x + 1, y + chi - bar_h - 1, cwi - 2, bar_h,
                                       self.lut[fam][lv])
                        else:
                            p.setPen(self.lut[fam][max(3, lv - 5)])
                            p.drawRect(x + 1, y + chi - bar_h - 1,
                                       max(1, cwi - 3), max(1, bar_h - 1))
                    else:
                        p.fillRect(x + 1, y + chi - bar_h - 1,
                                   max(1, round((cwi - 2) * v / 127)), bar_h,
                                   self.lut[fam][lv])
                    # the channel shifts the hue of the digits, not of the cell
                    p.setPen(self.lut[(fam + chn) % 8][lv])
                    p.drawText(x + 2, y + self.m_asc,
                               ("%02X" % v) if two else HEX[v >> 3])
                    # The HL edge, LAST so nothing paints over it: a brief
                    # border at this control's own family hue, decaying on the
                    # cells' shared curve. It says "the set just played through
                    # me" — never a value, never a touch, which is why it is a
                    # ring and not a fill: a fill would lie about either.
                    ht = self.hl_t.get(cc)
                    if ht is not None:
                        hlv = self._lv(now - ht)
                        if hlv > 0:
                            p.setPen(self.lut[fam][hlv])
                            p.setBrush(Qt.BrushStyle.NoBrush)
                            p.drawRect(x, y, max(1, cwi - 1), max(1, chi - 1))

        # ── state, not events ──────────────────────────────────────────────
        # The whole surface now, not just the three knobs the first slice drew.
        #
        # PLN, 2026-09-22: "D doesnt show the faders mapped tot he cells, and
        # pressing D E F vbuttons also doesnt lit EF which tbh E should be sticky
        # to track current real E button state so people 'get' that E1 is four on
        # the floor intuitively". The first cause of that was the driver being
        # down (midiviz was reading the untranslated board, where the faders
        # carry CCs 5-12 and land in no cell at all). The second is this layer:
        # an EVENT-only picture has nothing to say about a control until the
        # control moves, so a freshly launched window shows eight dots where the
        # faders are, whatever the faders are actually set to.
        #
        # So every cell the surface publishes is drawn — but ONLY where the event
        # layer cannot speak for it, which is the one thing that keeps the two
        # from becoming two pictures of the same fact. A cell whose last event
        # value equals the surface value is skipped entirely: the event bar has
        # it, brighter and with digits. What is left is exactly the gap — a
        # control not touched since launch, and a control the surface moved while
        # this window was not listening (a driver restart, a `--paint` reseed,
        # another client on the port).
        #
        # E is sticky for free, at the right layer: v3 DAW buttons are momentary,
        # so `lcxl3-driver` owns the latch (press flips, release ignored) and
        # publishes 127 until the next press. Nothing here has to remember
        # anything, which is why it cannot drift out of step with the board.
        #
        # WHAT A ROW-D STRIP MEANS. Those faders are Ardour's, and the driver has
        # no readback for them — so a strip in row D is Ardour's own reported
        # position, arriving on the feedback port, and NO strip means nobody has
        # told us yet. The driver omits an Ardour control it has never observed
        # rather than publishing its seed of 0, so this layer can never draw
        # eight faders resting at zero on a mix whose faders are up.
        #
        # Drawn in the cells the controls physically occupy (C1/C2/C3) rather
        # than in a strip of its own, for two reasons. The layout PLN just
        # signed off on does not move; and this module's founding idea is that
        # "an event's identity is its POSITION" — so state belongs at the same
        # position as the events about it, which is where his hands are.
        #
        # ZERO IS THE CENTRE DETENT, VALUE 64 — not 0. gDJF is built as lpf and
        # hpf sections that are both wide open at ch=0.5, so a 0-127 bar would
        # say the wrong thing about the one value that matters most. Three zones
        # with the detent drawn say it, and it matches the model the hardware
        # trains into the hand.
        #
        # Static at rest ON PURPOSE: no decay, no pulse. That is the entire
        # complaint being answered — this layer must still be readable when
        # nothing has moved for a minute.
        def _paint_state(self, p):
            st = getattr(self, "state", None)
            if st is None or not st.values:
                return
            rows = {row: r for r, row in enumerate(grid.PHYSICAL_ORDER)}
            mx, my, cw, ch = self.mx, self.my, self.cw, self.ch
            cwi = max(2, round(cw) - self.inset)
            chi = max(2, round(ch) - self.inset)
            # The event bar owns the bottom `ebar + 1` pixels of every cell and
            # is LEFT-anchored — which is precisely the misleading picture: a
            # family filter resting at its centre detent draws a half-width bar
            # that reads as "half of something". So the state strip sits just
            # ABOVE it, centre-anchored, and the two never share a pixel. The
            # first version of this drew into the same row and the three cells
            # rendered as mud (caught by looking at it, not by reasoning).
            ebar = max(2, round(chi * self.theme.bar_frac))
            # 3 units, not 2: this layer's whole job is being legible when
            # nothing has moved, in sunlight, at a glance. Thin enough to stay
            # subordinate to the value digits, thick enough to read across the
            # room — checked at 900x560 and 1600x1000 before being called done.
            sh = max(3, round(3 * self.dens))
            for cc, val in st.values.items():
                cell = grid.CC_TO_CELL.get(cc)
                if not cell or cell[0] not in rows:
                    continue
                # The family filters are drawn ALWAYS: their whole complaint was
                # that a left-anchored bar lies about a centre detent, and that
                # lie is told by the event layer too. Everything else defers.
                detent = grid.CC_ROLE.get(cc, ("", 0))[0] == "family_filter"
                ev = self.cc.get(cc)
                if not detent and ev is not None and ev[0] == val:
                    continue
                row, col = cell
                x = round(mx + (col - 1) * cw) + 1
                w = max(4, cwi - 2)
                y = round(my + rows[row] * ch) + chi - ebar - 2 - sh
                if y <= round(my + rows[row] * ch):
                    continue                      # cell too short to say it honestly
                # the track the control travels: without it, a marker at an edge
                # and an absent marker look the same
                p.fillRect(x, y, w, sh, self.tint[FAM_OTHER][2])
                tick = max(1, round(self.dens))
                if detent:
                    # THE DETENT, at the centre. Zero is 64, not 0 — gDJF is an
                    # lpf and an hpf section both wide open at ch=0.5, so the
                    # centre is bypass and the ends are the extremes. This tick
                    # is the whole reason the strip exists.
                    mid = x + w // 2
                    p.fillRect(mid, y - 1, tick, sh + 2, self.chrome[8])
                    # where it RESTS, as a bar growing out of the detent toward
                    # the side it is filtering: direction says LOW or HIGH, no
                    # decoding.
                    pos = x + round((w - 1) * max(0.0, min(1.0, val / 127.0)))
                    at_zero = abs(val - 64) <= ZERO_ZONE
                    fam = FAM_LEVEL if at_zero else FAM_FAMILY
                    if not at_zero:
                        lo, hi = (mid, pos) if pos >= mid else (pos, mid)
                        p.fillRect(lo, y, max(1, hi - lo), sh, self.lut[fam][11])
                    # the resting position itself, brightest: the eye lands here
                    p.fillRect(min(pos, x + w - tick), y - 1, tick, sh + 2,
                               self.lut[fam][15])
                elif cc in grid.BUTTON_CCS:
                    # A latch is not a position, so it gets no marker travelling
                    # a track: it is the whole strip, lit or unlit. An OFF latch
                    # therefore looks exactly like a latch nobody has pressed —
                    # which is correct, because on this surface they ARE the same
                    # state, and inventing a distinction would be the picture
                    # lying to look informative.
                    if val >= 64:
                        p.fillRect(x, y, w, sh, self.lut[self._fam_for_cc(cc)][13])
                else:
                    # Knobs and faders: left-anchored, 0 at the left, the same
                    # geometry the event bar uses — deliberately, so a cell that
                    # hands over from state to events does not appear to jump.
                    fam = self._fam_for_cc(cc)
                    pos = x + round((w - 1) * max(0.0, min(1.0, val / 127.0)))
                    if pos > x:
                        p.fillRect(x, y, pos - x, sh, self.lut[fam][10])
                    p.fillRect(min(pos, x + w - tick), y - 1, tick, sh + 2,
                               self.lut[fam][15])

        def _paint_gutter(self, p, now, pulse):
            gx, gw = self.gut_x, self.gut_w
            p.fillRect(gx, round(self.my), gw, round(self.mh_px), self.tint[FAM_OTHER][0])
            p.setPen(self.chrome[3 + int(3 * pulse)])
            p.drawLine(gx, round(self.my), gx, round(self.my + self.mh_px))
            if not self.drops:
                return
            p.setFont(self.f_micro)
            limit = self.my + self.mh_px
            for sub, ypos, born, g, fam in self.drops:
                lv = self._lv(now - born)
                if lv < 2:
                    continue
                y = round(self.my + ypos * self.uh + self.u_asc)
                if y > limit:
                    continue
                # Three lanes across whatever room the TEXT leaves, rather than
                # three fixed thirds of the gutter. A single glyph jitters as
                # widely as it always did; `^42` is three glyphs in a gutter five
                # wide, so its lanes collapse to almost nothing and it can never
                # be drawn off the right edge. One formula, no branch on length.
                span = max(0.0, gw - 4 - self.uw * len(g))
                x = round(gx + 2 + sub * span / 2.0)
                p.setPen(self.lut[fam][lv])
                p.drawText(x, y, g)
                if lv > 11:                      # one trailing glyph sells the fall
                    p.setPen(self.lut[fam][lv - 7])
                    p.drawText(x, round(y - self.uh), g)

        def _paint_stream(self, p, now):
            """The ribbon, drawn in RUNS rather than one call per glyph.

            One drawText per glyph was 2.1 ms of an 8.9 ms frame. Consecutive
            entries sharing a colour AND a decay level are concatenated into one
            call — which is legitimate only for hex glyphs, because those are
            guaranteed to come from the monospaced face at exactly `self.mw`
            advance. A class glyph (◆ ≈ ≡ …) may be served by a fallback face
            with a different advance, so those still draw one at a time or the
            ribbon would drift out of its grid.
            """
            if not self.stream:
                return
            p.setFont(self.f_main)
            mw = self.mw
            x = float(self.pad)
            y = round(self.foot_y + self.m_asc)
            run: list[str] = []
            run_x, run_key = x, (0, 0)
            for g, fam, t in self.stream:
                lv = self._lv(now - t, floor=2)
                key = (fam, lv) if g in HEX else None
                if run and key == run_key:
                    run.append(g)
                else:
                    if run:
                        p.setPen(self.lut[run_key[0]][run_key[1]])
                        p.drawText(round(run_x), y, "".join(run))
                        run = []
                    if key is None:
                        p.setPen(self.lut[fam][lv])
                        p.drawText(round(x), y, g)
                    else:
                        run, run_x, run_key = [g], x, key
                x += mw
            if run:
                p.setPen(self.lut[run_key[0]][run_key[1]])
                p.drawText(round(run_x), y, "".join(run))

        # ── input ──────────────────────────────────────────────────────────
        # ── actions ────────────────────────────────────────────────────────
        # One method per thing the window can do, because there are now THREE
        # ways to ask for each: the key, the window's context menu and the tray.
        # The menu is discoverability for what the keyboard already did -- if it
        # were a second implementation, the two would drift, and the one that
        # drifted would be the one PLN reaches for mid-set (the keys are the
        # ones exercised by --selftest; the menu is the one nothing watches).
        def quit_now(self):
            self._user_closed = True
            self.close()

        def restart(self):
            """Re-exec in place — same process, same argv, fresh code.

            PLN, 2026-09-24: "add to mini menu a restart entry so we can
            reload easily on such updates". midiviz iterates fast and its unit
            restarts it on failure, but a code update needs a hand reload, and
            the old path was Quit and pray the converger agreed. os.execv
            replaces the process WITHOUT exiting, so systemd never sees an
            exit code at all: no latch, no restart counter, no window flap —
            the lens blinks and comes back with the new source. Everything
            with a thread or a subprocess is torn down first, exactly as
            closeEvent would, because none of it survives execv.
            """
            self.timer.stop()
            if self.rebind_timer is not None:
                self.rebind_timer.stop()
            if self.reader is not None:
                self.reader.close()
            if self.hl is not None:
                self.hl.close()
                self.hl = None
            if self.spec_src is not None:
                self.spec_src.close()
                self.spec_src = None
            self._save()
            if self.tray is not None:
                self.tray.hide()
            # If the exec itself fails, the exception escapes and the process
            # dies non-78 — which Restart=always reads as "a failure", and
            # brings the lens back anyway. There is no path to a blank desk.
            os.execv(sys.executable, [sys.executable] + sys.argv)

        def toggle_pause(self):
            self.paused = not self.paused
            self._sync_menu()
            self.update()

        def clear(self):
            self.cc.clear()
            self.stream.clear()
            self.drops.clear()
            self._drop_t.clear()
            self.col_t = [0.0] * 9
            self.hist = deque([0] * 64, maxlen=64)
            self.update()

        def toggle_spectro(self):
            # The runtime half of "off by default": `s` both acquires the
            # tap and gives it back, so nothing about the audio graph is
            # permanent and nothing needs a restart to undo.
            self.set_spectro(not self.spectro)
            self._sync_menu()

        def keyPressEvent(self, e):
            k = e.key()
            if k in (Qt.Key.Key_Q, Qt.Key.Key_Escape):
                self.quit_now()
            elif k == Qt.Key.Key_T:
                self._set_on_top(not self.on_top)
            elif k == Qt.Key.Key_P:
                self.toggle_pause()
            elif k == Qt.Key.Key_S:
                self.toggle_spectro()
            elif k == Qt.Key.Key_H:
                self.toggle_hl()
            elif k == Qt.Key.Key_W:
                self.toggle_wave()
            elif k == Qt.Key.Key_L:
                self.toggle_word()
            elif k == Qt.Key.Key_R:
                self.restart()
            elif k == Qt.Key.Key_D:
                self.cycle_theme()
            elif k == Qt.Key.Key_C:
                self.clear()
            elif k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self._rescale(+0.15)
            elif k in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
                self._rescale(-0.15)
            elif k == Qt.Key.Key_0:
                self.reset_size()
            else:
                super().keyPressEvent(e)

        def _rescale(self, d):
            """Move the density knob by `d`, and resize NOTHING.

            `+` used to mean "make the window bigger" and therefore also "make
            the type bigger", one press for both. It now means only the second
            one: the size is the WM's (and the corner grabs'), the type is
            this. See the FIT_MIN block.
            """
            s = max(0.6, min(2.4, round((self.scale + d) * 100) / 100))
            if s != self.scale:
                self.scale = s
                self._apply_scale()
                self._sync_menu()
                self._save()
                self.update()

        def _set_scale(self, s):
            self._rescale(s - self.scale)

        # ── the menu, on one surface, owned by two faces ───────────────────
        # PLN, 2026-09-22: "can we have a basic menu for midimon, maybe a tray
        # presence when runnig, with scaling, theme, etc options ?"
        #
        # ONE QMenu object. It is the window's right-click menu (the window is
        # frameless and the whole surface is a drag handle, so right-click is
        # the only gesture left) and it is the tray's context menu, and those
        # are the same menu because there is only one set of things to do.
        #
        # Labels carry their key after a tab, which is the column QMenu renders
        # shortcuts in. Real `setShortcut`s were deliberately NOT used: Qt's
        # shortcut machinery intercepts a key before `keyPressEvent` ever sees
        # it, so installing them would silently move every key onto a path
        # --selftest does not drive. The hint is a hint; the key stays the key.
        def _section(self, menu, title):
            """A dim, unclickable header row. Same idiom as perf-tray's gear
            headers: the separation IS the design, and a reader who sees one
            flat list learns nothing from it twice."""
            a = QtGui.QAction(title, menu)
            a.setEnabled(False)
            menu.addAction(a)
            return a

        def _swatch(self, th):
            """A chip of one theme's page and ink — painted, never loaded.

            From the Theme's own RAMPS and not from `self.lut`: the LUTs only
            ever hold the palette that is currently up, so swatches built from
            them would have been three identical chips, which is worse than
            none (it would say "these look the same" about the one axis PLN
            opens this menu for).
            """
            n = 36
            pm = QtGui.QPixmap(n, n)
            pm.fill(QtGui.QColor(*th.bg))
            p = QtGui.QPainter(pm)
            try:
                p.setPen(Qt.PenStyle.NoPen)
                # Three of the grid's real family hues, at three decay levels:
                # the chip is a miniature of what the page will look like, so
                # `sun`'s near-black ink on near-white and `dark`'s violet glow
                # on black read as the two different pages they are.
                for i, fam in enumerate((FAM_LEVEL, FAM_FX, FAM_NOTE)):
                    val, sat = th.ink.at(1.0 - i * 0.30)
                    p.setBrush(QtGui.QColor.fromHsvF(FAM_HUE[fam] / 360.0, sat, val))
                    p.drawRect(6, 5 + i * 9, n - 13 - i * 4, 6)
                edge = th.chrome or th.ink
                val, sat = edge.at(0.55)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QtGui.QColor.fromHsvF(th.chrome_hue / 360.0, sat, val))
                p.drawRect(0, 0, n - 1, n - 1)
            finally:
                p.end()
            return QtGui.QIcon(pm)

        def _build_menu(self):
            # Parentless, like perf-tray's: the tray has to be able to pop this
            # up while the window is HIDDEN, and a popup whose parent is not
            # on screen is exactly the sort of thing a compositor gets to have
            # an opinion about. The actions are parented to the widget, and
            # `self.menu` is what keeps the menu itself alive.
            self.menu = QtWidgets.QMenu()
            self._theme_acts = {}
            m = self.menu

            # PLN, 2026-09-22: "midimon still unreadable on the sun ... right
            # click on window -> big modern contextual menu to quickly make it
            # color ?"
            #
            # So the theme is not a submenu any more. It is the FIRST thing in
            # the menu, at the top level, one row per page with that page's own
            # colours painted into a swatch — because the reason this menu gets
            # opened at all, outdoors, with a set starting, is "make it
            # readable right now", and a nested Theme ▸ costs a hover, a
            # sub-popup and a second aim at a row labelled with a bare word.
            # Everything else keeps its place below it.
            f = m.font()
            f.setPointSizeF(max(10.5, f.pointSizeF() * 1.2))
            m.setFont(f)

            self._section(m, "◆  PAGE — what it looks like")
            grp = QtGui.QActionGroup(self)
            grp.setExclusive(True)
            for name in THEME_ORDER:
                a = QtGui.QAction(THEME_MENU[name], self)
                a.setCheckable(True)
                a.setIcon(self._swatch(THEMES[name]))
                # Some styles drop an icon on a checkable row and show only the
                # tick. The swatch is the point of the row, so ask for it.
                a.setIconVisibleInMenu(True)
                a.triggered.connect(lambda _c=False, n=name: self.set_theme(n))
                grp.addAction(a)
                m.addAction(a)
                self._theme_acts[name] = a
            self._act_cycle = QtGui.QAction("Next page\td", self)
            self._act_cycle.triggered.connect(lambda _c=False: self.cycle_theme())
            m.addAction(self._act_cycle)

            m.addSeparator()
            # The size axis, and it says out loud which half of it is which:
            # the window's shape is the WM's business, the type's weight is
            # this menu's. Bigger/Smaller sit at the top level because they are
            # the two rows anybody actually clicks twice in a row.
            self._section(m, "◆  TYPE — the window's own size is the WM's:"
                          " drag an edge or a corner")
            for label, delta in (("Bigger type\t+", +0.15), ("Smaller type\t-", -0.15)):
                a = QtGui.QAction(label, self)
                a.triggered.connect(lambda _c=False, d=delta: self._rescale(d))
                m.addAction(a)
            sc = m.addMenu("Density multiplier")
            self._scale_acts = {}
            sgrp = QtGui.QActionGroup(self)
            sgrp.setExclusive(True)
            for s in (0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.4):
                a = QtGui.QAction("%.1f×" % s, self)
                a.setCheckable(True)
                a.triggered.connect(lambda _c=False, v=s: self._set_scale(v))
                sgrp.addAction(a)
                sc.addAction(a)
                self._scale_acts[s] = a
            self._act_reset = QtGui.QAction("Reset size\t0", self)
            self._act_reset.triggered.connect(lambda _c=False: self.reset_size())
            m.addAction(self._act_reset)

            m.addSeparator()
            self._section(m, "◆  STREAM")
            self._act_pause = QtGui.QAction("Pause\tp", self)
            self._act_pause.setCheckable(True)
            self._act_pause.triggered.connect(lambda _c=False: self.toggle_pause())
            m.addAction(self._act_pause)
            a = QtGui.QAction("Clear\tc", self)
            a.triggered.connect(lambda _c=False: self.clear())
            m.addAction(a)
            self._act_spec = QtGui.QAction("Spectrum backdrop\ts", self)
            self._act_spec.setCheckable(True)
            self._act_spec.triggered.connect(lambda _c=False: self.toggle_spectro())
            m.addAction(self._act_spec)
            self._act_hl = QtGui.QAction("Tidal HL feed\th", self)
            self._act_hl.setCheckable(True)
            self._act_hl.triggered.connect(lambda _c=False: self.toggle_hl())
            m.addAction(self._act_hl)
            self._act_wave = QtGui.QAction(f"{BRAND} wave\tw", self)
            self._act_wave.setCheckable(True)
            self._act_wave.triggered.connect(lambda _c=False: self.toggle_wave())
            m.addAction(self._act_wave)
            self._act_word = QtGui.QAction(f"{WORDMARK} lettering\tl", self)
            self._act_word.setCheckable(True)
            self._act_word.triggered.connect(lambda _c=False: self.toggle_word())
            m.addAction(self._act_word)
            self._act_pin = QtGui.QAction("Always on top\tt", self)
            self._act_pin.setCheckable(True)
            self._act_pin.triggered.connect(
                lambda _c=False: self._set_on_top(not self.on_top))
            m.addAction(self._act_pin)

            m.addSeparator()
            self._section(m, "◆  WINDOW")
            self._port_menu = m.addMenu("MIDI source")
            self._act_show = QtGui.QAction("Show window", self)
            self._act_show.setCheckable(True)
            self._act_show.triggered.connect(
                lambda _c=False: self.set_window_shown(not self.isVisible()))
            m.addAction(self._act_show)
            self._act_restart = QtGui.QAction("Restart\tR", self)
            self._act_restart.triggered.connect(lambda _c=False: self.restart())
            m.addAction(self._act_restart)

            m.addSeparator()
            a = QtGui.QAction("Quit\tq", self)
            a.triggered.connect(lambda _c=False: self.quit_now())
            m.addAction(a)

            m.aboutToShow.connect(self._menu_about_to_show)
            self._sync_menu()

        def _menu_about_to_show(self):
            self._sync_ports()
            self._sync_menu()

        def _sync_menu(self):
            """Make the checkmarks tell the truth.

            Called on every state change as well as on aboutToShow, because a
            tray menu is often rendered by the DESKTOP from an exported
            DBusMenu (perf-tray.py records this: "one surface, two renderers")
            and aboutToShow may never fire for it. Syncing eagerly costs a
            handful of setChecked calls and removes a whole class of "the menu
            says dark and the window is white".
            """
            m = getattr(self, "menu", None)
            if m is None:                  # during __init__, before the menu exists
                return
            for name, act in self._theme_acts.items():
                on = name == self.theme.name
                act.setChecked(on)
                # A checkable row that also carries an ICON loses its tick
                # under most Qt styles: the check indicator and the icon share
                # one column, and all that is left is a faint frame around the
                # swatch, which is not a thing anybody reads at a glance in a
                # dark room. So the state is also a glyph in the label — the
                # same reason perf-tray labels its rig rows ● / ○ instead of
                # trusting colour it does not control.
                act.setText(("● " if on else "○ ") + THEME_MENU[name])
            for s, act in self._scale_acts.items():
                act.setChecked(abs(s - self.scale) < 1e-9)
            self._act_pause.setChecked(self.paused)
            self._act_spec.setChecked(self.spectro)
            self._act_hl.setChecked(self.hl is not None)
            self._act_wave.setChecked(self.wave)
            self._act_word.setChecked(self.word)
            self._act_pin.setChecked(self.on_top)
            self._act_show.setChecked(self.isVisible())

        def _sync_ports(self):
            """Rebuild the source submenu from what ALSA has RIGHT NOW.

            Only on aboutToShow: this shells out to `aseqdump -l`, and the one
            thing the rebind tick has taught this file is that a port list is
            true for about two seconds. Read-only, like everything here -- the
            window subscribes to a source and never opens an output.
            """
            pm = getattr(self, "_port_menu", None)
            if pm is None:
                return
            # Every QAction here is parented to the SUBMENU, not to the
            # widget: `clear()` deletes its children, and a right-click that
            # left ten dead QActions attached to the window would accumulate
            # them for the length of a two-hour set.
            pm.clear()
            a = QtGui.QAction("Auto (re-resolve)", pm)
            a.setCheckable(True)
            a.setChecked(self._pinned is None)
            a.triggered.connect(lambda _c=False: self._pin_port(None))
            pm.addAction(a)
            pm.addSeparator()
            try:
                ports = list_ports()
            except Exception:              # ALSA absent is not a menu failure
                ports = []
            for pt in ports:
                label = "%s  %s │ %s" % (pt["addr"], pt["client"], pt["port"])
                a = QtGui.QAction(label, pm)
                a.setCheckable(True)
                a.setChecked(self._pinned == pt["addr"])
                a.triggered.connect(
                    lambda _c=False, pid=pt["addr"]: self._pin_port(pid))
                pm.addAction(a)
            if not ports:
                a = QtGui.QAction("no MIDI sources", pm)
                a.setEnabled(False)
                pm.addAction(a)

        def _pin_port(self, pid):
            """Pin a source, or None to go back to auto, then re-resolve now.

            Reuses `_rebind_tick`, which is the only code that knows how to
            swap a Reader without dropping the window -- picking a port from a
            menu must not become a second, less-tested way to do that.
            """
            self._pinned = pid
            self._rebind_tick()
            self.update()

        def contextMenuEvent(self, e):
            self._menu_about_to_show()
            self.menu.exec(e.globalPos())
            e.accept()

        # ── the tray ───────────────────────────────────────────────────────
        # PLN: "maybe a tray presence when runnig". The point is recall: the
        # lens can be dismissed for a track and brought back for the next one
        # without killing the process, which is what `close` has always meant
        # here ("if i close i wanna close it" -- that stays true, and the X and
        # `q` still really quit; only Show/Hide hides).
        #
        # Guarded on `isSystemTrayAvailable()` and silent when there is none: a
        # monitor that refuses to start because the desktop has no status area
        # would be a new failure mode traded for a convenience.
        def _build_tray(self):
            self.tray = None
            try:
                if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
                    return
                # Held on self: an unreferenced QSystemTrayIcon is garbage
                # collected and the icon simply disappears from the panel.
                self.tray = QtWidgets.QSystemTrayIcon(self)
                self.tray.setIcon(self._tray_icon())
                self.tray.setToolTip("midiviz")
                self.tray.setContextMenu(self.menu)
                self.tray.activated.connect(self._tray_activated)
                self.tray.show()
            except Exception as exc:       # no status area, no D-Bus, no matter
                self.tray = None
                print("midiviz: no tray presence (%s)" % (exc,), file=sys.stderr)

        def _tray_icon(self):
            """A 64px grid in the current theme's own colours, painted not
            loaded: the lens has no icon file and should not acquire one."""
            pm = QtGui.QPixmap(64, 64)
            pm.fill(QtGui.QColor(0, 0, 0, 0))
            p = QtGui.QPainter(pm)
            try:
                p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
                p.setBrush(self.bg)
                p.setPen(self.chrome[8])
                p.drawRoundedRect(2, 2, 60, 60, 12, 12)
                for r in range(3):
                    for c in range(4):
                        fam = (r * 4 + c) % 8
                        p.setBrush(self.lut[fam][11 - 2 * r])
                        p.setPen(Qt.PenStyle.NoPen)
                        p.drawRect(10 + c * 12, 12 + r * 14, 8, 9)
            finally:
                p.end()
            return QtGui.QIcon(pm)

        def _tray_activated(self, reason):
            if reason in (QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
                          QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick):
                self.set_window_shown(not self.isVisible())

        def set_window_shown(self, on: bool):
            """Hide/show, never close. `close()` stops the timers and hands the
            source port back -- that is quitting, and it is a different verb."""
            if on:
                self.show()
                self.raise_()
                self.activateWindow()
            else:
                self.hide()
            self._sync_menu()

        def _set_on_top(self, on: bool):
            self.on_top = on
            self._sync_menu()
            # PLN, 2026-09-24: "pressing t makes the win disappear and never
            # reappear it does kill it". The cause is an ordering trap: Qt
            # HIDES the window the instant setWindowFlag runs, so the old
            # `if self.isVisible(): self.show()` — written after the call —
            # always read False and the re-show never fired. Visibility must
            # be captured BEFORE the flag change: it is the only witness of
            # whether the window was on screen. Qt requires the re-show after
            # a flag change for the compositor to be told; it does not touch
            # the KWin all-desktops rule, which is keyed on the app id.
            was_visible = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            if was_visible:
                self.show()
            self.update()

        # ── the grab zones ─────────────────────────────────────────────────
        # PLN, 2026-09-22: "only drag-> grab atm, can we have corner grags to
        # make it bigger".
        #
        # A hit test against a margin, NOT four QSizeGrips, and the reasons are
        # worth writing down because QSizeGrip is the obvious answer:
        #
        #   * a QSizeGrip is a child WIDGET. This canvas is
        #     WA_OpaquePaintEvent and paints every pixel itself; a grip would
        #     draw its style's little triangle on top of the picture, in the
        #     desktop palette, permanently — furniture on a performance
        #     surface, and invisible or garish depending on the theme;
        #   * a grip only ever gives the CORNERS, and "squeeze it narrower"
        #     means the EDGES. Eight zones out of one margin is the same
        #     dozen lines as four grips;
        #   * one hit test is shared by the press, the hover cursor and the
        #     corner ticks the painter draws, so what is grabbable and what
        #     LOOKS grabbable cannot drift apart. Two mechanisms drifting is
        #     exactly the bug `_ctrl_rects` exists to prevent one size up.
        EDGE_L, EDGE_R = Qt.Edge.LeftEdge, Qt.Edge.RightEdge
        EDGE_T, EDGE_B = Qt.Edge.TopEdge, Qt.Edge.BottomEdge

        def _edges_at(self, pos):
            """Which window edges this point grabs. Qt.Edge(0) means the body.

            Or-ed flags, so a corner is literally two edges — which is also
            what `startSystemResize` wants.
            """
            x, y = pos.x(), pos.y()
            w, h = self.width(), self.height()
            g = GRAB_PX
            e = Qt.Edge(0)
            if 0 <= x < g:
                e |= self.EDGE_L
            elif w - g <= x < w:
                e |= self.EDGE_R
            if 0 <= y < g:
                e |= self.EDGE_T
            elif h - g <= y < h:
                e |= self.EDGE_B
            if not e:
                return e
            # A corner is far easier to aim at than a 7 px strip, and it is the
            # gesture that means "make it bigger", so the last CORNER_PX along
            # the OTHER axis counts as a corner even where only one band was
            # hit. Every desktop does this; a 7×7 px corner would be a joke
            # mid-set.
            horiz, vert = e & (self.EDGE_L | self.EDGE_R), e & (self.EDGE_T | self.EDGE_B)
            if horiz and not vert:
                if y < CORNER_PX:
                    e |= self.EDGE_T
                elif y >= h - CORNER_PX:
                    e |= self.EDGE_B
            elif vert and not horiz:
                if x < CORNER_PX:
                    e |= self.EDGE_L
                elif x >= w - CORNER_PX:
                    e |= self.EDGE_R
            return e

        def _grab_cursor(self, edges):
            """The cursor for a grab zone — the only hover hint a frameless
            window gets, and the thing that says "this edge is live"."""
            C = Qt.CursorShape
            if edges in (self.EDGE_L | self.EDGE_T, self.EDGE_R | self.EDGE_B):
                return C.SizeFDiagCursor
            if edges in (self.EDGE_R | self.EDGE_T, self.EDGE_L | self.EDGE_B):
                return C.SizeBDiagCursor
            if edges in (self.EDGE_L, self.EDGE_R):
                return C.SizeHorCursor
            if edges in (self.EDGE_T, self.EDGE_B):
                return C.SizeVerCursor
            return C.SizeAllCursor          # the body: still drag-to-move

        def _resized_rect(self, r0, d):
            """The geometry a manual drag of `self._resize_edges` asks for.

            Clamped to minimumSize on the way, and the moving edge is the one
            that gives: dragging the LEFT edge right must shrink the window and
            leave the right edge where it is, which means the position moves
            too. That is why this returns a whole rect and not a size.
            """
            x, y, w, h = r0.x(), r0.y(), r0.width(), r0.height()
            mw, mh = max(1, self.minimumWidth()), max(1, self.minimumHeight())
            e = self._resize_edges
            if e & self.EDGE_L:
                nw = max(mw, w - d.x())
                x, w = x + w - nw, nw
            elif e & self.EDGE_R:
                w = max(mw, w + d.x())
            if e & self.EDGE_T:
                nh = max(mh, h - d.y())
                y, h = y + h - nh, nh
            elif e & self.EDGE_B:
                h = max(mh, h + d.y())
            return QtCore.QRect(x, y, w, h)

        def mousePressEvent(self, e):
            if e.button() == Qt.MouseButton.LeftButton:
                # Controls win over the drag. The whole surface is a drag
                # handle (frameless window, no titlebar), so without this the
                # X would only ever move the window.
                pin, close = self._ctrl_rects()
                pos = e.position().toPoint()
                if close.contains(pos):
                    self._user_closed = True
                    self.close()
                    return
                if pin.contains(pos):
                    self._set_on_top(not self.on_top)
                    self._drag_from = None
                    return
                # Controls first, THEN the edges, then the body. The X's hit
                # rect reaches up into the top grab band (its y is the header
                # pad minus the slack), and a click meant for the close must
                # never turn into a resize.
                edges = self._edges_at(pos)
                if edges:
                    self._drag_from = None
                    self._resize_edges = edges
                    wh = self.windowHandle()
                    if wh is not None and wh.startSystemResize(edges):
                        # The compositor owns the drag from here — same deal as
                        # startSystemMove below, and the only path Wayland
                        # allows. Nothing left for us to track.
                        self._resize_edges = Qt.Edge(0)
                        self._resize_from = None
                        return
                    # Only reached where the compositor said no (offscreen, or
                    # an X11 server that refuses): do it by hand from the
                    # press-time geometry. This one moves the window as well as
                    # sizing it, which a Wayland client may not do — hence the
                    # order: ask the compositor first, always.
                    self._resize_from = (e.globalPosition().toPoint(), self.geometry())
                    return
                # Wayland forbids a client positioning itself; startSystemMove is
                # the compositor-blessed path. X11 falls back to a manual move.
                wh = self.windowHandle()
                if wh is not None and wh.startSystemMove():
                    self._drag_from = None
                    return
                self._drag_from = e.globalPosition().toPoint() - self.pos()

        def mouseMoveEvent(self, e):
            pin, close = self._ctrl_rects()
            pos = e.position().toPoint()
            hot = pin.contains(pos) or close.contains(pos)
            # The controls sit inside the top band, so they take the point
            # before the grab test does — the hover hint has to agree with what
            # the press will actually do.
            edges = Qt.Edge(0) if hot else self._edges_at(pos)
            dragging = self._drag_from is not None or self._resize_from is not None
            if not dragging and (hot != self._chrome_hot or edges != self._grab_hot):
                self._chrome_hot, self._grab_hot = hot, edges
                # An arrow over the controls, a resize arrow in a grab zone,
                # the move cursor everywhere else: the cursor is the only hint
                # a frameless window can give.
                self.setCursor(Qt.CursorShape.ArrowCursor if hot
                               else self._grab_cursor(edges))
                self.update()
            if self._resize_from is not None:
                g0, r0 = self._resize_from
                self.setGeometry(
                    self._resized_rect(r0, e.globalPosition().toPoint() - g0))
                return
            if self._drag_from is not None:
                self.move(e.globalPosition().toPoint() - self._drag_from)

        def mouseReleaseEvent(self, _e):
            self._drag_from = None
            self._resize_edges = Qt.Edge(0)
            self._resize_from = None

        def leaveEvent(self, _e):
            """Back to rest when the pointer leaves.

            You leave a frameless window by crossing an EDGE, so the last move
            this widget ever sees is inside a grab band: without this the
            corner ticks stay long and bright and the cursor stays a resize
            arrow for the rest of the set. The affordance is only quiet if it
            also knows how to stop.
            """
            if self._grab_hot or self._chrome_hot:
                self._grab_hot, self._chrome_hot = Qt.Edge(0), False
                self.setCursor(Qt.CursorShape.SizeAllCursor)
                self.update()

        def closeEvent(self, e):
            self.timer.stop()
            if self.rebind_timer is not None:
                self.rebind_timer.stop()
            if self.reader:
                self.reader.close()
            # The HL listener is a bound socket; like the spectro tap below,
            # OFF at close must mean closed, not orphaned.
            if self.hl is not None:
                self.hl.close()
                self.hl = None
            # Hand the audio tap back before anything else. A closed window
            # that left a `pw-record` on the master bus would be exactly the
            # "always-on consumer" this feature promised not to become, and it
            # would outlive the only UI that could have switched it off.
            if self.spec_src is not None:
                self.spec_src.close()
                self.spec_src = None
            # "if i close i wanna close it". A clean exit is not a failure, so
            # systemd will not restart us -- but rig_units.ensure() starts
            # every non-manual unit that is not active, and gig-up and the
            # Bridge watcher both converge, so the window came straight back.
            # Leave a latch ensure() honours. $XDG_RUNTIME_DIR means it dies
            # at logout, which is the right lifetime: closed for this session,
            # present again next login, nothing to remember to undo.
            #
            # Only for a close the HUMAN asked for. A compositor teardown at
            # logout must not be mistaken for an opinion.
            if self._user_closed:
                _latch_close()
            if self.tray is not None:
                self.tray.hide()
            # With a tray in the panel, `main()` turns quitOnLastWindowClosed
            # OFF -- that setting is what makes "hide to tray" possible at all,
            # since Qt otherwise treats the last window going away as the end
            # of the program. The price is that closing must now say so out
            # loud, or the X would dismiss the window and leave the process
            # running with nothing left to show it.
            app = QtWidgets.QApplication.instance()
            if app is not None and not app.quitOnLastWindowClosed():
                app.quit()
            e.accept()

    return MidiViz()


# ── entry points ───────────────────────────────────────────────────────────
def _synthetic_lines() -> list[str]:
    """aseqdump-shaped lines covering the whole grid, several channels, and every
    glyph class — fed through the REAL parser, so --selftest exercises
    parse_line + enrich as well as the drawing."""
    out: list[str] = []
    ccs = [cc for row in grid.PHYSICAL_ORDER for cc in grid.ROW_CCS[row]]
    for i, cc in enumerate(ccs):
        out.append(" 28:0   Control change          %d, controller %d, value %d"
                   % (i % 4, cc, (i * 17) % 128))
    for cc in (7, 93, 120, 64):                  # un-gridded CCs → gutter rain
        out.append(" 28:0   Control change          0, controller %d, value 99" % cc)
    for n in (36, 48, 60, 67, 72):
        out.append(" 28:0   Note on                 1, note %d, velocity %d" % (n, 40 + n // 2))
        out.append(" 28:0   Note off                1, note %d, velocity 0" % n)
    out += [
        " 28:0   Pitch bend              2, value 4096",
        " 28:0   Pitch bend              2, value -6000",
        " 28:0   Program change          3, program 7",
        " 28:0   Channel aftertouch      0, value 64",
        " 28:0   Poly aftertouch         5, note 60, value 31",
        " 28:0   Start",
        " 28:0   Stop",
        " 28:0   System exclusive        f0 00 20 29 f7",
    ]
    return out


def _synthetic_audio(n: int, t0: float, rate: int = SPEC_RATE):
    """A sweeping tone plus noise: enough spectral structure that a picture
    made of it cannot be a flat wash, at an amplitude (-20 dBFS) chosen so the
    bars land mid-scale instead of pinned. Synthetic on purpose -- the selftest
    must be able to prove the ANALYSIS and the PAINT on a box with no audio
    server at all. What it cannot prove is `pw-record`; that half is verified
    by running the real thing.
    """
    import numpy as np
    t = (t0 + np.arange(n, dtype=np.float64) / rate)
    f = 90.0 * (2.0 ** (2.6 * (0.5 + 0.5 * math.sin(t0 * 0.7))))
    sig = 0.09 * np.sin(2 * math.pi * f * t)
    sig += 0.012 * np.random.default_rng(int(t0 * 1000) & 0xFFFF).standard_normal(n)
    return sig.astype(np.float32)


def selftest_spectrum(bands: int = SPEC_BANDS) -> tuple[bool, str]:
    """The backdrop's own checks: real FFT, real bands, real numbers.

    Three properties, each of which a plausible bug breaks:
      * a tone lands in ONE part of the spectrum, not everywhere (band edges
        and the reduceat truncation are both wrong in ways that smear it);
      * digital silence reads as silence (an absent epsilon or a log of zero
        gives NaN, which paints as a full-height bar -- silence must never
        look like signal, the inverse of the "mapped-but-idle" rule);
      * the envelope RISES faster than it FALLS, or the wash lags the music.
    """
    import numpy as np
    src = SpectrumSource(target="__never__", bands=bands)
    rate = src.rate
    t = np.arange(src.fft_n, dtype=np.float64) / rate
    notes = []

    for _ in range(24):                       # let the envelope settle
        loud = src.push(0.25 * np.sin(2 * math.pi * 220.0 * t))
    peak_band = int(np.argmax(loud))
    # Ask the band EDGES where the band is, not the nominal log ramp. Below
    # ~750 Hz the ramp is not what `_spec_band_edges` returns -- bins run out
    # and the low edges become consecutive -- so checking 220 Hz against the
    # ramp reported MISPLACED for a band that is in fact exactly 211-234 Hz.
    # The first version of this assertion was wrong about the code it tested.
    bin_hz = rate / float(src.fft_n)
    lo = float(src.edges[peak_band]) * bin_hz
    hi = float(src.edges[peak_band + 1]) * bin_hz
    placed = lo <= 220.0 <= hi
    notes.append("tone_220Hz->band%d[%.0f-%.0fHz]:%s"
                 % (peak_band, lo, hi, "ok" if placed else "MISPLACED"))
    energy = float(np.sum(loud))
    concentrated = bool(loud[peak_band] > 0.2 and energy < loud[peak_band] * 12)
    notes.append("concentration:%s" % ("ok" if concentrated else "SMEARED"))

    for _ in range(80):                       # release is slow; give it room
        quiet = src.push(np.zeros(src.fft_n, dtype=np.float32))
    silent = bool(np.all(np.isfinite(quiet)) and max(quiet) < 0.02)
    notes.append("silence->%.3f:%s" % (max(quiet), "ok" if silent else "NOT SILENT"))

    rise = src.push(0.25 * np.sin(2 * math.pi * 220.0 * t))[peak_band]
    fall = src.push(np.zeros(src.fft_n, dtype=np.float32))[peak_band]
    asym = bool(rise > 0.05 and fall > rise * 0.5)
    notes.append("attack%.3f>release(kept %.3f):%s"
                 % (rise, fall, "ok" if asym else "BACKWARDS"))

    src.close()                               # no thread was started; must be safe
    ok = placed and concentrated and silent and asym
    return ok, " ".join(notes)


def selftest_config() -> tuple[bool, str]:
    """The persistence layer, against every file a disk can actually hand back.

    Runs entirely inside a throwaway XDG_CONFIG_HOME, so the selftest cannot
    read or overwrite PLN's real choice. The cases are not hypothetical: a
    truncated write, a config from a build where a theme had a different name,
    and a hand-edited scale are all things that happen, and every one of them
    must come back as "use the default", never as a traceback in front of an
    audience.
    """
    import tempfile
    notes, ok = [], True
    prev = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory(prefix="midiviz-cfg-") as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            cases = [
                ("missing", None, (None, None)),
                ("roundtrip", '{"theme": "sun", "scale": 1.5}', ("sun", 1.5)),
                ("corrupt", '{"theme": "sun", "sca', (None, None)),
                ("not-a-dict", '["sun", 1.5]', (None, None)),
                ("unknown-theme", '{"theme": "neon", "scale": 0.8}', (None, 0.8)),
                ("scale-text", '{"theme": "light", "scale": "abc"}', ("light", None)),
                ("scale-nan", '{"theme": "light", "scale": NaN}', ("light", None)),
                ("scale-huge", '{"theme": "light", "scale": 99}', ("light", 2.4)),
                ("empty", "", (None, None)),
            ]
            for name, body, want in cases:
                path = config_path()
                try:
                    if body is None:
                        if path.exists():
                            path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(body)
                    got = load_config()
                    good = (got["theme"], got["scale"]) == want
                except Exception as exc:                 # noqa: BLE001
                    got, good = repr(exc), False
                ok = ok and good
                notes.append("%s:%s" % (name, "ok" if good else "BAD(%r)" % (got,)))
            # A write that cannot land must report False, not raise: the
            # config directory's parent is a FILE here, so mkdir fails.
            wall = Path(tmp) / "wall"
            wall.write_text("not a directory\n")
            os.environ["XDG_CONFIG_HOME"] = str(wall)
            try:
                wrote = save_config("sun", 1.2)
                good = wrote is False and load_config()["theme"] is None
            except Exception as exc:                     # noqa: BLE001
                good = False
                notes.append("unwritable:RAISED(%r)" % (exc,))
            else:
                notes.append("unwritable:%s" % ("ok" if good else "BAD"))
            ok = ok and good
            # And the happy path really does come back after a real save.
            os.environ["XDG_CONFIG_HOME"] = tmp
            good = save_config("light", 1.35) and load_config() == {
                "theme": "light", "scale": 1.35}
            notes.append("save->load:%s" % ("ok" if good else "BAD"))
            ok = ok and good
        finally:
            if prev is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = prev
    return ok, " ".join(notes)


def selftest(seconds: float = 3.0, show: bool = False,
             theme: str = DEFAULT_THEME, shot_dir: str | None = None) -> int:
    if shot_dir:
        os.makedirs(shot_dir, exist_ok=True)   # img.save() no-ops on a missing dir
    if not show:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
    QtCore, QtGui, QtWidgets = _qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # persist=False: the selftest presses `+`, `-` and cycles every theme, and
    # none of that is allowed to become PLN's saved preference.
    w = build_widget("00:0", None, scale=1.0, theme=theme, persist=False)
    w.show()
    app.processEvents()

    # The backdrop, fed by hand. `target` is a name no node can have, so even
    # if `start()` were called nothing would be captured -- but it is not
    # called: `push()` is the seam, and the selftest is the other caller of it.
    cfg_ok, cfg_notes = selftest_config()
    spec_ok, spec_notes = selftest_spectrum()
    spec_src = SpectrumSource(target="__never__")
    w.set_spectro(True, spec_src)
    spec_t = 0.0

    parsed = [midimon.enrich(ev) for ev in
              (midimon.parse_line(ln) for ln in _synthetic_lines()) if ev]
    if not parsed:
        print("selftest FAIL: the parser produced no events", file=sys.stderr)
        return 1

    grabs, i = 0, 0
    colours: set = set()
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        for _ in range(3):                       # a plausible live event rate
            w.ingest(parsed[i % len(parsed)])
            i += 1
        spec_src.push(_synthetic_audio(spec_src.hop, spec_t))
        spec_t += spec_src.hop / float(spec_src.rate)
        w.tick()
        app.processEvents()
        pm = w.grab()                            # forces the real paint path
        grabs += 1
        if grabs % 10 == 1:
            img = pm.toImage()
            sx = max(1, img.width() // 16)
            sy = max(1, img.height() // 16)
            for yy in range(0, img.height(), sy):
                for xx in range(0, img.width(), sx):
                    colours.add(img.pixel(xx, yy))
        time.sleep(0.033)

    # exercise the keyboard paths too (pause, unpause, clear, both density
    # steps, and back to the birth size)
    for key in ("Key_P", "Key_P", "Key_C", "Key_Plus", "Key_Minus", "Key_0"):
        w.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                                        getattr(QtCore.Qt.Key, key),
                                        QtCore.Qt.KeyboardModifier.NoModifier))
    w.ingest(parsed[0])
    w.tick()
    app.processEvents()
    w.grab()

    # ── the tiling-WM shapes, painted for real ────────────────────────────
    # The density now comes from the WINDOW, so the shapes a tiling WM hands
    # out are a real code path and not a hypothetical: a font that never
    # re-measures, a gutter that no longer fits or a layout that clips is a
    # paint bug the old fixed-size selftest could not have seen. Base, large,
    # tall/narrow, short/wide -- and the type has to actually follow, which is
    # what the density comparison at the bottom checks.
    shapes: dict[str, float] = {}
    shapes_ok = True
    for sw, sh in ((900, 560), (1600, 1000), (600, 900), (1400, 420)):
        w.resize(sw, sh)
        app.processEvents()
        w.tick()
        # Re-feed before every grab: the decay is seconds-long and the loop
        # above already spent most of them, so a picture taken now would show
        # the resting state rather than the lens working.
        for ev in parsed[:24]:
            w.ingest(ev)
        app.processEvents()
        img = w.grab().toImage()
        if shot_dir and (sw, sh) == (1600, 1000):
            img.save(os.path.join(shot_dir, "hero-%s.png" % w.theme.name))
            # The brand layer is OFF by default (it is atmosphere, and the
            # default lens must be pure measurement), so a screenshot set that
            # never turns it on would fail to document it. Enable once here;
            # every theme grab after this carries the watermark too.
            if not w.wave:
                w.toggle_wave()
            if not w.word:
                w.toggle_word()
            app.processEvents()
            for ev in parsed[:24]:
                w.ingest(ev)
            w.tick()
            app.processEvents()
            w.grab().toImage().save(
                os.path.join(shot_dir, "hero-branded-%s.png" % w.theme.name))
        shapes["%dx%d" % (sw, sh)] = round(w.dens, 2)
        if img.width() != sw or img.height() != sh:
            shapes_ok = False
        # The column digits live in their own band and the ribbon in the
        # footer; both have to still be inside the window at every shape.
        if not (0 < w.col_y < w.foot_y < sh and w.mx < w.gut_x <= sw):
            shapes_ok = False
    shapes_ok = shapes_ok and shapes["1600x1000"] > shapes["600x900"]
    w.reset_size()
    app.processEvents()
    shapes_ok = shapes_ok and (w.width(), w.height()) == (w._ref_w, w._ref_h)

    # Every theme, painted for real. `_build_palette` is re-entrant by
    # construction and the scanline pixmap is invalidated with it -- both are
    # the kind of claim that is true until it is not, and a grab per theme is
    # the cheapest possible proof that no palette has a colour key the paint
    # loop reads and the theme forgot to define.
    theme_paints = {}
    for name in THEME_ORDER:
        w.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                                        QtCore.Qt.Key.Key_D,
                                        QtCore.Qt.KeyboardModifier.NoModifier))
        w.tick()
        for ev in parsed[:24]:
            w.ingest(ev)
        app.processEvents()
        img = w.grab().toImage()
        if shot_dir:
            img.save(os.path.join(shot_dir, "theme-%s.png" % w.theme.name))
        sx, sy = max(1, img.width() // 12), max(1, img.height() // 12)
        seen = {img.pixel(xx, yy)
                for yy in range(0, img.height(), sy)
                for xx in range(0, img.width(), sx)}
        theme_paints[w.theme.name] = len(seen)
    w.set_theme(theme, save=False)
    themes_ok = (len(theme_paints) == len(THEME_ORDER)
                 and min(theme_paints.values()) >= 8)
    painted = w.painted
    # Prove the LAYER, not just the maths: count the bars the painter actually
    # emitted on a real frame, then prove `s` gives the tap back.
    _pm = QtGui.QPixmap(w.width(), w.height())
    _pm.fill(w.bg)
    _pp = QtGui.QPainter(_pm)
    try:
        bars = w._paint_spectrum(_pp, w.width(), w.height())
    finally:
        _pp.end()
    spec_frames = spec_src.frames
    w.set_spectro(False)
    released = w.spec_src is None and not w.spectro
    w.close()

    ok = (grabs >= 30 and len(colours) >= 8 and painted >= grabs
          and spec_ok and bars >= SPEC_BANDS // 2 and spec_frames >= 20
          and released and themes_ok and cfg_ok and shapes_ok)
    print("midiviz selftest: theme=%s parsed=%d ingested=%d frames=%d paints=%d "
          "grabs=%d distinct_sampled_colours=%d themes[%s] shapes[%s ok=%s] "
          "config[%s] spectro[frames=%d bars=%d released=%s %s] platform=%s -> %s"
          % (theme, len(parsed), w.ingested, w.frames, painted, grabs,
             len(colours),
             " ".join("%s:%d" % kv for kv in sorted(theme_paints.items())),
             " ".join("%s:%.2f×" % kv for kv in shapes.items()), shapes_ok,
             cfg_notes, spec_frames, bars, released, spec_notes,
             os.environ.get("QT_QPA_PLATFORM", "native"), "PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="midiviz - MIDI stream as a picture")
    ap.add_argument("-p", "--port", help="source port (CLIENT:PORT), e.g. 28:0")
    ap.add_argument("-l", "--list", action="store_true", help="list ports and exit")
    # default None, not 1.0: the three sources of a scale are the flag, the
    # saved config and the built-in 1.0, in that order, and a flag whose
    # default equals the built-in cannot be told apart from an absent one --
    # which would mean the saved scale never won and the memory was decoration.
    ap.add_argument("-s", "--scale", type=float, default=None,
                    help="initial scale, 0.6-2.4 (also +/- at runtime; "
                         "remembered between runs)")
    ap.add_argument("--theme", choices=sorted(THEMES), default=None,
                    help="palette: dark (the cockpit), light (pale desktop), "
                         "sun (maximum contrast, for playing outdoors); "
                         "'d' cycles at runtime and the choice is remembered")
    ap.add_argument("--no-tray", action="store_true",
                    help="skip the system-tray presence even where one exists")
    ap.add_argument("--spectro", action="store_true",
                    help="spectrum backdrop from the default sink's monitor "
                         "(OFF by default; 's' toggles it at runtime)")
    ap.add_argument("--spectro-target",
                    help="capture this node instead of the default sink")
    ap.add_argument("--no-hl", action="store_true",
                    help="do not subscribe to the Tidal HL feed (SuperDirt "
                         "mirror on port %d; 'h' toggles it at runtime)"
                         % oscfeed.VIZ_PORT)
    ap.add_argument("--selftest", action="store_true",
                    help="render synthetic events for --seconds, then exit")
    ap.add_argument("--screenshot", metavar="DIR",
                    help="with --selftest: save rendered frames (one per "
                         "theme, plus a large hero shot) as PNGs into DIR")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--show", action="store_true",
                    help="run the selftest on the real display, not offscreen")
    a = ap.parse_args(argv)

    if a.list:
        for pt in list_ports():
            print("%7s  %s  │ %s" % (pt["addr"], pt["client"], pt["port"]))
        print("-> %s" % (resolve_watch_port()[1],))
        return 0
    if a.selftest:
        return selftest(a.seconds, a.show, a.theme or DEFAULT_THEME,
                        shot_dir=a.screenshot)

    # PLN, 2026-09-24: "do ensure never more than one instance, midiviz
    # should check unique else restart not duplicate". flock on a runtime
    # lockfile: atomic, released by the kernel on death, no stale-pid
    # guessing. A duplicate exits 78 — the unit's restart-prevent code —
    # because Restart=always plus exit 0 would be a restart storm against
    # the live instance.
    rt = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    lock_path = Path(rt) / CONFIG_DIR / "midiviz.instance"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("midiviz: already running — refusing to duplicate "
                  "(focus the existing window)", file=sys.stderr)
            try:
                import launchers
                launchers.focus_window("midiviz")
            except Exception:
                pass
            return USER_CLOSE_EXIT
        fh.write(str(os.getpid()))
        fh.flush()
        globals()["_INSTANCE_LOCK"] = fh        # alive for the process lifetime
    except OSError:
        pass                                    # no runtime dir: degrade, don't die

    _install_signals()
    # cli beats the saved choice beats the built-in. A bad config file loses
    # here silently and on purpose: see load_config.
    cfg = load_config()
    theme = a.theme or cfg["theme"] or DEFAULT_THEME
    scale = a.scale if a.scale is not None else cfg["scale"]
    scale = 1.0 if scale is None else max(0.6, min(2.4, scale))
    pinned = a.port                      # explicit -p pin, or None for auto-resolve
    port, label = pinned, ""
    if port is None:
        port, label = resolve_watch_port()
    # The window itself stays wordless; where it is listening goes to stderr, so
    # `-l`-free debugging is still possible without putting prose on the canvas.
    # The theme joins it for the same reason: a `--theme` that lost to a saved
    # one, or a saved one that lost to a corrupt file, is otherwise a silent
    # surprise, and the window has no words to explain itself with.
    print("⚓ midiviz — %s · theme %s · %.2f× · q/Esc/Ctrl-C to quit · right-click for the menu"
          % (label or "port %s" % port, theme, scale), file=sys.stderr)
    # A blind `aseqdump` (no -p) subscribes to NOTHING -- see the
    # WATCH_PREFERENCE comment above -- so start a Reader only once a real
    # port resolved. Otherwise the window opens idle and the RECONNECT_MS
    # rebind tick picks up the surface the moment it (or its driver) appears.
    reader = Reader(port).start() if port else None
    # The HL listener: on unless told off. A datagram aimed at a dead port
    # vanishes, so Tidal never cares whether anyone is listening.
    hl = None if a.no_hl else oscfeed.HLFeed().start()

    _QtCore, _QtGui, QtWidgets = _qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("midiviz")
    # Wayland app_id comes from desktopFileName, NOT applicationName. Without
    # this, KWin saw resourceClass "python3" (verified 2026-09-05 by querying
    # workspace.windowList()) -- so launchers.focus_window("midimon") could
    # never match, and a KWin rule keyed on the app id would either miss or,
    # worse, match EVERY python3 GUI on the box. Give the window its own
    # identity so the compositor can be told about this window and no other.
    app.setDesktopFileName("midiviz")
    # quitOnLastWindowClosed OFF is what makes "hide to the tray" survivable
    # -- otherwise Qt ends the program the moment the window goes away, and a
    # dismissed lens could never be recalled. closeEvent quits explicitly.
    app.setQuitOnLastWindowClosed(False)
    w = build_widget(port or "--", reader, scale=scale,
                     pinned_port=pinned, watch=True, theme=theme,
                     persist=True, tray=not a.no_tray, hl=hl)
    if a.spectro:
        src = None
        if a.spectro_target:
            src = SpectrumSource(target=a.spectro_target).start()
        w.set_spectro(True, src)
    w.show()
    w.raise_()
    w.activateWindow()
    rc = app.exec()
    if w.reader is not None:
        w.reader.close()
    if getattr(w, "hl", None) is not None:
        w.hl.close()
        w.hl = None
    if getattr(w, "spec_src", None) is not None:
        w.spec_src.close()
    # A deliberate close reports itself, so the unit does not undo it. Only
    # when the exit was otherwise clean: a real failure keeps its own code and
    # stays restartable.
    if getattr(w, "_user_closed", False) and rc == 0:
        return USER_CLOSE_EXIT
    return rc


if __name__ == "__main__":
    sys.exit(main())
