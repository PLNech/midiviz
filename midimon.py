#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""midimon — a clean live MIDI monitor (aseqdump, parsed & rendered right).

`aseqdump | tr -d …` is a hack. This wraps aseqdump, parses each event, and
renders an aligned, colourised stream: note numbers → names (C4…), velocity as a
little bar, events tinted by type. Ctrl-C to stop.

    python3 midimon.py                 # monitor aseqdump's default port
    python3 midimon.py -p 28:0         # subscribe to a specific source
    python3 midimon.py -l              # list ports and exit
    python3 midimon.py --raw           # passthrough (debug)

Parsing is a pure function (`parse_line`) so it's unit-tested without ALSA.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# event keyword → ANSI colour
C = {"note on": "92", "note off": "90", "control": "93", "program": "96",
     "pitch": "95", "channel": "94", "aftertouch": "94",
     "clock": "90", "start": "92", "stop": "91", "continue": "92",
     "song": "90", "sysex": "95", "active": "90", "reset": "91"}
RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"

# " 28:0   Note on                 0, note 60, velocity 100"
_ROW = re.compile(r"^\s*(\d+:\d+)\s+(\S.*?\S|\S)\s{2,}(.*?)\s*$")


def note_name(n: int) -> str:
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def parse_line(line: str) -> dict | None:
    """aseqdump event row → {source, event, data, ch?}. None if not an event."""
    m = _ROW.match(line.rstrip("\n"))
    if not m:
        return None
    source, event, data = m.group(1), m.group(2), m.group(3)
    ch = None
    mc = re.match(r"^(\d+)\b", data)          # first int of data is the channel
    if mc:
        ch = int(mc.group(1))
    return {"source": source, "event": event, "data": data, "ch": ch}


def enrich(ev: dict) -> dict:
    """Add structured fields parsed from `data` (note/velocity/controller/…).

    One parsing source of truth — used by the CLI renderer and the web stream.
    """
    out = dict(ev)
    for key, pat in (("note", r"note (\d+)"), ("velocity", r"velocity (\d+)"),
                     ("controller", r"controller (\d+)"), ("value", r"value (-?\d+)"),
                     ("program", r"program (\d+)")):
        m = re.search(pat, ev["data"])
        if m:
            out[key] = int(m.group(1))
    if "note" in out:
        out["note_name"] = note_name(out["note"])
    return out


def _color(event: str) -> str:
    e = event.lower()
    for kw, code in C.items():
        if e.startswith(kw):
            return code
    return "97"


def _render_data(event: str, data: str) -> str:
    """Friendlier data: note names + a velocity bar where it applies."""
    note = re.search(r"note (\d+)", data)
    vel = re.search(r"velocity (\d+)", data)
    out = data
    if note:
        n = int(note.group(1))
        out = out.replace(f"note {n}", f"note {n} {DIM}({note_name(n)}){RESET}")
    if vel:
        v = int(vel.group(1))
        bars = "▁▂▃▄▅▆▇█"
        out += f"  {bars[min(v, 127) * (len(bars) - 1) // 127]}"
    return out


def render(ev: dict) -> str:
    code = _color(ev["event"])
    ch = f"ch{ev['ch']:<2}" if ev["ch"] is not None else "   "
    return (f"{DIM}{ev['source']:>6}{RESET}  "
            f"\033[{code}m{BOLD}{ev['event']:<18}{RESET} "
            f"{DIM}{ch}{RESET} {_render_data(ev['event'], ev['data'])}")


def list_ports():
    subprocess.run(["aseqdump", "-l"])


# What to watch, best first. `aseqdump` with no -p subscribes to NOTHING and
# prints "Waiting for data at port ..." — so midimon opened blind and PLN had to
# wire it to Midi Through by hand every time ("had to wire it to midi through
# myself", 2026-08-29). A monitor that does not monitor anything on open is a
# monitor you have to repair before you can use it.
#
# The ORDER encodes what is actually interesting:
#   1. the lcxl3-driver's translated stream — the numbers the CORPUS speaks, i.e.
#      what you actually want to verify when a knob does the wrong thing.
#   2. the raw v3 DAW port — the numbers the HARDWARE speaks, for when the
#      question is "is the surface even sending".
#   3. the raw v3 custom-mode port, for standalone work.
#   4. Midi Through, the old default, kept last as a catch-all.
WATCH_PREFERENCE = ("ParVagues LCXL3", "LCXL3 1 DAW", "LCXL3", "Launch Control XL",
                    "Midi Through")


def resolve_port() -> tuple[str | None, str]:
    """(port_id, human_label) of the most interesting source present.

    Matches CLIENT *and* PORT names: python-rtmidi registers the driver's client
    as 'RtMidiOut Client' and puts 'ParVagues LCXL3' on the port, so a
    client-only match misses exactly the stream we most want to watch.
    """
    try:
        out = subprocess.run(["aseqdump", "-l"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None, "aseqdump unavailable"
    # `aseqdump -l` prints: "  20:1    LCXL3 1    LCXL3 1 DAW In"
    rows = []
    for line in out.splitlines():
        tok = line.split()
        if tok and ":" in tok[0] and tok[0].split(":")[0].isdigit():
            rows.append((tok[0], line))
    for want in WATCH_PREFERENCE:
        for pid, line in rows:
            if want in line:
                return pid, f"{pid} · {want}"
    return None, "no MIDI source found"


def monitor(port: str | None, raw: bool):
    label = ""
    if not port:
        port, label = resolve_port()
    cmd = ["aseqdump"] + (["-p", port] if port else [])
    where = label or (f"port {port}" if port else "NOTHING — nothing to watch")
    print(f"{DIM}⚓ midimon — {where} · Ctrl-C to stop{RESET}", file=sys.stderr)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout or []:
            if raw:
                sys.stdout.write(line); continue
            ev = parse_line(line)
            if ev:
                print(render(ev), flush=True)
            elif line.strip() and not line.startswith(("Source", "Waiting")):
                print(f"{DIM}{line.rstrip()}{RESET}")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port",
                   help="source port (CLIENT:PORT), e.g. 28:0. Default: the most "
                        "interesting source present — the lcxl3-driver's "
                        "translated stream if it is up, else the raw surface, "
                        "else Midi Through.")
    ap.add_argument("-l", "--list", action="store_true", help="list ports and exit")
    ap.add_argument("--raw", action="store_true", help="passthrough, no parsing")
    a = ap.parse_args(argv)
    if a.list:
        return list_ports()
    monitor(a.port, a.raw)


if __name__ == "__main__":
    main()
