"""Tests for midiviz's non-drawing parts (no Qt, no ALSA needed).
# SPDX-License-Identifier: GPL-3.0-or-later

The drawing itself is verified by `python3 midiviz.py --selftest`, which renders
synthetic events through the real paint path; green tests on pure functions prove
nothing about a window. What is worth pinning here is the stuff that would fail
*silently*: the port-preference copy drifting from midimon's authored one, and a
word sneaking into the glyph set.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import midimon                    # noqa: E402
import midiviz as V               # noqa: E402


# ── the copy must never disagree with the original ─────────────────────────
def test_watch_preference_does_not_drift_from_midimon():
    """midiviz keeps a fallback copy of the preference order for trees whose
    midimon predates `resolve_port`. Two lists that are supposed to be the same
    list are exactly the kind of thing that quietly stops being the same."""
    authored = getattr(midimon, "WATCH_PREFERENCE", None)
    if authored is None:
        return                     # this tree's midimon has none; nothing to drift from
    assert tuple(V.WATCH_PREFERENCE) == tuple(authored)


def test_resolve_delegates_to_midimon_when_it_has_the_helper(monkeypatch):
    monkeypatch.setattr(midimon, "resolve_port", lambda: ("99:9", "sentinel"),
                        raising=False)
    assert V.resolve_watch_port() == ("99:9", "sentinel")


# ── the fallback resolver ──────────────────────────────────────────────────
# Real `aseqdump -l` output from this rig, with the LCXL3 and the driver both up.
_PORTS = """ Port    Client name                      Port name
  0:0    System                           Timer
 14:0    Midi Through                     Midi Through Port-0
 20:0    LCXL3 1                          LCXL3 1 MIDI In
 20:1    LCXL3 1                          LCXL3 1 DAW In
133:0    RtMidiOut Client                 ParVagues LCXL3
"""


def _fallback(monkeypatch, text):
    monkeypatch.delattr(midimon, "resolve_port", raising=False)
    monkeypatch.setattr(V.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, text, ""))
    return V.resolve_watch_port()


def test_the_driver_is_found_by_its_PORT_name_not_its_client(monkeypatch):
    """The whole point. python-rtmidi registers the lcxl3 driver's client as
    'RtMidiOut Client' and puts 'ParVagues LCXL3' on the PORT, so matching only
    the client column silently falls through to the raw hardware — the monitor
    would work, and show the wrong stream."""
    pid, label = _fallback(monkeypatch, _PORTS)
    assert pid == "133:0"
    assert "ParVagues LCXL3" in label


def test_it_falls_back_down_the_preference_order(monkeypatch):
    without_driver = "\n".join(l for l in _PORTS.splitlines() if "ParVagues" not in l)
    assert _fallback(monkeypatch, without_driver)[0] == "20:1"     # raw v3 DAW port
    only_thru = "\n".join(l for l in _PORTS.splitlines() if "LCXL3" not in l)
    assert _fallback(monkeypatch, only_thru)[0] == "14:0"          # the catch-all
    assert _fallback(monkeypatch, " Port  Client name  Port name\n")[0] is None


def test_a_header_only_listing_is_not_mistaken_for_a_port(monkeypatch):
    pid, label = _fallback(monkeypatch, "")
    assert pid is None and "no MIDI source" in label


# ── the viz is wordless, and every value lands on one scale ────────────────
def test_no_glyph_is_a_word():
    """PLN: 'no word like control change pure viz'. Every rendered glyph is one
    character, and none of them is a letter — hex digits are the only alnum."""
    glyphs = [g for _kw, g in V.CLASS_GLYPH] + [V.OTHER_GLYPH] + list(V.HEX)
    for g in glyphs:
        assert len(g) == 1, g
        assert not g.isalpha() or g in V.HEX, g


def test_note_on_and_off_are_different_glyph_classes():
    on, on_fam = V._glyph_for("Note on")
    off, off_fam = V._glyph_for("Note off")
    assert on != off and on_fam == off_fam == V.FAM_NOTE
    assert V._glyph_for("Some future event")[0] == V.OTHER_GLYPH


def test_every_event_kind_normalises_onto_0_127():
    assert V._norm_value({"value": 0}) == 0
    assert V._norm_value({"value": 127}) == 127
    assert V._norm_value({"velocity": 100}) == 100
    assert V._norm_value({"program": 7}) == 7
    assert V._norm_value({}) == 64                      # no value = mid, not 0
    # pitch bend is ±8192, and centre must land mid-scale rather than clamp to 0
    assert V._norm_value({"value": 0, "event": "Pitch bend"}) == 0
    assert 61 <= V._norm_value({"value": -1}) <= 65
    assert V._norm_value({"value": -8192}) == 0
    assert V._norm_value({"value": 8191}) == 127


def test_dataless_transport_messages_do_not_parse_at_all():
    """A real gap, pinned here rather than papered over.

    `midimon.parse_line`'s row regex is `addr  EVENT  <2+ spaces>  DATA`, so an
    aseqdump line with no data column — which is exactly how Start, Stop,
    Continue and Clock print — matches nothing and is dropped. midiviz therefore
    has glyphs (▶ ■ ▷ ·) that can never light up, and midimon's own console
    silently omits transport too. Fixing it belongs in midimon (one parser per
    concept); when that lands, this test is what tells you to delete the xfail.
    """
    assert midimon.parse_line(" 28:0   Start") is None
    assert midimon.parse_line(" 28:0   Stop") is None
    # ...while the same event WITH a data column parses fine:
    assert midimon.parse_line(" 28:0   Note on   1, note 60, velocity 99") is not None


def test_the_real_parser_feeds_the_real_ingest_path():
    """The selftest's synthetic lines must actually parse — if the fixture drifted
    out of aseqdump's shape the selftest would render an empty window and pass."""
    lines = V._synthetic_lines()
    parsed = [midimon.parse_line(l) for l in lines]
    unparsed = [l.split()[1] for l, p in zip(lines, parsed) if p is None]
    # the ONLY tolerated failures are the data-less transport lines above
    assert set(unparsed) <= {"Start", "Stop"}, unparsed
    enriched = [midimon.enrich(p) for p in parsed if p is not None]
    ccs = [e["controller"] for e in enriched if "controller" in e]
    # every one of the 48 authored grid cells is covered
    assert set(V.grid.CC_TO_CELL) <= set(ccs)
    assert any(cc not in V.grid.CC_TO_CELL for cc in ccs), "no un-gridded CC to rain"


