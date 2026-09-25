"""Pure tests for midistream: port parsing, and the coalescer (no ALSA).
# SPDX-License-Identifier: GPL-3.0-or-later

The coalescer's one rule is the only thing standing between "the monitor is fast" and
"the monitor lies": continuous controls are STATE and may be superseded, notes are
EVENTS and may never be dropped. Get that backwards and a fast monitor silently loses
what was played, which is strictly worse than a slow one.
"""
import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import midistream as MS

SAMPLE = """ Port    Client name                      Port name
  0:0    System                           Timer
  0:1    System                           Announce
 14:0    Midi Through                     Midi Through Port-0
 28:0    nanoKONTROL2                     nanoKONTROL2 MIDI 1
"""


def test_parse_ports_skips_header_and_parses_rows():
    ports = MS.parse_ports(SAMPLE)
    assert {"addr": "28:0", "client": "nanoKONTROL2", "port": "nanoKONTROL2 MIDI 1"} in ports
    assert all(":" in p["addr"] for p in ports)
    assert not any(p["client"] == "Client name" for p in ports)  # header skipped
    assert len(ports) == 4


def test_parse_ports_empty():
    assert MS.parse_ports("") == []


# ------------------------------------------------------------------ coalesce

def cc(controller, value, ch=0, source="24:0"):
    return {"source": source, "event": "Control change", "ch": ch,
            "controller": controller, "value": value, "data": ""}


def note(n, vel=100, on=True, ch=0, source="24:0"):
    return {"source": source, "event": "Note on" if on else "Note off", "ch": ch,
            "note": n, "velocity": vel, "data": ""}


def test_a_fader_sweep_collapses_to_its_final_value():
    out = MS.coalesce([cc(77, v) for v in range(128)])
    assert len(out) == 1 and out[0]["value"] == 127


def test_every_note_survives_even_a_flood_of_controllers():
    """Notes are what was PLAYED. Losing one to a fader sweep is unforgivable."""
    batch = [note(60), *(cc(77, v) for v in range(100)), note(64), note(60, on=False)]
    out = MS.coalesce(batch)
    assert [e for e in out if "note" in e] == [note(60), note(64), note(60, on=False)]


def test_distinct_controllers_do_not_collapse_into_each_other():
    out = MS.coalesce([cc(77, 10), cc(78, 20), cc(77, 30)])
    assert len(out) == 2
    assert {e["controller"]: e["value"] for e in out} == {77: 30, 78: 20}


def test_same_controller_on_a_different_channel_or_device_is_a_different_control():
    out = MS.coalesce([cc(77, 1, ch=0), cc(77, 2, ch=1), cc(77, 3, source="28:0")])
    assert len(out) == 3


def test_chronological_order_is_preserved():
    """A superseded value is replaced WHERE IT STOOD, so the monitor still reads as a
    timeline rather than reshuffling history to the end."""
    out = MS.coalesce([cc(77, 1), note(60), cc(77, 2), note(62)])
    assert [e.get("controller", e.get("note")) for e in out] == [77, 60, 62]
    assert out[0]["value"] == 2


def test_coalesce_is_a_no_op_on_an_already_sparse_batch():
    batch = [note(60), cc(77, 5), note(60, on=False)]
    assert MS.coalesce(batch) == batch


def test_unknown_event_kinds_are_treated_as_discrete():
    """Unrecognised == keep. A coalescer that guesses wrong should lose nothing."""
    weird = {"source": "24:0", "event": "System exclusive", "ch": None, "data": "F0"}
    assert MS.coalesce([weird, weird]) == [weird, weird]


# ---------------------------------------------------- the subscriber queue

def test_a_full_queue_drops_the_OLDEST_event_not_the_newest():
    """The original `except queue.Full: pass` kept 512 stale events and threw away the
    one that had just happened, so a tab that stalled once froze forever."""
    stream = MS.MidiStream()
    q: queue.Queue = queue.Queue(maxsize=4)
    stream._subs.add(q)
    for v in range(10):
        stream._fanout(cc(77, v))
    got = [q.get_nowait()["value"] for _ in range(q.qsize())]
    assert got == [6, 7, 8, 9], f"kept {got}; the newest value must survive"
