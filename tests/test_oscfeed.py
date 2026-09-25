"""Tests for the Tidal HL feed: OSC decode + which cells an orbit relates to.
# SPDX-License-Identifier: GPL-3.0-or-later

No Qt, no ALSA, no network: `decode_osc` is a pure function tested against
hand-built bytes — the exact packets Tidal's Stream sends (a `#bundle`
containing a `/play` message whose arguments are name/value runs) and a couple
of shapes it must survive (bare message, garbage, empty datagram).

The cell relation is tested against `lcxl_grid` itself, so the test dies the
day the grid changes shape instead of lying about it.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import oscfeed                   # noqa: E402
import sys as _sys               # noqa: E402
_tools = str(Path(__file__).resolve().parent.parent.parent)
if _tools not in _sys.path:
    _sys.path.insert(0, _tools)
import lcxl_grid as grid         # noqa: E402  lives in tools/, like midiviz's own import


def _osc_msg(addr: str, pairs: list[tuple[str, int]]) -> bytes:
    """Address + typetags + (string, int) pair args, padded the way OSC does.
    A pair is: name padded to 4 (so 'orbit' → 8 bytes) + a 4-byte int."""
    msg = addr.encode() + b"\0"
    msg += b"\0" * ((4 - len(msg) % 4) % 4)
    tags = "," + "si" * len(pairs)           # (string, int) runs — /play's shape
    t = tags.encode() + b"\0"
    msg += t + b"\0" * ((4 - len(t) % 4) % 4)
    for name, value in pairs:
        n = name.encode() + b"\0"
        n += b"\0" * ((4 - len(n) % 4) % 4)
        msg += n + struct.pack(">i", value)
    return msg


def _bundle(*elements: bytes) -> bytes:
    out = b"#bundle\0" + b"\0" * 8           # timetag zero
    for el in elements:
        out += struct.pack(">i", len(el)) + el
        out += b"\0" * ((4 - len(out) % 4) % 4)
    return out


def _named_pair(name: str, value: int) -> bytes:
    n = name.encode() + b"\0"
    n += b"\0" * ((4 - len(n) % 4) % 4)
    return n + struct.pack(">i", value)


def test_decode_play_bundle_finds_the_orbit():
    # d4 → Tidal says orbit: 3; midiviz speaks d-numbers.
    data = _bundle(_osc_msg("/play", [("orbit", 3)]))
    msgs = oscfeed.decode_osc(data)
    assert len(msgs) == 1
    assert msgs[0]["addr"] == "/play"
    assert oscfeed.play_orbit(msgs[0]) == 4


def test_decode_bare_message_and_named_args():
    data = _osc_msg("/play", [("orbit", 0), ("gain", 1)])
    msgs = oscfeed.decode_osc(data)
    assert oscfeed.play_orbit(msgs[0]) == 1
    assert msgs[0]["args"]["gain"] == 1


def test_non_play_and_garbage_yield_nothing():
    assert oscfeed.play_orbit({"addr": "/hush", "args": {}}) is None
    assert oscfeed.decode_osc(b"") == []
    assert oscfeed.decode_osc(b"\x01\x02\x03") == []    # must not raise


def test_orbit_bounds():
    # orbit 14 (0-based 13) is the rig's last; anything past is not ours.
    data = _bundle(_osc_msg("/play", [("orbit", 13)]))
    assert oscfeed.play_orbit(oscfeed.decode_osc(data)[0]) == 14
    data = _bundle(_osc_msg("/play", [("orbit", 99)]))
    assert oscfeed.play_orbit(oscfeed.decode_osc(data)[0]) is None


# ── the relation: which cells does a trigger light ─────────────────────────
def _hl_cells(orbit):
    """The widget's relation, re-derived the same way — tested against the
    grid's own tables rather than against a copy of the widget's cache."""
    ccs = set(grid.orbit_home(orbit).values())
    for role, fam_of in (("family_filter", grid.filter_family),
                         ("family_mute", grid.mute_family)):
        n = fam_of(orbit)
        ccs |= {cc for cc, (r, who) in grid.CC_ROLE.items()
                if r == role and who == n}
    return ccs


def test_d1_trigger_lights_its_column_and_both_families():
    # d1 (the kick): its own fx/level/gate cells, plus C1 (all-percs filter)
    # and F1 (kick-alone mute) — the two family controls that act on it.
    cells = {_: grid.CC_TO_CELL.get(_) for _ in _hl_cells(1)}
    assert cells[grid.CELL_TO_CC[("C", 1)]] == ("C", 1)   # gF1, the perc filter
    assert cells[grid.CELL_TO_CC[("F", 1)]] == ("F", 1)   # gM1, the kick mute
    for row, col in (("B", 1), ("D", 1), ("E", 1)):       # its own column
        assert (row, col) in cells.values()


def test_every_d_orbit_maps_to_at_least_one_control():
    # An orbit with NO related cells would make the HL feed a no-op that
    # still LOOKS wired — the worst failure shape. d1-d12 own a full column
    # plus two family controls; d13/d14 (the 14-orbit boot's tail) own only
    # their family bloc, which is still one honest thing to show.
    for o in range(1, 15):
        assert len(_hl_cells(o)) >= 1, f"d{o} has no related controls"
    for o in range(1, 13):
        assert len(_hl_cells(o)) >= 3, f"d{o} has too few related controls"


def test_widget_relation_matches_grid_derivation():
    """The widget caches `_hl_cells` — the cache must never drift from a fresh
    derivation, which is the same two-copies-one-truth lesson the port
    preference already taught."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import midiviz as V
    # The method hangs off the widget class; derive it without a Qt app.
    cls = None
    for obj in vars(V).values():
        if isinstance(obj, type) and hasattr(obj, "_hl_cells"):
            cls = obj
            break
    if cls is None:
        return
    holder = object.__new__(cls)
    holder._hl_cells_cache = {}
    for o in (1, 4, 7, 11):
        assert set(holder._hl_cells(o)) == _hl_cells(o)
