#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""lcxl_grid — THE grid. Authored once here; everything else derives or is generated.

WHY THIS FILE EXISTS
--------------------
PLN, 2026-07-29, loading bombe_dj after the remap: "i see the hud shows d9 on A8
so didnt we migrate parvagues HUD to new convention? why 2 sources of truth tbh"

It was three. The mapping from a MIDI CC to a physical control, and from a
physical control to the orbit that owns it, was hardcoded independently in:

    tools/surface-columns.py                 GRID: cc -> (column, row)
    tools/migrate-columns.py                 KNOB_A/B/C, BUTTONS, label(), slots
    tools/lcxl-leds.py                       ROW_BASE + per-orbit binding logic
    tools/pvlint/rules.py                    PV008's BT_CCS / BL_CCS / FAMILY_CCS
    <hud>/lib/lcxl-map.js + lib/render.js    TABLE + ORBIT_CONVENTION

After the 2026-07-29 remap the Python copies moved and the HUD's did not, so the
topbar showed d9 on A8 while the file and the LED board both said A1 — the
display contradicted the hardware under his hands, mid-test. That is the worst
failure mode for a glance-instrument, and it was structurally guaranteed to
happen again on the next remap.

So: this module is the only place the grid is WRITTEN. Python consumers import
it. The HUD (JavaScript, separate repo) consumes a GENERATED artifact, and a test
regenerates and compares, so editing one copy fails the suite instead of shipping.

Pattern borrowed from the fleet colour language (armada/tide-table/models.py ->
gen_tokens -> tokens.css/json): author the ontology once in Python, generate for
every other language.

THE GRID — one sentence: COLUMN N IS ORBIT N, all the way down.

      col        1       2       3       4       5       6       7       8
  A 13-20    d9 lvl  d10 lvl d11 lvl d12 lvl d9 fx   d10 fx  d11 fx  d12 fx
  B 29-36    d1 fx   d2 fx   d3 fx   d4 fx   d5 fx   d6 fx   d7 fx   d8 fx
  C 49-56    gF1     gF2     gF3     d4 fx2  d5 fx2  d6 fx2  d7 fx2  d8 fx2
  D 77-84    d1 lvl  d2 lvl  d3 lvl  d4 lvl  d5 lvl  d6 lvl  d7 lvl  d8 lvl
  E 41-44    d1 gate d2 gate d3 gate d4 gate
    57-60    d5 gate d6 gate d7 gate d8 gate
  F 73-76    gMute1  gMute2  gMute3  d4 gate2
    89-92    d5 gate2 d6 gate2 d7 gate2 d8 gate2

Rows E and F are NON-CONTIGUOUS (41-44 then 57-60): the two halves are eight
columns of one row on the hardware, and treating them as two rows is what put
d6's second button in column 5 in the old map.

C1-C3 and F1-F3 are the FAMILY controls, not per-orbit. That is measured, not
assumed — across all 13 setlist tracks:

    gF1 -> d1x13 d2x13 d3x13 d8x12    drums / core rhythm
    gF2 -> d4x14                      bass, almost exclusively
    gF3 -> d5x12 d7x9 + d9-d12        leads / melodic / extras