# ── the close latch ────────────────────────────────────────────────────────
#
# The close-latch tests that assert agreement with the RIG's supervisor
# (rig_units.ensure() and launchers.py) live in the rig's own suite —
# those two files are rig-side and are not part of this project. The
# latch path itself is still exercised below via _latch_close().

# ── the two controls stay hittable ─────────────────────────────────────────
#
# Sizing a hit target to its glyph gave an 8px-wide X: pleasant to look at,
# unhittable during a set, and at 0.6 scale the pin and the X had to either
# overlap or shrink below usable. The layout now derives targets from the
# right edge and centres the glyphs inside them, so this holds by
# construction -- which is worth an assertion at the scales PLN actually
# zooms through, since a regression here is invisible until he reaches for it.

def _widget(scale, width):
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_mv_ctrl",
                                                  root / "midiviz.py")
    mv = importlib.util.module_from_spec(spec)
    _sys.modules["_mv_ctrl"] = mv
    spec.loader.exec_module(mv)
    try:
        _QtCore, _QtGui, QtWidgets = mv._qt()
    except Exception:
        return None, None
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = mv.build_widget("--", None, scale=scale)
    w.resize(width, 400)
    w.show()
    app.processEvents()
    return mv, w


def test_controls_are_hittable_and_disjoint_at_every_scale():
    import os
    import pytest
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    mv, w0 = _widget(1.0, 900)
    if mv is None:
        pytest.skip("no Qt available")
    for scale in (0.6, 1.0, 1.6, 2.4):
        for width in (520, 900, 1920):
            _, w = _widget(scale, width)
            pin, close = w._ctrl_rects()
            assert not pin.intersects(close), (
                f"pin and X overlap at scale={scale} width={width}: a click "
                f"meant for one would hit the other")
            assert close.right() <= w.width(), \
                f"the X is off-screen at scale={scale} width={width}"
            assert min(pin.width(), pin.height(),
                       close.width(), close.height()) >= w.MIN_TARGET, (
                f"a target fell below MIN_TARGET at scale={scale} "
                f"width={width}: pin={pin} close={close}")
            # The sparkline must stop before the controls, not run under them.
            assert w._ctrl_left() <= pin.left(), \
                "the sparkline is allowed to draw over the controls"
            w.close()


def test_user_close_exit_code_matches_the_unit():
    """The code's exit status and the unit's guard are one number, twice.

    Restart=always cannot distinguish an aseqdump EOF from PLN clicking the X,
    so a deliberate close is signalled by exit status and the unit exempts
    exactly that status. If these two ever drift, the X closes the window and
    systemd puts it back five seconds later — which is the complaint, reported
    twice, and it looks like the close is broken rather than the number.
    """
    import importlib.util
    import re
    import sys as _sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_mv_exit",
                                                  root / "midiviz.py")
    mv = importlib.util.module_from_spec(spec)
    _sys.modules["_mv_exit"] = mv
    spec.loader.exec_module(mv)

    unit = (root / "packaging/midiviz.service").read_text()
    m = re.search(r"^RestartPreventExitStatus=(\d+)\s*$", unit, re.M)
    assert m, ("midiviz.service has no RestartPreventExitStatus, so "
               "Restart=always will undo every deliberate close")
    assert int(m.group(1)) == mv.USER_CLOSE_EXIT, (
        f"the unit exempts exit {m.group(1)} but the code exits "
        f"{mv.USER_CLOSE_EXIT} on a user close")
    # Must not collide with the codes a normal run or a signal produces.
    assert mv.USER_CLOSE_EXIT not in (0, 1), "collides with a normal exit"
    assert mv.USER_CLOSE_EXIT < 128, "collides with the 128+N signal range"

    # Preventing the restart is only half of it: without SuccessExitStatus,
    # systemd files a deliberate close as `failed (result: exit-code)` and the
    # unit shows red everywhere the rig is monitored, which sends the next
    # person to debug a unit that is behaving perfectly.
    ok = re.search(r"^SuccessExitStatus=(.+)$", unit, re.M)
    assert ok, ("midiviz.service has no SuccessExitStatus, so a close the "
                "human asked for will be reported as a unit failure")
    assert str(mv.USER_CLOSE_EXIT) in ok.group(1).split(), (
        f"SuccessExitStatus={ok.group(1)!r} does not include "
        f"{mv.USER_CLOSE_EXIT}")


# ── the spectrum backdrop ──────────────────────────────────────────────────
#
# PLN asked for a spectro "behind" the rain so the Ardour spectro VSTs could
# go. The whole risk of the feature is that it is an AUDIO consumer on a box
# that performs live, so what is asserted here is mostly about restraint: it
# must not exist until asked, it must genuinely go away when un-asked, and
# its numbers must not be able to paint silence as signal.

