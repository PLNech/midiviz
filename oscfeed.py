#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""oscfeed — the Tidal HL feed: SuperDirt's own /play packets, snoopable.

Tidal does not talk to SuperDirt alone. `BootTidal.hs` mirrors every
`superdirtShape` packet to a second target (port `VIZ_PORT`), so anything may
listen to what the music is DOING — which orbit triggered, with which values —
without touching the audio path, the way the Pulsar editor already reads its
own highlight feed. This module is that listener for midiviz.

Two halves, both importable without Qt or ALSA:

* `decode_osc` — the smallest OSC decoder the packets need. Pure function,
  unit-tested against hand-built bytes.
* `HLFeed` — a UDP daemon thread with the same shape as midiviz's `Reader`
  (bounded deque, drain, close): a live monitor wants the PRESENT, not a
  backlog, so the same lesson as `midistream.DEPTH` applies.
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from collections import deque

# BootTidal.hs sends the mirror here. High enough to dodge everything
# scsynth/sclang grab by default, referenced by nothing else in the repo.
VIZ_PORT = 57130

# Tidal's orbits are 0-based (`orbit: 0` is d1) and the rig runs 14 of them.
MIN_ORBIT, MAX_ORBIT = 1, 14


def _skip_pad(data: bytes, i: int) -> int:
    """OSC blobs are 4-aligned; return the index past the padding."""
    return i + ((4 - (i % 4)) % 4)


def _read_string(data: bytes, i: int) -> tuple[str, int]:
    end = data.index(b"\0", i)
    return data[i:end].decode("utf-8", "replace"), _skip_pad(data, end + 1)


def _read_blob(data: bytes, i: int) -> tuple[bytes, int]:
    (n,) = struct.unpack_from(">i", data, i)
    i += 4
    return data[i:i + n], i + n + ((4 - ((i + n) % 4)) % 4)


def decode_osc(data: bytes) -> list[dict]:
    """One UDP datagram → the OSC messages inside it.

    Tidal's Stream sends either bare messages or `#bundle`s (one timetag,
    then length-prefixed elements). Returns [{"addr", "args"}] with `args`
    the *named* argument pairs a /play carries: Tidal→SuperDirt arguments
    are ("gain", 0.8) style name/value runs, so a flat dict is the shape
    every consumer wants. Unnamed args land under "0", "1", …
    """
    out: list[dict] = []
    stack = [data]
    while stack:
        cur = stack.pop(0)
        if not cur:
            continue
        if cur.startswith(b"#bundle"):
            i = 16                                    # "#bundle\0" + 8-byte timetag
            while i + 4 <= len(cur):
                (n,) = struct.unpack_from(">i", cur, i)
                i += 4
                if n:
                    stack.append(cur[i:i + n])
                i = _skip_pad(cur, i + n)
            continue
        try:
            addr, i = _read_string(cur, 0)
            tags, i = _read_string(cur, i)            # ",ifs…" — the comma is part of it
            args: dict = {}
            slot = 0
            for t in tags[1:]:
                if t == "i":
                    (v,) = struct.unpack_from(">i", cur, i)
                    i += 4
                elif t == "f":
                    (v,) = struct.unpack_from(">f", cur, i)
                    i += 4
                elif t == "d":
                    (v,) = struct.unpack_from(">d", cur, i)
                    i += 8
                elif t == "s":
                    v, i = _read_string(cur, i)
                elif t == "b":
                    v, i = _read_blob(cur, i)
                else:
                    v = None                          # T/F/I/N… carry no bytes
                args[str(slot)] = v
                slot += 1
        except (ValueError, struct.error, IndexError):
            continue                                  # one bad element, not the datagram
        # A /play message's named pairs are positional: ("gain", v), ("orbit", v)…
        # Re-fold every ("string", value) run into a dict without losing the
        # originals — the names ARE the Tidal parameter names.
        named: dict = {}
        k = 0
        while k < len(args):
            key = args.get(str(k))
            if isinstance(key, str) and str(k + 1) in args:
                named[key] = args[str(k + 1)]
                k += 2
            else:
                k += 1
        out.append({"addr": addr, "args": named})
    return out


def play_orbit(msg: dict) -> int | None:
    """A decoded message → its 1-based orbit number, or None if not a trigger.

    Tidal 1.9's superdirtShape addresses its messages `/dirt/play`, not
    `/play` — captured off the wire on this rig — and a decoder that only
    accepted the documentation's address silently dropped every real packet
    while every synthetic test passed. Accept both spellings."""
    if msg["addr"] not in ("/play", "/dirt/play"):
        return None
    o = msg["args"].get("orbit")
    if isinstance(o, float) and o == int(o):
        o = int(o)
    if not isinstance(o, int) or not (0 <= o <= MAX_ORBIT):
        return None
    return o + 1                                       # Tidal 0-based → d1-based


class HLFeed:
    """UDP listener on `VIZ_PORT`; a bounded deque the GUI drains each frame.

    Same contract as midiviz.Reader — start/drain/close/alive/error — so the
    tick can treat the two feeds identically. A missed datagram is a missed
    frame of truth, never a queue: bounded like the Reader, for the same
    recorded reason.
    """

    DEPTH = 512

    def __init__(self, port: int = VIZ_PORT):
        self.port = port
        self.error: str | None = None
        self.total = 0
        self._q: deque[dict] = deque(maxlen=self.DEPTH)
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> "HLFeed":
        threading.Thread(target=self._run, daemon=True,
                         name="oscfeed").start()
        return self

    def _run(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("127.0.0.1", self.port))
            self._sock.settimeout(0.5)
        except OSError as exc:
            self.error = type(exc).__name__
            return
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    self.error = "recv"
                break
            now = time.monotonic()
            # One malformed datagram must not kill the feed the way an
            # uncaught exception in a daemon thread dies silently: skip it.
            try:
                msgs = decode_osc(data)
            except (ValueError, struct.error, IndexError):
                continue
            first = False
            for msg in msgs:
                o = play_orbit(msg)
                if o is None:
                    continue
                with self._lock:
                    self._q.append({"orbit": o, "t": now,
                                    "params": msg["args"]})
                    self.total += 1
                    first = self.total == 1
            if first:
                # One line, once: the question "is Tidal even sending?" is
                # answered by the journal, not by staring at a silent grid.
                print("⚓ HL feed: first trigger received (d%d)" % o,
                      file=sys.stderr, flush=True)

    def alive(self) -> bool:
        return self._sock is not None and self.error is None

    def drain(self) -> list[dict]:
        with self._lock:
            if not self._q:
                return []
            out = list(self._q)
            self._q.clear()
        return out

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
