#!/usr/bin/env python3
"""Build Uni2-Fixed16-blocks.psf.gz, the console font the live view needs.

The Linux VT renders from one bitmap page capped at 512 glyphs, so it cannot
carry everything a terminal emulator's font can.  This spends that budget on
what the console actually draws:

  ASCII + Latin-1          kept from the stock font
  box drawing  U+2500-257F kept from the stock font (all 128)
  block elements U+2580-259F  drawn here, geometrically -> pixel exact
  braille      U+2800-28FF drawn here, all 256, 2x4 dots -> full resolution

Greek and Cyrillic are dropped to pay for braille.  Anything still absent gets a
many-to-one unicode mapping onto the nearest glyph present, which costs no slots
because PSF lets any number of codepoints share one glyph.

Why this is needed: gpu-fan-status -w draws its curve and line graphs with
runtime-generated braille -- fan_look.py has
    BRAILLE = ((0x01,0x02,0x04,0x40), (0x08,0x10,0x20,0x80))
and emits chr(0x2800 | bits).  Grepping the source for literal non-ASCII finds
no braille at all, which is misleading: the codepoints only exist at runtime.
Codepoints absent from the font's unicode map fall through to raw font
positions, so the graphs come out as random letters.  Stock Uni2-Fixed16 also
lacks every Block Element past "full", "light shade" and "medium shade", and no
stock font in /usr/share/consolefonts has the eighth-blocks.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psf1

HERE = os.path.dirname(os.path.abspath(__file__))
# The package-shipped font first, so this works on a machine where console-setup
# has never run and /etc/console-setup/cached_* does not exist yet.
SOURCES = ("/usr/share/consolefonts/Uni2-Fixed16.psf.gz",
           "/etc/console-setup/cached_Uni2-Fixed16.psf.gz")
DST = os.path.join(HERE, "Uni2-Fixed16-blocks.psf.gz")

H, FULL, LEFT, RIGHT = 16, 0xFF, 0xF0, 0x0F

row = lambda spec: bytearray(spec)
bottom = lambda n: row([0x00] * (H - n) + [FULL] * n)
top = lambda n: row([FULL] * n + [0x00] * (H - n))
cols = lambda mask: row([mask] * H)
quad = lambda up, lo: row([up] * (H // 2) + [lo] * (H // 2))


def source():
    for p in SOURCES:
        if os.path.exists(p):
            return p
    sys.exit(f"none of these exist, install the console-setup package:\n  " +
             "\n  ".join(SOURCES))


# ---- block elements, drawn geometrically ------------------------------------
BLOCKS = {
    0x2580: top(8),      0x2581: bottom(2),   0x2582: bottom(4),  0x2583: bottom(6),
    0x2584: bottom(8),   0x2585: bottom(10),  0x2586: bottom(12), 0x2587: bottom(14),
    0x2588: cols(FULL),  0x2594: top(2),      0x2595: cols(0x01),
    0x258F: cols(0x80),  0x258E: cols(0xC0),  0x258D: cols(0xE0), 0x258C: cols(LEFT),
    0x258B: cols(0xF8),  0x258A: cols(0xFC),  0x2589: cols(0xFE), 0x2590: cols(RIGHT),
    0x2596: quad(0x00, LEFT),   0x2597: quad(0x00, RIGHT),
    0x2598: quad(LEFT, 0x00),   0x259D: quad(RIGHT, 0x00),
    0x2599: quad(LEFT, FULL),   0x259A: quad(LEFT, RIGHT),
    0x259B: quad(FULL, LEFT),   0x259C: quad(FULL, RIGHT),
    0x259E: quad(RIGHT, LEFT),  0x259F: quad(RIGHT, FULL),
    0x23F8: row([0x00] * 3 + [0x66] * 10 + [0x00] * 3),      # pause, for fan_dash.py
}

# ---- braille, drawn at full 2x4 dot resolution ------------------------------
# A braille cell is 2 columns x 4 rows of dots.  Dots are 2px squares at x=1,2
# and x=4,5, on rows 1,5,9,13 -- evenly spaced down the 16px cell.
DOTCOL = (0x60, 0x0C)
DOTROW = (1, 5, 9, 13)
BRAILLE = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))   # == fan_look.py


def braille_glyph(bits):
    g = [0x00] * H
    for cx in range(2):
        for cy in range(4):
            if bits & BRAILLE[cx][cy]:
                for dy in range(2):
                    g[DOTROW[cy] + dy] |= DOTCOL[cx]
    return row(g)


# ---- what keeps a real slot -------------------------------------------------
KEEP = set(range(0x20, 0x7F))                 # ASCII
KEEP |= set(range(0xA0, 0x100))               # Latin-1, so accented letters survive
KEEP |= set(range(0x2500, 0x2580))            # box drawing
KEEP |= {0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026,
         0x2190, 0x2191, 0x2192, 0x2193, 0x25A0, 0x25CF, 0x00B0, 0x00B7}

# ---- many-to-one fallbacks, so nothing falls through to garbage -------------
FALLBACK = {
    0x2190: [0x21A9, 0x2B05, 0x23CE, 0x232B],
    0x2192: [0x21AA, 0x2B0C, 0x2194, 0x27A1, 0x25B8, 0x25B9, 0x2B95],
    0x2191: [0x2196, 0x2197, 0x2B06, 0x2195, 0x2B0D],
    0x2193: [0x2198, 0x2199, 0x2B07],
    0x25CF: [0x2B24, 0x2B58, 0x26AB, 0x26AA, 0x25CB, 0x25EF],
    0x25A0: [0x25AA, 0x25A1, 0x25AF, 0x25AE, 0x25B0, 0x2B1B, 0x2B1C],
    0x00B7: [0x2023, 0x25AB, 0x25B1, 0x2219],
    0x002A: [0x2B51, 0x2605, 0x2606, 0x204E, 0x2055],
    0x2594: [0x203E],
    0x2502: [0x2503, 0x2551],
}


def main():
    src = source()
    f = psf1.load(src)
    if f["nglyphs"] != 512 or f["charsize"] != 16:
        sys.exit(f"{src}: expected a 512-glyph 8x16 font, got "
                 f"{f['nglyphs']} glyphs 8x{f['charsize']}")
    m = psf1.cpmap(f)
    print(f"source: {src}")

    draw = dict(BLOCKS)
    light = f["glyphs"][m[0x2591]]
    draw[0x2591] = light
    draw[0x2592] = f["glyphs"][m[0x2592]]
    draw[0x2593] = bytearray((~b) & 0xFF for b in light)   # dark shade: inverse of light
    draw.update({0x2800 + b: braille_glyph(b) for b in range(256)})

    keep_slots = {m[c] for c in KEEP if c in m}
    pool = [i for i in range(f["nglyphs"])
            if i not in keep_slots and not any(m.get(c) == i for c in draw)]
    need = sum(1 for cp in draw if cp not in m)
    print(f"slots: {len(keep_slots)} kept for text/box, {need} needed, {len(pool)} in pool")
    if len(pool) < need:
        sys.exit(f"short by {need - len(pool)} slots")

    drawn = redrawn = 0
    for cp, bitmap in sorted(draw.items()):
        if cp in m and m[cp] in keep_slots:
            f["glyphs"][m[cp]] = bitmap
            redrawn += 1
            continue
        if cp in m:
            i = m[cp]
            if i in pool:
                pool.remove(i)
        else:
            i = pool.pop(0)
        f["glyphs"][i] = bitmap
        f["singles"][i] = [cp]
        f["seqs"][i] = []
        m[cp] = i
        drawn += 1
    print(f"drawn: {drawn} assigned, {redrawn} redrawn in place, {len(pool)} spare")

    extra = 0
    for tgt, cps in FALLBACK.items():
        if tgt not in m:
            continue
        for cp in cps:
            if cp in m:
                continue
            f["singles"][m[tgt]].append(cp)
            m[cp] = m[tgt]
            extra += 1
    print(f"fallbacks: {extra} codepoints mapped onto existing glyphs")

    n = psf1.save(f, DST)
    print(f"\n{DST}\n  {f['nglyphs']} glyphs, "
          f"{sum(len(s) for s in f['singles'])} codepoints, {n} bytes gzipped")


if __name__ == "__main__":
    main()