def test_the_spectro_does_not_exist_until_it_is_asked_for(monkeypatch):
    """Off by default means NO PROCESS, not a hidden one.

    A backdrop that spawned `pw-record` at startup and merely declined to
    paint would be exactly the always-on consumer this is not allowed to be,
    and nothing on screen would reveal it.
    """
    import os
    import pytest
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    mv, w = _widget(1.0, 900)
    if mv is None:
        pytest.skip("no Qt available")
    spawned = []
    monkeypatch.setattr(mv.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(
                            AssertionError("the backdrop spawned a capture unasked")))
    assert w.spectro is False
    assert w.spec_src is None
    w.tick()                              # a full frame with nobody asking
    w.grab()
    assert spawned == []
    w.close()


def test_toggling_the_spectro_acquires_and_releases_the_tap():
    """`s` on, `s` off — and off must hand the capture back, not hide it."""
    import os
    import pytest
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    mv, w = _widget(1.0, 900)
    if mv is None:
        pytest.skip("no Qt available")
    closed = []

    class FakeSource:
        hop, rate, err, frames = 256, 48000, "", 0

        def latest(self):
            return tuple(0.5 for _ in range(mv.SPEC_BANDS))

        def close(self):
            closed.append(True)

    w.set_spectro(True, FakeSource())
    assert w.spectro and w.spec_src is not None
    w.tick()
    assert w._spec_frame is not None, "a published frame never reached the paint state"
    w.set_spectro(False)
    assert not w.spectro and w.spec_src is None
    assert closed == [True], "switching the backdrop off left the capture running"
    assert w._spec_frame is None
    w.close()


def test_closing_the_window_releases_the_audio_tap():
    """A closed window must not outlive its own switch.

    The X writes the close latch and systemd will not restart us, so nothing
    would ever come back to turn a leaked `pw-record` off again.
    """
    import os
    import pytest
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    mv, w = _widget(1.0, 900)
    if mv is None:
        pytest.skip("no Qt available")
    closed = []

    class FakeSource:
        err, frames = "", 0

        def latest(self):
            return None

        def close(self):
            closed.append(True)

    w.set_spectro(True, FakeSource())
    w.close()
    assert closed == [True], "closeEvent left the capture on the master bus"


def test_band_edges_never_leave_a_band_empty():
    """Strictly increasing, in range, at every width the window can be.

    A 2048-point FFT has fewer bins than we have bands below ~750 Hz, so the
    naive log ramp produces duplicate edges — and a duplicated edge is a band
    that sums zero bins, which paints as a black hole in the bass. "Dark = not
    mapped" is the reading a cockpit trains you into (the LED work settled
    that), so an empty band is worse than a coarse one.
    """
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_mv_bands",
                                                  root / "midiviz.py")
    mv = importlib.util.module_from_spec(spec)
    _sys.modules["_mv_bands"] = mv
    spec.loader.exec_module(mv)
    for bands in (12, 24, mv.SPEC_BANDS, 64, 96):
        e = mv._spec_band_edges(bands, mv.SPEC_FFT, mv.SPEC_RATE)
        assert len(e) == bands + 1
        assert all(e[i] < e[i + 1] for i in range(bands)), \
            f"an empty band at bands={bands}: {list(e)}"
        assert e[0] >= 1 and e[-1] <= mv.SPEC_FFT // 2, \
            f"edges ran off the spectrum at bands={bands}: {e[0]}..{e[-1]}"


def test_the_analysis_seam_is_the_only_thing_the_paint_path_needs():
    """`push()` carries the whole feature; the subprocess carries none of it.

    This is what lets the backdrop be tested at all on a box with no audio
    server, and it is also the honest boundary: green here says nothing about
    `pw-record`, only that everything downstream of a block of samples works.
    """
    import math
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path
    import pytest
    np = pytest.importorskip("numpy")
    root = _Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_mv_push",
                                                  root / "midiviz.py")
    mv = importlib.util.module_from_spec(spec)
    _sys.modules["_mv_push"] = mv
    spec.loader.exec_module(mv)

    src = mv.SpectrumSource(target="__never__")
    t = np.arange(src.fft_n) / float(src.rate)
    for _ in range(30):
        frame = src.push(0.25 * np.sin(2 * math.pi * 3000.0 * t))
    assert len(frame) == mv.SPEC_BANDS
    assert all(0.0 <= v <= 1.0 for v in frame), "a band escaped 0..1"
    hi = int(np.argmax(frame))
    bin_hz = src.rate / float(src.fft_n)
    assert src.edges[hi] * bin_hz <= 3000.0 <= src.edges[hi + 1] * bin_hz, \
        "a 3 kHz tone did not land in the band that contains 3 kHz"
    for _ in range(120):
        quiet = src.push(np.zeros(src.fft_n, dtype=np.float32))
    assert all(v == v for v in quiet), "silence produced NaN, which paints full-height"
    assert max(quiet) < 0.02, f"silence read as signal: {max(quiet):.3f}"
    src.close()                          # never started; must still be safe


def test_the_backdrop_is_painted_behind_everything_else():
    """Ordering IS the feature — "overlay behind a basic spectro"."""
    import os
    import pytest
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    mv, w = _widget(1.0, 900)
    if mv is None:
        pytest.skip("no Qt available")
    order = []
    for name in ("_paint_spectrum", "_paint_chrome", "_paint_header",
                 "_paint_matrix", "_paint_gutter", "_paint_stream"):
        real = getattr(w, name)

        def probe(*a, _n=name, _r=real, **k):
            order.append(_n)
            return _r(*a, **k)
        setattr(w, name, probe)

    class FakeSource:
        err, frames = "", 0

        def latest(self):
            return tuple(0.6 for _ in range(mv.SPEC_BANDS))

        def close(self):
            pass

    w.set_spectro(True, FakeSource())
    w.tick()
    w.grab()
    assert order and order[0] == "_paint_spectrum", \
        f"the spectrum is not the first layer painted: {order}"
    w.set_spectro(False)
    w.close()


