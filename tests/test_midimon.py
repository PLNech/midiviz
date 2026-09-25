"""Pure-parser tests for midimon (no ALSA needed)."""
# SPDX-License-Identifier: GPL-3.0-or-later
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import midimon as M


def test_note_name():
    assert M.note_name(60) == "C4"
    assert M.note_name(69) == "A4"
    assert M.note_name(72) == "C5"
    assert M.note_name(61) == "C#4"


def test_parse_note_on():
    ev = M.parse_line(" 28:0   Note on                 0, note 60, velocity 100")
    assert ev["source"] == "28:0"
    assert ev["event"] == "Note on"
    assert ev["ch"] == 0
    assert "note 60" in ev["data"]


def test_parse_control_change():
    ev = M.parse_line(" 28:0   Control change          5, controller 7, value 99")
    assert ev["event"] == "Control change" and ev["ch"] == 5


def test_parse_non_event_returns_none():
    assert M.parse_line("Source  Event                  Ch  Data") is None
    assert M.parse_line("Waiting for data at port 128:0.") is None
    assert M.parse_line("") is None


def test_render_contains_event_and_notename():
    ev = M.parse_line(" 28:0   Note on                 0, note 60, velocity 100")
    out = M.render(ev)
    assert "Note on" in out and "C4" in out


def test_enrich_note_and_velocity():
    ev = M.enrich(M.parse_line(" 28:0   Note on                 0, note 60, velocity 100"))
    assert ev["note"] == 60 and ev["note_name"] == "C4" and ev["velocity"] == 100


def test_enrich_controller():
    ev = M.enrich(M.parse_line(" 28:0   Control change          0, controller 74, value 64"))
    assert ev["controller"] == 74 and ev["value"] == 64 and "note" not in ev