Which is why d1-d3 own exactly ONE knob and ONE button each, and why an orbit
with two button gestures (bombe_dj's 3-state kick) is over budget BY DESIGN.

ROW NAME ALIASES exist because the three consumers each invented their own:
`D`/`fader`, `E`/`btn1`/`BT`, `F`/`btn2`/`BL`. Rather than force a rename across
five files and a live muscle-memory, every row carries all its names.

USAGE
    python3 tools/lcxl_grid.py --check      # self-consistency, prints the grid
    python3 tools/lcxl_grid.py --generate   # write the JSON + the HUD's JS
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
JSON_OUT = ROOT / "tools" / "lcxl_grid.json"
HUD_OUT = ROOT.parent.parent / "Tools" / "pulsar-parvagues-hud" / "lib" / "lcxl-grid.generated.js"

# --------------------------------------------------------------------------
# THE AUTHORED FACTS. Everything below this block is derived.
# --------------------------------------------------------------------------

# row id -> (aliases, [(column, cc), ...])
# Segments are listed so a non-contiguous row stays ONE row of eight columns.
_ROWS: dict[str, tuple[tuple[str, ...], list[tuple[int, int]]]] = {
    "A": (("A", "knobA", "top"),    [(n, 12 + n) for n in range(1, 9)]),
    "B": (("B", "knobB", "mid"),    [(n, 28 + n) for n in range(1, 9)]),
    "C": (("C", "knobC", "bot"),    [(n, 48 + n) for n in range(1, 9)]),
    "D": (("D", "fader"),           [(n, 76 + n) for n in range(1, 9)]),
    "E": (("E", "btn1", "BT"),      [(n, 40 + n) for n in range(1, 5)]
                                  + [(n, 52 + n) for n in range(5, 9)]),
    "F": (("F", "btn2", "BL"),      [(n, 72 + n) for n in range(1, 5)]
                                  + [(n, 84 + n) for n in range(5, 9)]),
}

# Physical row order as PLN corrected it: the faders sit BETWEEN the knobs and
# the buttons. Any UI that lays the surface out vertically must use this order.
PHYSICAL_ORDER = ("A", "B", "C", "D", "E", "F")

# What each (row, column) is FOR. `orbit` means "column N belongs to orbit N";
# a literal int pins it. Roles:
#   level  fader/knob setting an orbit's volume      (Ardour-owned, see below)
#   fx     an orbit's primary effect knob
#   fx2    an orbit's second effect knob
#   gate   an orbit's primary on/off button
#   gate2  an orbit's second button
#   family_filter / family_mute   shared across a stem family, NOT per-orbit
_ROLES: dict[str, list[tuple[int, str, object]]] = {
    # d9-d12 live on row A: levels in columns 1-4, effects in columns 5-8.
    "A": [(n, "level", 8 + n) for n in range(1, 5)]
       + [(n, "fx",    8 + (n - 4)) for n in range(5, 9)],
    "B": [(n, "fx", "orbit") for n in range(1, 9)],
    "C": [(1, "family_filter", 1), (2, "family_filter", 2), (3, "family_filter", 3)]
       + [(n, "fx2", "orbit") for n in range(4, 9)],
    "D": [(n, "level", "orbit") for n in range(1, 9)],
    "E": [(n, "gate", "orbit") for n in range(1, 9)],
    "F": [(1, "family_mute", 1), (2, "family_mute", 2), (3, "family_mute", 3)]
       + [(n, "gate2", "orbit") for n in range(4, 9)],
}

# Ardour MIDI-learned these to the Tidal 01-12 strip gains. Writing or seeding
# them from Tidal is the CC77-goes-to-silence footgun; a track referencing one is
# a CONFLICT, not a column question.
ARDOUR_ROWS = ("D",)                  # all 8 faders
ARDOUR_EXTRA = tuple(12 + n for n in range(1, 5))   # A1-A4 = d9-d12 levels

# gPanic is a global kill; never sweep or seed it.
PANIC_CC = 93

# --------------------------------------------------------------------------
# Which FAMILY an orbit belongs to. Filters and mutes group DIFFERENTLY —
# that asymmetry is deliberate, authored by PLN 2026-07-30 settling #105:
#
#   "I want the kick, now on d1/fader1, to have its mute on F1 botrow button.
#    I want the C1 knob to djf all percs, C2 only bass djf, C3 melodies DJF.
#    however i want the mutes consistent across all all tracks:
#    m1 d1 / m2 other percs / m3 all bass+melodics"
#
# So the DJ filters treat the whole rhythm section as one bloc (you sweep the
# drums together, which is the gesture), while the mutes split the kick out on
# its own button (you drop the kick alone, which is the other gesture). Both
# maps are three-wide because columns 4-8 of rows C and F are already spent as
# d4-d8's fx2/gate2 slots — see _ROLES above. There is no fourth family.
#
# Note the kick ALSO has its own per-orbit gate at E1 (^41), freed when gMask
# was retired. "Mute the kick alone" is served twice over: F1 as the family
# mute, E1 as its own gate.
_FILTER_FAMILY = {1: 1, 2: 1, 3: 1, 8: 1,          # all percs -> C1
                  4: 2,                             # bass only -> C2
                  5: 3, 6: 3, 7: 3, 9: 3, 10: 3, 11: 3, 12: 3}   # melodies -> C3
_MUTE_FAMILY   = {1: 1,                             # the kick, alone -> F1
                  2: 2, 3: 2, 8: 2,                 # other percs -> F2
                  4: 3, 5: 3, 6: 3, 7: 3, 9: 3, 10: 3, 11: 3, 12: 3}  # bass+mels -> F3

# Orbits past 12 (d13/d14 exist on the 14-orbit boot) are melodic/FX by
# default. A fallback, not a claim — if one of them ever becomes a drum,
# author it above rather than letting the default decide.
_FAMILY_FALLBACK = 3


def filter_family(orbit: int) -> int:
    """Which gF<N> / row-C knob this orbit's DJ filter belongs to."""
    return _FILTER_FAMILY.get(orbit, _FAMILY_FALLBACK)


def mute_family(orbit: int) -> int:
    """Which gM<N> / row-F button this orbit's mute belongs to."""
    return _MUTE_FAMILY.get(orbit, _FAMILY_FALLBACK)

# --------------------------------------------------------------------------
# Derived views. Import these; do not re-derive them in a consumer.
# --------------------------------------------------------------------------

CC_TO_CELL: dict[int, tuple[str, int]] = {}      # cc -> (row, column)
CELL_TO_CC: dict[tuple[str, int], int] = {}      # (row, column) -> cc
ROW_CCS: dict[str, list[int]] = {}               # row -> ccs in column order
ALIAS_TO_ROW: dict[str, str] = {}
CC_ROLE: dict[int, tuple[str, int]] = {}         # cc -> (role, orbit-or-family-n)

for _row, (_aliases, _cells) in _ROWS.items():
    for _alias in _aliases:
        ALIAS_TO_ROW[_alias] = _row
    ROW_CCS[_row] = [cc for _c, cc in sorted(_cells)]
    for _col, _cc in _cells:
        CC_TO_CELL[_cc] = (_row, _col)
        CELL_TO_CC[(_row, _col)] = _cc
for _row, _specs in _ROLES.items():
    for _col, _role, _who in _specs:
        _cc = CELL_TO_CC[(_row, _col)]
        CC_ROLE[_cc] = (_role, _col if _who == "orbit" else int(_who))

ARDOUR_CCS: set[int] = {cc for r in ARDOUR_ROWS for cc in ROW_CCS[r]} | set(ARDOUR_EXTRA)
KNOB_CCS: set[int] = set(ROW_CCS["A"]) | set(ROW_CCS["B"]) | set(ROW_CCS["C"])
BUTTON_CCS: set[int] = set(ROW_CCS["E"]) | set(ROW_CCS["F"])

# ── the v3 DAW-mode surface, positionally ──────────────────────────────────
# The LCXL3 in DAW mode speaks its OWN fixed CC numbering, which is not the
# corpus's. `lcxl3-driver.py` translates it on the way through and publishes the
# result as the virtual port 'ParVagues LCXL3'. The table lived only inside the
# driver, so anything ELSE reading the board direct saw v3 numbers land in v2 cells:
# midiviz drew row C's knobs into row B, row D into nothing at all, and E5-E8
# into E1-E4 -- a coherent, confident, wrong picture, for as long as the driver
# was down (2026-09-22; the driver had in fact failed 1077 times).
#
# So the table belongs to the GRID, not to one of its readers. Verified against
# the hardware by moving one control per row and reading what arrived
# (fader1->cc5 ch16, A1->13 ch16, B1->21 ch16, C1->29 ch16, E1->37 ch1,
# F1->45 ch1).
V3_ROWS: dict[str, list[int]] = {
    "A": list(range(13, 21)),
    "B": list(range(21, 29)),
    "C": list(range(29, 37)),
    "D": list(range(5, 13)),
    "E": list(range(37, 45)),
    "F": list(range(45, 53)),
}


def build_v3_map() -> dict[int, int]:
    """v3 DAW CC -> v2 corpus CC, positionally, straight out of the grid.

    Raises rather than guesses: a row whose hardware width disagrees with the
    authored width is exactly what must not be papered over, because the result
    would be a silently shifted row rather than an error.
    """
    out: dict[int, int] = {}
    for row, v3ccs in V3_ROWS.items():
        v2ccs = ROW_CCS.get(row)
        if not v2ccs:
            raise KeyError(f"lcxl_grid has no row {row!r}")
        if len(v2ccs) != len(v3ccs):
            raise ValueError(
                f"row {row} has {len(v2ccs)} v2 cells but {len(v3ccs)} v3 "
                "indices -- the grid and the hardware disagree"
            )
        out.update(zip(v3ccs, v2ccs))
    return out


V3_TO_V2: dict[int, int] = build_v3_map()
FAMILY_CCS: set[int] = {cc for cc, (role, _n) in CC_ROLE.items()
                        if role.startswith("family_")} | {PANIC_CC}


def label(cc: int) -> str:
    """44 -> 'E4'. The canonical short name of a physical control."""
    cell = CC_TO_CELL.get(int(cc))
    return f"{cell[0]}{cell[1]}" if cell else f"cc{cc}"


def label_as(cc: int, style: str) -> str:
    """Same, in a consumer's own row vocabulary — e.g. label_as(44,'BT') -> 'BT4'."""
    cell = CC_TO_CELL.get(int(cc))
    if not cell:
        return f"cc{cc}"
    row, col = cell
    for alias, target in ALIAS_TO_ROW.items():
        if target == row and alias.startswith(style):
            return f"{alias}{col}"
    return f"{row}{col}"


def slots(orbit: int, kind: str) -> list[int]:
    """The CCs `orbit` owns of one kind, most-reachable first.

    kind='knob' -> its fx then fx2;  kind='button' -> its gate then gate2.
    Returns ONE entry for d1-d3, because C1-3 and F1-3 are the family controls.
    Derived from CC_ROLE, so it cannot drift from the table above — the previous
    version hardcoded `28 + orbit` / `48 + orbit` in migrate-columns.py.
    """
    want = ("fx", "fx2") if kind == "knob" else ("gate", "gate2")
    out = []
    for role in want:
        for cc, (r, who) in CC_ROLE.items():
            if r == role and who == orbit and cc not in ARDOUR_CCS:
                out.append(cc)
    return out


def orbit_home(orbit: int) -> dict[str, int]:
    """Where an orbit lives: {'level': cc, 'fx': cc, ...}. Empty roles omitted."""
    out: dict[str, int] = {}
    for cc, (role, who) in CC_ROLE.items():
        if who == orbit and not role.startswith("family_"):
            out.setdefault(role, cc)
    return out


def as_dict() -> dict:
    """The whole grid, JSON-ready. This is what non-Python consumers get."""
    return {
        "_generated_by": "tools/lcxl_grid.py — do not edit; run --generate",
        "physical_order": list(PHYSICAL_ORDER),
        "rows": {
            row: {
                "aliases": list(_ROWS[row][0]),
                "columns": {str(col): CELL_TO_CC[(row, col)]
                            for _c, _cc in _ROWS[row][1]
                            for col in [_c]},
            }
            for row in PHYSICAL_ORDER
        },
        "cc": {
            str(cc): {
                "row": CC_TO_CELL[cc][0],
                "column": CC_TO_CELL[cc][1],
                "label": label(cc),
                "role": CC_ROLE.get(cc, ("unassigned", 0))[0],
                "owner": CC_ROLE.get(cc, ("unassigned", 0))[1],
                "ardour_owned": cc in ARDOUR_CCS,
            }
            for cc in sorted(CC_TO_CELL)
        },
        "orbit_home": {str(o): orbit_home(o) for o in range(1, 13)},
        # Filters and mutes group differently on purpose — see _FILTER_FAMILY.
        # The HUD needs both to say which knob and which button own an orbit.
        "orbit_family": {str(o): {"filter": filter_family(o), "mute": mute_family(o)}
                         for o in range(1, 13)},
        "ardour_ccs": sorted(ARDOUR_CCS),
        "family_ccs": sorted(FAMILY_CCS),
        "panic_cc": PANIC_CC,
    }


# --------------------------------------------------------------------------
# Self-check. These are invariants of the SURFACE, so a violation means the
# authored table above is wrong — not that a consumer is out of date.
# --------------------------------------------------------------------------

def problems() -> list[str]:
    out = []
    if len(CC_TO_CELL) != 48:
        out.append(f"expected 48 controls (6 rows x 8), got {len(CC_TO_CELL)}")
    if len(CELL_TO_CC) != len(CC_TO_CELL):
        out.append("a CC appears in two cells, or two CCs share one cell")
    for row in PHYSICAL_ORDER:
        cols = sorted(c for r, c in CC_TO_CELL.values() if r == row)
        if cols != list(range(1, 9)):
            out.append(f"row {row} does not cover columns 1-8: {cols}")
    missing = sorted(set(CC_TO_CELL) - set(CC_ROLE))
    if missing:
        out.append(f"controls with no role: {[label(c) for c in missing]}")
    # Every orbit must have somewhere to live, or the migrator has nowhere to aim.
    for o in range(1, 9):
        home = orbit_home(o)
        if "level" not in home or "fx" not in home or "gate" not in home:
            out.append(f"d{o} is missing a level/fx/gate slot: {home}")
    for o in range(9, 13):
        if "level" not in orbit_home(o) or "fx" not in orbit_home(o):
            out.append(f"d{o} is missing a level/fx slot")
    # d1-d3 must have exactly one of each, d4-d8 two — the family-control budget.
    for o in (1, 2, 3):
        if len(slots(o, "knob")) != 1 or len(slots(o, "button")) != 1:
            out.append(f"d{o} should own exactly one knob and one button "
                       f"(C{o}/F{o} are family controls)")
    for o in range(4, 9):
        if len(slots(o, "knob")) != 2 or len(slots(o, "button")) != 2:
            out.append(f"d{o} should own two knobs and two buttons")
    # A control Ardour learned must never be handed out as a Tidal effect slot.
    for o in range(1, 13):
        for kind in ("knob", "button"):
            for cc in slots(o, kind):
                if cc in ARDOUR_CCS:
                    out.append(f"d{o} {kind} slot {label(cc)} is Ardour-owned")
    return out


def generate() -> list[pathlib.Path]:
    payload = as_dict()
    JSON_OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    js = (
        "'use babel';\n\n"
        "// GENERATED by Sound/Tidal/tools/lcxl_grid.py — DO NOT EDIT.\n"
        "// Regenerate:  python3 tools/lcxl_grid.py --generate\n"
        "// The grid is authored once in Python because it had drifted into three\n"
        "// hardcoded copies, and the HUD's was the one that went stale: it showed\n"
        "// d9 on A8 after the remap while the LED board and the .tidal files said A1.\n\n"
        "export const GRID = " + json.dumps(payload, indent=2, ensure_ascii=False) + ";\n\n"
        "// Convenience: orbit -> its physical home, in the row/lane shape render.js uses.\n"
        "export const ORBIT_CONVENTION = Object.fromEntries(\n"
        "  Object.entries(GRID.orbit_home).map(([orbit, home]) => {\n"
        "    const cc = home.level;\n"
        "    const cell = GRID.cc[String(cc)];\n"
        "    return [Number(orbit), { row: cell.row, lane: cell.column }];\n"
        "  })\n"
        ");\n"
    )
    written = [JSON_OUT]
    if HUD_OUT.parent.is_dir():
        HUD_OUT.write_text(js)
        written.append(HUD_OUT)
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--generate", action="store_true")
    a = ap.parse_args()
    if not (a.check or a.generate):
        a.check = True

    if a.check:
        print("        col   1     2     3     4     5     6     7     8")
        for row in PHYSICAL_ORDER:
            cells = []
            for col in range(1, 9):
                cc = CELL_TO_CC[(row, col)]
                role, who = CC_ROLE[cc]
                if role.startswith("family_"):
                    tag = f"{'gF' if 'filter' in role else 'gMute'}{who}"
                else:
                    tag = f"d{who}{'' if role == 'level' else ':' + role}"
                cells.append(f"{tag:>10}")
            print(f"  {row} {' '.join(str(c) for c in ROW_CCS[row][:1]):>3}   " + "".join(cells))
        errs = problems()
        if errs:
            print("\nPROBLEMS:")
            for e in errs:
                print(f"  !! {e}")
            return 1
        print(f"\nlcxl_grid: OK — 48 controls, every row 1-8, every orbit housed.")

    if a.generate:
        for p in generate():
            print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
