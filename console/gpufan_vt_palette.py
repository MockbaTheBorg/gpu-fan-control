"""Teach Rich the Linux VT's real 16-colour palette, on the VT only.

The kernel console shows at most 16 colours at once, so Rich downgrades the
dashboard's truecolor styles to the nearest of 16.  It picks "nearest" against
its own built-in table of standard ANSI values -- but TRipper's VTs run a custom
palette (/etc/vtrgb, see /etc/console-setup/vtrgb.gpufan) tuned to the dashboard theme,
so that table is wrong and the comparison lands on the wrong slots.  Handing Rich
the palette that is actually loaded fixes the match: the title gradient goes from
2 distinct steps to 4, with the right hues.

Read from /etc/vtrgb rather than hardcoded, so this cannot drift out of sync with
whatever palette is really in force.

Loaded by zz_gpufan_vt_palette.pth at interpreter startup, before anything
imports rich, which matters because rich caches colour downgrades.  It is a no-op
over SSH or in any terminal that is not a local VT, where truecolor is used and
no downgrade happens at all.  Every failure path is silent: the dashboard must
still run if this file is wrong.
"""


def _on_linux_vt() -> bool:
    """True when a standard stream is a kernel VT (/dev/tty1../dev/tty63)."""
    import os
    import re
    for fd in (1, 2, 0):
        try:
            name = os.ttyname(fd)
        except Exception:
            continue
        return re.fullmatch(r"/dev/tty([1-9]|[1-5][0-9]|6[0-3])", name) is not None
    return False


def _load_vtrgb(path="/etc/vtrgb"):
    """setvtrgb's format: three lines of 16 comma-separated values, R then G then B."""
    with open(path) as fh:
        rows = [[int(v) for v in line.split(",")] for line in fh if line.strip()]
    if len(rows) != 3 or any(len(r) != 16 for r in rows):
        raise ValueError(f"{path}: expected 3 rows of 16")
    return [(rows[0][i], rows[1][i], rows[2][i]) for i in range(16)]


def _apply() -> None:
    if not _on_linux_vt():
        return
    colours = _load_vtrgb()
    from rich.palette import Palette
    import rich.color
    rich.color.STANDARD_PALETTE = Palette(colours)


try:
    _apply()
except Exception:
    pass