def test_a_short_pipe_read_is_not_mistaken_for_end_of_stream():
    """The bug that made the whole backdrop dead while the selftest passed.

    `Popen(bufsize=0)` hands back a raw `FileIO`: `read(n)` is one `os.read`
    and returns whatever the pipe holds, so a hop-sized read is short far more
    often than not. Treating that as EOF ended the capture on its first chunk
    (`blocks=0`, no error recorded) — and every pure-function test still went
    green, because none of them touched the pipe.
    """
    import io
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_mv_read",
                                                  root / "midiviz.py")
    mv = importlib.util.module_from_spec(spec)
    _sys.modules["_mv_read"] = mv
    spec.loader.exec_module(mv)

    class Dribble(io.RawIOBase):
        """A pipe that never gives you as much as you asked for."""

        def __init__(self, data, most=7):
            self.buf, self.most = bytearray(data), most

        def read(self, n=-1):
            take = min(n if n and n > 0 else 1, self.most, len(self.buf))
            out, self.buf = bytes(self.buf[:take]), self.buf[take:]
            return out

    payload = bytes(range(256)) * 8              # 2048 bytes
    got = mv._read_exact(Dribble(payload), len(payload))
    assert got == payload, "a dribbling stream lost or truncated bytes"
    # and the real end of stream still reports itself as one
    assert mv._read_exact(Dribble(b""), 16) is None
    assert mv._read_exact(Dribble(payload[:100]), len(payload)) is None, \
        "a stream that ends mid-block must read as EOF, not a partial frame"


# ── themes ─────────────────────────────────────────────────────────────────
#
# The paint loop indexes its palette blind: `self.lut[fam][lv]`,
# `self.tint[fam][0]`, `self.chrome[5]`, `self.spec_lut[lv]`. A theme that
# forgot one of those does not render wrong, it raises KeyError or IndexError
# inside `paintEvent`, at 25 fps, mid-set. That is what these pin: not how the
# themes look (screenshots did that, and the selftest paints every one), but
# that each of them answers every question the painter asks.

def _themed(theme):
    """A built widget on `theme`, or (None, None) where Qt is unavailable."""
    import importlib.util
    import os
    import sys as _sys
    from pathlib import Path as _Path
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root = _Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_mv_theme",
                                                  root / "midiviz.py")
    mv = importlib.util.module_from_spec(spec)
    _sys.modules["_mv_theme"] = mv
    spec.loader.exec_module(mv)
    try:
        _QtCore, _QtGui, QtWidgets = mv._qt()
    except Exception:
        return None, None
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = mv.build_widget("--", None, scale=1.0, theme=theme, persist=False)
    w.show()
    app.processEvents()
    return mv, w


def test_every_theme_answers_every_colour_the_painter_asks_for():
    import pytest
    mv, _ = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    for name in mv.THEMES:
        _, w = _themed(name)
        assert len(w.lut) == 8, f"{name}: a family lost its ink ramp"
        for fam in range(8):
            assert len(w.lut[fam]) == mv.LEVELS, \
                f"{name}: lut[{fam}] is not {mv.LEVELS} deep — _lv() indexes it blind"
            assert len(w.tint[fam]) == 4, \
                f"{name}: tint[{fam}] must hold 4 surfaces (idle + 3 heat steps)"
        assert len(w.chrome) == mv.LEVELS, \
            f"{name}: chrome is read at index 12 on hover; a short ramp raises there"
        assert len(w.spec_lut) == mv.SPEC_LEVELS, f"{name}: spectrum ramp is short"
        for probe in (w.bg, w.spec_peak_col, w.chrome[12], w.tint[0][0]):
            assert probe.isValid(), f"{name}: an invalid QColor reached the palette"


def test_no_theme_leaves_a_field_unset():
    """A Theme built by copying another one and editing is how a None gets in;
    `scan` and `chrome` are the only two fields allowed to be one."""
    import pytest
    if not hasattr(V, "THEMES"):
        pytest.skip("no themes in this tree")
    optional = {"chrome", "scan"}
    for name, th in V.THEMES.items():
        assert th.name == name, f"{name}: the key and the theme's own name differ"
        for field in th._fields:
            if field in optional:
                continue
            assert getattr(th, field) is not None, f"{name}.{field} is None"
    assert set(V.THEME_ORDER) <= set(V.THEMES), \
        "the cycle order names a theme that does not exist"
    assert V.DEFAULT_THEME in V.THEMES


def test_mapped_but_idle_is_visible_on_every_theme():
    """The design invariant, generalised.

    On dark it reads "must not be black"; on a light page the same rule is
    "must not be invisible against the page". Either way the floor tint of a
    mapped cell has to differ from the background by enough that an eye lands
    on it — a cell you cannot see reads as a control that is not wired, which
    is the hardware-fault misreading the LED work already paid for once.
    """
    import pytest
    mv, _ = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    for name in mv.THEMES:
        _, w = _themed(name)
        bg = w.bg
        for fam in range(8):
            floor = w.tint[fam][0]
            dist = (abs(floor.red() - bg.red()) + abs(floor.green() - bg.green())
                    + abs(floor.blue() - bg.blue()))
            assert dist >= 9, (
                f"{name}: the idle floor tint of family {fam} is "
                f"{floor.name()} against a {bg.name()} page — invisible, so "
                f"48 wired controls would read as unwired")
            # and it must never be the page itself
            assert floor.rgba() != bg.rgba(), f"{name}: floor tint IS the background"


