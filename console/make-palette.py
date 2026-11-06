#!/usr/bin/env python3
"""Build vtrgb.gpufan, the 16-colour palette the Linux VT loads via setvtrgb.

The kernel console shows at most 16 colours at once, so Rich downgrades the
dashboard's truecolor styles to the nearest of 16.  Which 16 those are is ours to
choose, so we point them at the theme's own values: every colour the live view
uses then lands on a slot holding its exact RGB instead of a generic ANSI colour.

Slots are chosen from the measured targets Rich's downgrade actually picks, and
the colours are read straight out of fan_look.py so this cannot drift out of sync
with the theme.  Slots 4 and 5 are set to points along the ACCENT -> ACCENT2 line
so the title gradient has intermediate steps to quantise onto; with the matching
Rich hook (gpufan_vt_palette.py) that takes the gradient from 2 steps to 4.

Slots 1, 2 and 3 stay recognisably red/green/yellow, just darkened towards the
theme, so ls, systemd and everything else on the console still read correctly.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOOK = os.path.join(HERE, os.pardir, "fan_look.py")
DST = os.path.join(HERE, "vtrgb.gpufan")

HEX = re.compile(r'^#[0-9a-fA-F]{6}$')


def theme(path=LOOK):
    """The "#rrggbb" constants assigned at the top level of fan_look.py."""
    out = {}
    with open(path) as fh:
        for line in fh:
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            lhs, _, rhs = line.partition("=")
            names = [n.strip() for n in lhs.split(",")]
            vals = [v.strip().strip('"\'') for v in rhs.split(",")]
            if len(names) != len(vals) or not all(n.isidentifier() for n in names):
                continue
            if not all(HEX.match(v) for v in vals):
                continue
            out.update(zip(names, vals))
    missing = {"BG", "INK", "INK_SOFT", "FAINT", "ACCENT", "ACCENT2",
               "ACCENT_TEXT", "OK", "OK_TEXT", "WARN", "DANGER"} - set(out)
    if missing:
        sys.exit(f"{path}: could not find {sorted(missing)}")
    return out


rgb = lambda h: [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def mix(a, b, t):
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(rgb(a), rgb(b)))


def palette(t):
    """slot -> "#rrggbb", 0..15, in the kernel's ANSI order."""
    return {
        0:  t["BG"],                           # black
        1:  mix(t["DANGER"], t["BG"], 0.35),   # red      (darkened, for other programs)
        2:  mix(t["OK"], t["BG"], 0.35),       # green    (darkened)
        3:  mix(t["WARN"], t["BG"], 0.35),     # yellow   (darkened)
        4:  mix(t["ACCENT"], t["ACCENT2"], 0.58),   # blue     <- gradient step
        5:  mix(t["ACCENT"], t["ACCENT2"], 0.28),   # magenta  <- gradient step
        6:  t["OK"],                           # cyan     <- rich maps OK here
        7:  t["INK_SOFT"],                     # white
        8:  t["FAINT"],                        # bright black
        9:  t["DANGER"],                       # bright red
        10: t["OK_TEXT"],                      # bright green
        11: t["WARN"],                         # bright yellow
        12: t["ACCENT"],                       # bright blue    <- gradient start
        13: t["ACCENT_TEXT"],                  # bright magenta
        14: t["ACCENT2"],                      # bright cyan    <- gradient end
        15: t["INK"],                          # bright white
    }


def main():
    t = theme()
    p = palette(t)
    rows = [[rgb(p[i])[j] for i in range(16)] for j in range(3)]
    with open(DST, "w") as fh:
        fh.write("\n".join(",".join(str(v) for v in r) for r in rows) + "\n")
    names = {v: k for k, v in t.items()}
    print(f"theme read from {os.path.relpath(LOOK, HERE)}")
    for i in range(16):
        tag = names.get(p[i], "gradient step" if i in (4, 5) else "derived")
        print(f"  slot {i:2d}  {p[i]}  {tag}")
    print(f"\nwrote {DST}")


if __name__ == "__main__":
    main()
