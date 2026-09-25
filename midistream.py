"""midistream — fan live, parsed MIDI events out to web subscribers (SSE).
# SPDX-License-Identifier: GPL-3.0-or-later

One `aseqdump` subprocess feeds N dashboard tabs. Reuses `midimon.parse_line` +
`enrich` (one parsing source of truth). The reader runs only while someone is
watching: started on first subscribe, stopped when the last subscriber leaves —
so we don't hold a MIDI port open for nothing.

`parse_ports` is pure (tested without ALSA).
"""
from __future__ import annotations

import queue
import re
import subprocess
import threading

import midimon

_PORT = re.compile(r"^\s*(\d+:\d+)\s+(.+?)\s{2,}(.+?)\s*$")

# Which events are STATE and which are EVENTS. The distinction is the whole of
# `coalesce`: a controller's older value is worthless the moment a newer one exists, but
# a note you drop never happened. Getting this backwards would make the monitor lie
# about what was played, which is worse than making it slow.
_CONTINUOUS = ("control change", "pitchbend", "pitch bend", "aftertouch",
               "channel aftertouch", "poly aftertouch", "control")


def _coalesce_key(ev: dict):
    """A key for events that supersede each other, or None if the event is discrete."""
    e = (ev.get("event") or "").lower()
    if not e.startswith(_CONTINUOUS):
        return None
    if e.startswith(("control change", "control")):
        return "cc", ev.get("source"), ev.get("ch"), ev.get("controller")
    return e.split()[0], ev.get("source"), ev.get("ch")


def coalesce(events: list[dict]) -> list[dict]:
    """Fold a batch: every discrete event survives, continuous ones keep only the last.

    Order is preserved by overwriting in place at the first occurrence, so the monitor
    still reads chronologically -- a superseded fader value is replaced where it stood,
    not moved to the end.
    """
    out: list[dict] = []
    at: dict = {}
    for ev in events:
        k = _coalesce_key(ev)
        if k is None:
            out.append(ev)
        elif k in at:
            out[at[k]] = ev
        else:
            at[k] = len(out)
            out.append(ev)
    return out


def parse_ports(text: str) -> list[dict]:
    """Parse `aseqdump -l` output into [{addr, client, port}] (header skipped)."""
    out = []
    for line in text.splitlines():
        m = _PORT.match(line)
        if m and ":" in m.group(1):           # header "Port  Client name…" has no digits:digits
            out.append({"addr": m.group(1), "client": m.group(2).strip(),
                        "port": m.group(3).strip()})
    return out


class MidiStream:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()
        self._proc = None
        self._port = None

    def list_ports(self) -> list[dict]:
        try:
            r = subprocess.run(["aseqdump", "-l"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        return parse_ports(r.stdout)

    # A live monitor wants the PRESENT, not a complete history. 512 was chosen as
    # "generous"; on a real surface it is a LATENCY BUDGET. PLN, 2026-08-21: "when i
    # move faders up/down quickly, lags by 500ms+, almost 1s, behind ahah" — a fader
    # sweep is ~400 CC/s, so a 512-deep queue is 1.3 s of backlog, and once it fills
    # it STAYS full: drop-oldest keeps the buffer at capacity, so every event the
    # browser renders is 512 events stale, forever. Deep buffers do not smooth a
    # stream the consumer cannot keep up with; they just add a fixed delay to it.
    #
    # 64 bounds the backlog to ~160 ms at that rate, which is jitter absorption
    # rather than a queue. The browser coalesces consecutive same-control events, so
    # it now drains far faster than it fills and this should rarely be reached at all.
    DEPTH = 64

    def subscribe(self, port=None) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.DEPTH)
        with self._lock:
            self._subs.add(q)
            restart = self._proc is None or (port and port != self._port)
        if restart:
            self._start(port)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subs.discard(q)
            stop = not self._subs
        if stop:
            self._stop()

    # ── internals ───────────────────────────────────────────────────────
    def _start(self, port):
        self._stop()
        cmd = ["aseqdump"] + (["-p", port] if port else [])
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
        except OSError:
            self._proc = None
            return
        self._port = port
        threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout or []:
            ev = midimon.parse_line(line)
            if not ev:
                continue
            self._fanout(midimon.enrich(ev))

    def _fanout(self, ev: dict) -> None:
        """Hand one event to every subscriber. Its own method so the drop policy is
        testable without ALSA -- green tests on a parser prove nothing about the seam."""
        with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    # Drop the OLDEST, not the newest. The original `except Full: pass`
                    # kept 512 stale events and threw away the one that had just
                    # happened, so a tab that stalled once showed frozen values forever
                    # with no error anywhere. For MIDI state the newest event IS truth.
                    try:
                        q.get_nowait()
                        q.put_nowait(ev)
                    except (queue.Empty, queue.Full):
                        pass

    def _stop(self):
        if self._proc:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self._proc = None
        self._port = None