def test_dark_stays_the_look_pln_trained_his_eye_on():
    """A ratchet, not a design statement.

    `dark` is the palette a dozen sets were played on. It was proven
    pixel-identical to the pre-theme render when themes landed; these few rgba
    values keep it that way, so a tweak aimed at `light` cannot quietly warm
    the cockpit by one step.
    """
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    assert w.bg.rgba() == 0xff0a0612
    assert w.lut[0][0].rgba() == 0xff08010e
    assert w.lut[0][8].rgba() == 0xff541c7c
    assert w.lut[0][15].rgba() == 0xffce8aff
    assert w.chrome[6].rgba() == w.lut[mv.FAM_OTHER][6].rgba(), \
        "on dark, chrome IS the FAM_OTHER ink ramp"
    for fam in range(8):
        for i in range(4):
            assert w.tint[fam][i].rgba() == w.lut[fam][1 + i].rgba(), (
                "on dark the surface ramp is the ink ramp at levels 1..4; that "
                "identity is what made the theme split invisible")


def test_switching_theme_drops_the_cached_scanline_pixmap():
    """The bug this prevents renders perfectly and looks broken anyway.

    The CRT texture is one cached pixmap, drawn in the theme's own colours and
    blitted whole every frame. Keep it across a theme change and a violet haze
    from the cockpit stays painted over a white page for the rest of the set.
    """
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    w._scanlines()
    assert w._scan is not None, "the texture did not cache at all"
    w.set_theme("light", save=False)
    assert w._scan is None, "a stale scanline pixmap survived the theme change"
    w.set_theme("sun", save=False)
    assert w.theme.scan is None and w._scanlines_blit is not None
    # sun has no texture: the blit must be a no-op, not a crash
    _QtCore, QtGui, _QtWidgets = mv._qt()
    pm = QtGui.QPixmap(w.width(), w.height())
    pm.fill(w.bg)
    p = QtGui.QPainter(pm)
    try:
        w._scanlines_blit(p)
    finally:
        p.end()


def test_the_menu_only_mirrors_actions_the_keyboard_already_had():
    """The menu is discoverability, not a second code path.

    Both faces call the same method, so this drives the KEYS and asserts the
    menu's checkmarks followed. If the menu ever grows its own copy of an
    action, this is where the two stop agreeing.
    """
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    _QtCore, QtGui, _QtWidgets = mv._qt()

    def press(key):
        w.keyPressEvent(QtGui.QKeyEvent(_QtCore.QEvent.Type.KeyPress,
                                        getattr(_QtCore.Qt.Key, key),
                                        _QtCore.Qt.KeyboardModifier.NoModifier))

    press("Key_P")
    assert w.paused and w._act_pause.isChecked(), "p paused but the menu says running"
    press("Key_P")
    assert not w.paused and not w._act_pause.isChecked()

    press("Key_T")
    assert w.on_top is False and w._act_pin.isChecked() is False
    press("Key_T")
    assert w.on_top is True and w._act_pin.isChecked() is True

    before = w.theme.name
    press("Key_D")
    assert w.theme.name != before, "d did not cycle the theme"
    assert w._theme_acts[w.theme.name].isChecked(), \
        "the theme cycled and the menu's radio did not follow"

    w.cc[13] = [64, 0.0, 0]
    press("Key_C")
    assert not w.cc, "c did not clear"

    s0 = w.scale
    size0 = (w.width(), w.height())
    press("Key_Plus")
    assert w.scale > s0, "+ did not grow the type"
    assert (w.width(), w.height()) == size0, \
        "+ resized the window: the size belongs to the WM now"
    press("Key_Minus")
    assert abs(w.scale - s0) < 1e-9, "+ then - did not return to the same scale"
    assert w._scale_acts[s0].isChecked(), \
        "the scale came back to 1.0 and the menu's radio did not"

    # The pin from the tray must not un-hide a window that was dismissed.
    w.set_window_shown(False)
    assert not w.isVisible()
    w._set_on_top(not w.on_top)
    assert not w.isVisible(), \
        "toggling always-on-top brought back a window hidden to the tray"
    w.set_window_shown(True)

    # every menu entry that names a key must have a handler wired
    labels = [a.text() for a in w.menu.actions() if a.text()]
    assert any("Quit" in t for t in labels)
    assert any("Pause" in t for t in labels)
    assert any("Clear" in t for t in labels)
    assert any("Spectrum" in t for t in labels)
    assert any("Always on top" in t for t in labels)


def test_quit_from_the_menu_records_a_deliberate_close(monkeypatch, tmp_path):
    """The tray's Quit must latch exactly like `q` does, or systemd's converge
    puts the window straight back and PLN reports "too sticky" a third time.

    XDG_RUNTIME_DIR is redirected because this drives a REAL deliberate close:
    without it, every suite run wrote `midiviz.closed` into the live runtime
    dir, which is the one file that tells rig_units.ensure() not to bring the
    monitor back. A test is not allowed to have an opinion about that.
    """
    import pytest
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    quit_act = [a for a in w.menu.actions() if a.text().startswith("Quit")]
    assert quit_act, "the menu has no Quit"
    assert not w._user_closed
    quit_act[0].trigger()
    assert w._user_closed, \
        "menu Quit closed the window without recording that a human asked"


