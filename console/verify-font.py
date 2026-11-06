#!/usr/bin/env python3
"""Prove the built font is correct, before it is installed.

Asserts nothing went missing (ASCII, Latin-1, box drawing, block elements, all
256 braille, and the exact glyph set fan_look.py / fan_dash.py draw with), that
braille did not collapse onto shared glyphs, then rasterises a braille curve as
ASCII art so the dot geometry can be eyeballed.  Exits non-zero on any failure.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psf1

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "Uni2-Fixed16-blocks.psf.gz")

# every non-ASCII glyph fan_look.py and fan_dash.py draw with
APP = "–·°→⏸━│●─└┤▀▁▂▃▄▅▆▇█▌▐░▒▓▔▕▖▗▘▙▚▛▜▝▞▟▉▊▋▍▎▏"

f = psf1.load(FONT)
m = psf1.cpmap(f)
G = lambda ch: f["glyphs"][m[ord(ch)]]

checks = {
    "ASCII U+0020-007E":      [c for c in range(0x20, 0x7F) if c not in m],
    "Latin-1 U+00A0-00FF":    [c for c in range(0xA0, 0x100) if c not in m],
    "box drawing U+2500-257F": [c for c in range(0x2500, 0x2580) if c not in m],
    "blocks U+2580-259F":     [c for c in range(0x2580, 0x25A0) if c not in m],
    "braille U+2800-28FF":    [c for c in range(0x2800, 0x2900) if c not in m],
    "app glyph set":          [ord(c) for c in APP if ord(c) not in m],
}
bad = False
for name, miss in checks.items():
    print(f"  {name:26s} {'OK' if not miss else 'MISSING ' + ' '.join(f'U+{c:04X}' for c in miss[:8])}")
    bad |= bool(miss)

slots = {m[c] for c in range(0x2800, 0x2900)}
print(f"  {'distinct braille glyphs':26s} {len(slots)}/256 {'OK' if len(slots) == 256 else 'COLLAPSED'}")
bad |= len(slots) != 256

blank = [c for c in range(0x2801, 0x2900) if not any(f["glyphs"][m[c]])]
print(f"  {'non-blank braille glyphs':26s} {255 - len(blank)}/255 "
      f"{'OK' if not blank else 'BLANK: ' + ' '.join(f'U+{c:04X}' for c in blank[:8])}")
bad |= bool(blank)
print(f"  {'codepoints mapped':26s} {sum(len(s) for s in f['singles'])}")

if bad:
    sys.exit("\nFAILED: the font is not safe to install")

# ---- visual proof: the same braille a line graph emits ----------------------
BR = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
W, ROWS = 30, 3
dots = {}
for x in range(W * 2):
    y = int((math.sin(x / 5.0) * 0.5 + 0.5) * (ROWS * 4 - 1))
    yy = ROWS * 4 - 1 - y
    dots[(x // 2, yy // 4)] = dots.get((x // 2, yy // 4), 0) | BR[x % 2][yy % 4]
print("\na braille curve, rasterised exactly as the console will draw it:")
for cr in range(ROWS):
    cells = [chr(0x2800 | dots.get((c, cr), 0)) for c in range(W)]
    for r in range(16):
        print("  " + "".join("".join("#" if G(ch)[r] & (0x80 >> c) else " "
                                     for c in range(8)) for ch in cells))
print("\nOK")
