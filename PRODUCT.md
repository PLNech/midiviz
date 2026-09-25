# Product

## Register

product

## Users

Live performers and MIDI musicians who watch a control surface while playing:
livecoders (Tidal/Strudel/Supercollider), electronic performers, anyone with a
MIDI controller and a screen near the stage. They are mid-performance,
glancing, not reading; the tool must answer "what did I just touch and what is
resting where" in under a second. Secondary audience: developers who want a
readable example of a small, disciplined Qt/GLib-free ALSA visualizer.

## Product Purpose

midiviz turns a live MIDI stream into a picture: position, glyph, bar, hex,
brightness. Zero English words on the canvas. It exists because transcripts of
MIDI events are unreadable at performance speed (~400 events/second on a fader
sweep). Success: a performer glances, knows the surface state, returns to the
music.

## Brand Personality

The Ship's Bridge (ParVagues' design language): an instrument panel, not a
toy. Three words: legible, calm, precise. Voice in copy: dry, specific,
competent; the tool explains itself in one line and gets out of the way. No
hype, no exclamation marks, no marketing adjectives.

## Anti-references

- Synth-vendor marketing pages (neon gradients, "unleash your creativity").
- Dashboard slop: glassmorphism, decorative charts, rounded-everything.
- Em-dash-heavy aphoristic prose that performs cleverness instead of
  documenting the tool.

## Design Principles

- Glance, don't read: the picture carries the information; copy only orients.
- Measure, don't decorate: every visual channel encodes data.
- Honest failure: when a layer is missing, it stays dark; the chrome says so.
- The rig is the contract: wire paths and port names are documented, not
  invented.

## Accessibility & Inclusion

Three themes exist because one contrast profile cannot serve every ambient
light (dark stage, pale desktop, direct sun). The `sun` theme is the
accessibility tier: near-white, near-black ink at every decay level, thick
bars, no CRT texture. Test suite holds every theme to a mapped-but-idle
visibility contract.