def test_the_tray_degrades_silently_where_there_is_no_status_area():
    """Headless is the guard's own test case: no tray, and the window still
    builds and paints. Never failing to start is the point."""
    import pytest
    import importlib.util
    import os
    import sys as _sys
    from pathlib import Path as _Path
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root = _Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_mv_tray",
                                                  root / "midiviz.py")
    mv = importlib.util.module_from_spec(spec)
    _sys.modules["_mv_tray"] = mv
    spec.loader.exec_module(mv)
    try:
        _QtCore, _QtGui, QtWidgets = mv._qt()
    except Exception:
        pytest.skip("no Qt available")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        pytest.skip("this session HAS a tray; the degrade path is not reachable")
    w = mv.build_widget("--", None, scale=1.0, tray=True, persist=False)
    w.show()
    app.processEvents()
    assert w.tray is None, "a tray was constructed where the platform has none"
    assert w.grab().width() > 0, "the window did not paint without a tray"
    # hide/show is still available: it is the window's own state, not the tray's
    w.set_window_shown(False)
    assert not w.isVisible()
    w.set_window_shown(True)
    assert w.isVisible()


# ── the remembered choice ──────────────────────────────────────────────────
#
# This is the part most likely to be done carelessly, so it gets the most
# cases. A monitor that will not start because its config file is half-written
# is strictly worse than one with no memory at all — every one of these inputs
# must come back as "use the default", never as a traceback two minutes before
# a set.

def test_config_path_follows_xdg_then_falls_back_to_dot_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert V.config_path() == tmp_path / "xdg" / "parvagues" / "midiviz.json"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert V.config_path() == tmp_path / "home" / ".config" / "parvagues" / "midiviz.json"


def test_config_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert V.save_config("sun", 1.45) is True
    assert V.load_config() == {"theme": "sun", "scale": 1.45}


def test_a_missing_config_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    assert V.load_config() == {"theme": None, "scale": None}


def test_every_shape_of_bad_config_reads_as_no_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = V.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cases = {
        '{"theme": "sun", "sca': (None, None),          # truncated write
        "": (None, None),                               # zero-length file
        "not json at all": (None, None),
        '["sun", 1.5]': (None, None),                   # a list, not an object
        "42": (None, None),                             # a bare number
        '{"theme": "neon", "scale": 1.0}': (None, 1.0),  # a theme that was renamed
        '{"theme": 7, "scale": 1.0}': (None, 1.0),      # a theme that is not a name
        '{"theme": "light", "scale": "abc"}': ("light", None),
        '{"theme": "light", "scale": null}': ("light", None),
        '{"theme": "light", "scale": NaN}': ("light", None),
        '{"theme": "light", "scale": Infinity}': ("light", None),
        '{"theme": "light", "scale": 99}': ("light", 2.4),   # clamped, not refused
        '{"theme": "light", "scale": -3}': ("light", 0.6),
        "{}": (None, None),
    }
    for body, want in cases.items():
        path.write_text(body)
        got = V.load_config()
        assert (got["theme"], got["scale"]) == want, \
            f"{body!r} read back as {got}, not {want}"


def test_an_unreadable_config_reads_as_no_config(monkeypatch, tmp_path):
    import os
    import pytest
    if os.getuid() == 0:
        pytest.skip("root can read anything; the chmod guard proves nothing")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = V.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"theme": "sun", "scale": 1.2}')
    path.chmod(0o000)
    try:
        assert V.load_config() == {"theme": None, "scale": None}
    finally:
        path.chmod(0o600)


def test_a_config_directory_that_cannot_exist_fails_without_raising(monkeypatch,
                                                                    tmp_path):
    """XDG_CONFIG_HOME pointing at a FILE. mkdir cannot succeed, and a window
    must not die over being unable to remember a colour."""
    wall = tmp_path / "wall"
    wall.write_text("not a directory\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(wall))
    assert V.save_config("sun", 1.0) is False
    assert V.load_config() == {"theme": None, "scale": None}


def test_a_nan_scale_never_reaches_the_font_code(monkeypatch, tmp_path):
    """The one bad value that survives float(), min() and max() untouched and
    only detonates later, inside round() in `_apply_scale`, where the
    traceback points at the fonts and not at the config file."""
    assert V._clean_scale(float("nan")) is None
    assert V._clean_scale(float("inf")) is None
    assert V._clean_scale("1.2") == 1.2
    assert V._clean_scale(None) is None
    assert V._clean_scale([]) is None


def test_a_saved_theme_and_scale_come_back_on_the_next_window(monkeypatch,
                                                              tmp_path):
    """The whole point: the second time PLN is in the sun, it is already sunny."""
    import pytest
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    w._persist = True
    w.set_theme("sun")
    w._rescale(+0.15)
    cfg = mv.load_config()
    assert cfg["theme"] == "sun", "the theme change was not written"
    assert cfg["scale"] == w.scale, "the scale change was not written"
    # and a widget that was NOT asked to persist must not write at all
    (tmp_path / "parvagues" / "midiviz.json").unlink()
    _, w2 = _themed("dark")
    w2.set_theme("light")
    w2._rescale(-0.15)
    assert mv.load_config() == {"theme": None, "scale": None}, \
        "a widget built with persist=False rewrote the human's saved choice"


# ── the size is the WM's, the density is the window's ──────────────────────
#
# `_apply_scale` used to end in `resize(BASE_W * scale, BASE_H * scale)`, so
# every theme cycle that flipped the bold weight threw away whatever geometry
# the tiling WM had handed out. These pin the two halves of the fix separately,
# because each half is invisible on its own: type that does not follow the
# window looks like a font bug, and a window that snaps back looks like a WM
# bug, and the same line caused both.

def test_the_type_follows_the_window_and_stops_at_the_clamp():
    """Bigger window, bigger glyphs — and a bound at each end, or a 4K tile
    would ask for a 90 px face and a 200 px-tall one for a 3 px face."""
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    ref = (w._ref_w, w._ref_h)
    assert w._fit(*ref) == 1.0, \
        "the window is not 1.0× of the size it was born at — the density " \
        "reference disagrees with the window, so nothing below means anything"
    small = w._density(round(ref[0] * 0.7), round(ref[1] * 0.7))
    base = w._density(*ref)
    big = w._density(ref[0] * 2, ref[1] * 2)
    assert small < base < big, \
        f"the type does not follow the window: {small} / {base} / {big}"
    # The tight axis decides: a window stretched on ONE axis only must not
    # grow the type, or a short/wide tile clips vertically.
    assert w._density(ref[0] * 4, ref[1]) == base, \
        "a wider-but-not-taller window grew the type — the tight axis has to win"
    assert mv.DENS_MIN <= w._density(1, 1) <= mv.DENS_MAX
    assert w._density(20_000, 20_000) <= mv.DENS_MAX, "the clamp does not hold"
    assert w._density(1, 1) >= mv.DENS_MIN
    # The knob is a multiplier ON TOP of the fit, not a replacement for it.
    w.scale = 2.0
    assert w._density(*ref) > base


def test_neither_the_knob_nor_the_theme_may_resize_the_window():
    """The tiling-WM bug itself. Cycling the theme re-runs `_apply_scale`
    whenever the bold weight changes (`sun` is the bold one), and that is what
    used to snap a tiled window back to BASE × scale."""
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    w.resize(900, 560)
    before = (w.width(), w.height())
    px_before = w.f_main.pixelSize()
    w._rescale(+0.15)
    assert (w.width(), w.height()) == before, "the density knob resized the window"
    assert w.f_main.pixelSize() > px_before, "the density knob changed nothing"
    for name in mv.THEME_ORDER:                 # including the bold one
        w.set_theme(name, save=False)
        assert (w.width(), w.height()) == before, \
            f"switching to {name} resized the window"
    # …and the one action that IS allowed to, is allowed to.
    w.reset_size()
    assert (w.width(), w.height()) == (w._ref_w, w._ref_h), \
        "Reset size did not return the window to the size it was born at"


def test_the_metrics_are_idempotent_and_re_measured_when_the_weight_flips():
    """`resizeEvent` runs the whole re-measure, so it has to be free of
    side effects — and it must NOT skip the one case where the pixel size is
    unchanged and the FACE is not: `sun` is bold, and the ribbon's run
    concatenation assumes a measured advance."""
    import pytest
    mv, w = _themed("light")
    if mv is None:
        pytest.skip("no Qt available")
    w.resize(880, 540)
    w._apply_scale()
    first = (w.mw, w.mh, w.uw, w.uh, w.gut_x, w.cw, w.ch)
    w._apply_scale()
    assert (w.mw, w.mh, w.uw, w.uh, w.gut_x, w.cw, w.ch) == first, \
        "re-applying the same size and scale moved the layout"
    bold_before = w.f_main.bold()
    w.set_theme("sun", save=False)
    assert w.f_main.bold() != bold_before, \
        "the bold weight did not reach the font: the metrics key skipped it"


def test_a_grab_zone_is_never_the_body():
    """Corner, edge, body — and a press in the BODY must never resize, or
    dragging the lens across a screen would reshape it instead of moving it."""
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    QtCore, _QtGui, _QtWidgets = mv._qt()
    Qt = QtCore.Qt
    w.resize(900, 560)
    W, H = w.width(), w.height()
    P = QtCore.QPoint

    def at(x, y):
        return w._edges_at(P(x, y))

    L, R = Qt.Edge.LeftEdge, Qt.Edge.RightEdge
    T, B = Qt.Edge.TopEdge, Qt.Edge.BottomEdge
    assert at(0, 0) == L | T
    assert at(W - 1, 0) == R | T
    assert at(0, H - 1) == L | B
    assert at(W - 1, H - 1) == R | B
    # an edge, far from any corner, is exactly one edge
    assert at(1, H // 2) == L
    assert at(W - 1, H // 2) == R
    assert at(W // 2, 0) == T
    assert at(W // 2, H - 1) == B
    for x, y in ((W // 2, H // 2), (mv.GRAB_PX + 2, H // 2),
                 (W // 2, mv.GRAB_PX + 3), (W - mv.GRAB_PX - 2, H // 2)):
        assert not at(x, y), f"({x},{y}) is the body and it reported a resize edge"
    # the body's cursor stays the move cursor; a corner gets a diagonal one
    assert w._grab_cursor(at(W // 2, H // 2)) == Qt.CursorShape.SizeAllCursor
    assert w._grab_cursor(at(0, 0)) == Qt.CursorShape.SizeFDiagCursor
    assert w._grab_cursor(at(W - 1, 0)) == Qt.CursorShape.SizeBDiagCursor
    assert w._grab_cursor(at(1, H // 2)) == Qt.CursorShape.SizeHorCursor
    assert w._grab_cursor(at(W // 2, H - 1)) == Qt.CursorShape.SizeVerCursor


def test_the_close_control_wins_over_the_top_edge(monkeypatch, tmp_path):
    """At the birth size the X's hit rect starts INSIDE the top grab band, so
    the order of the two tests in `mousePressEvent` is load-bearing: a click
    meant for the close must close, never begin a resize. The latch goes to a
    tmp runtime dir — a test must not tell the rig's converge that a human
    closed the monitor."""
    import pytest
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    QtCore, QtGui, _QtWidgets = mv._qt()
    Qt = QtCore.Qt
    pin, close = w._ctrl_rects()
    assert close.top() < mv.GRAB_PX, (
        "this test is moot at this size: the X no longer reaches into the top "
        "grab band, so it proves nothing about the ordering")
    # …and both controls stay clear of the two top CORNERS, which are the
    # zones a hand aims at to make the window bigger.
    assert pin.left() > mv.CORNER_PX and close.right() < w.width() - mv.CORNER_PX

    p = close.center()
    w.mousePressEvent(QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.QPointF(p.x(), p.y()), QtCore.QPointF(p.x(), p.y()),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    assert w._user_closed, "a click on the X did not close the window"
    assert not w._resize_edges and w._resize_from is None, \
        "a click on the X armed a resize instead"


def test_a_body_drag_moves_and_leaves_the_geometry_alone():
    """A manual resize (the fallback path, when the compositor refuses) only
    ever runs for a press that grabbed an edge."""
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    QtCore, QtGui, _QtWidgets = mv._qt()
    Qt = QtCore.Qt
    w.resize(900, 560)
    w.show()

    def press(x, y):
        return QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QPointF(x, y), QtCore.QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)

    w.mousePressEvent(press(w.width() // 2, w.height() // 2))
    assert not w._resize_edges and w._resize_from is None, \
        "a press in the body armed a resize"
    w.mouseReleaseEvent(None)

    # and the manual rect respects minimumSize, whichever edge is dragged
    w._resize_edges = Qt.Edge.LeftEdge | Qt.Edge.TopEdge
    r0 = QtCore.QRect(100, 100, 900, 560)
    got = w._resized_rect(r0, QtCore.QPoint(10_000, 10_000))
    assert got.width() >= w.minimumWidth() and got.height() >= w.minimumHeight(), \
        f"a crushing drag got through the minimum size: {got}"
    assert got.right() == r0.right() and got.bottom() == r0.bottom(), \
        "dragging the left/top edge moved the opposite edge"
    got = w._resized_rect(r0, QtCore.QPoint(-200, -100))
    assert got.width() == 1100 and got.height() == 660
    w._resize_edges = Qt.Edge.RightEdge | Qt.Edge.BottomEdge
    got = w._resized_rect(r0, QtCore.QPoint(60, 40))
    assert (got.x(), got.y()) == (100, 100), \
        "dragging the right/bottom edge moved the window"
    assert (got.width(), got.height()) == (960, 600)


def test_every_theme_gets_a_swatch_in_its_own_colours():
    """The menu's whole point is "make it colour, now", so a swatch that came
    out of the CURRENT palette (three identical chips) would be worse than no
    swatch at all."""
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    pages = {}
    for name, th in mv.THEMES.items():
        act = w._theme_acts[name]
        assert not act.icon().isNull(), f"{name} has no swatch"
        img = act.icon().pixmap(36, 36).toImage()
        assert img.width() >= 16, f"{name}'s swatch came back empty"
        cols = {img.pixel(x, y) for x in range(4, img.width() - 4)
                for y in range(4, img.height() - 4)}
        assert len(cols) >= 3, \
            f"{name}'s swatch is flat ({len(cols)} colours): it says nothing"
        pages[name] = img.pixel(img.width() - 3, 3)
        assert mv.THEME_MENU[name].split()[0].lower() in act.text().lower()
    assert len(set(pages.values())) == len(pages), \
        f"two themes painted the same page colour: {pages}"


def test_a_corner_press_really_arms_a_resize():
    """The one call in this file with a binding-type question in it.

    `startSystemResize` takes the compositor's word for it and returns a bool;
    the manual path only runs when it says no. Nothing else in the suite sends
    a press into a grab band, so if PySide6 rejected the flag type, resizing
    would be dead on the real desktop with every test still green. Offscreen
    there IS no compositor, so the fallback is what gets exercised here —
    which is also the X11 path.
    """
    import pytest
    mv, w = _themed("dark")
    if mv is None:
        pytest.skip("no Qt available")
    QtCore, QtGui, _QtWidgets = mv._qt()
    Qt = QtCore.Qt
    w.resize(900, 560)
    w.show()

    def press(x, y):
        w.mousePressEvent(QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QPointF(x, y), QtCore.QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))

    def move(gx, gy):
        w.mouseMoveEvent(QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove,
            QtCore.QPointF(0, 0), QtCore.QPointF(gx, gy),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))

    for x, y in ((0, 0), (w.width() - 1, w.height() - 1)):
        press(x, y)
        armed = w._resize_from is not None
        assert armed or not w._resize_edges, (
            "a corner press neither armed the manual resize nor handed the "
            "drag to the compositor — startSystemResize rejected the edges")
        w.mouseReleaseEvent(None)
        assert w._resize_from is None and not w._resize_edges

    # the bottom-right grab, dragged out: bigger, and the top-left corner of
    # the window stays exactly where it was
    before = w.geometry()
    press(w.width() - 1, w.height() - 1)
    if w._resize_from is None:
        pytest.skip("the platform took the resize; nothing to assert by hand")
    g0 = w._resize_from[0]
    move(g0.x() + 80, g0.y() + 40)
    assert w.width() > before.width() and w.height() > before.height(), \
        "dragging the bottom-right corner did not grow the window"
    assert (w.x(), w.y()) == (before.x(), before.y()), \
        "dragging the bottom-right corner moved the window as well"
    # …and the type followed, without anybody calling _apply_scale by hand
    assert w.dens == w._density(), "the density did not follow the resize"
    w.mouseReleaseEvent(None)
