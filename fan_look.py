"""How gpu-fan-status' live view looks: binsider's boxes (github.com/orhun/binsider) in the author's dashboard
colours - the palette, the big font8x8 numbers, the braille line graphs, the bars and the pieces of text the screen
draws.  Nothing here reads a GPU or a fan: fan_dash.py does that.

The palette and the big-digit trick come from the author's own setup screen (MIT), so a terminal running
both looks like one thing.
"""
from __future__ import annotations

from rich.text import Text
from textual.containers import Vertical
from textual.theme import Theme
from textual.widgets import Static

# The dark dashboard theme these colours come from; EDGE: the outer boxes, a step brighter than LINE
BG, SURFACE3, LINE, EDGE = "#090a14", "#1e2240", "#252a4a", "#3d4373"
INK, INK_SOFT, MUTED, FAINT = "#eceefe", "#c3c7e6", "#8a8fb4", "#555b88"
ACCENT, ACCENT2, ACCENT_TEXT, INFO_TEXT = "#8b7cff", "#38bdf8", "#b4aaff", "#7dd3fc"
OK, OK_TEXT, WARN, DANGER, TINT = "#34d399", "#6ee7b7", "#fbbf24", "#fb7185", "#1d1b37"
THEME = Theme(name="maya", primary=ACCENT, secondary=ACCENT2, accent=ACCENT, foreground=INK, background=BG,
              surface="#11132a", panel="#171a34", success=OK, warning=WARN, error=DANGER, dark=True)
SEVERITY = {"ok": OK, "warn": WARN, "bad": DANGER, "dim": FAINT, "none": MUTED}
# a line per GPU, taken in turn (the dashboard's brand colours first)
SERIES = (ACCENT2, ACCENT, OK, WARN, INFO_TEXT, ACCENT_TEXT)

# font8x8's digits (public domain: the IBM PC BIOS font), 2 x 2 pixels to a character cell, as the dashboard
DIGITS = {"0": (0x3E, 0x63, 0x73, 0x7B, 0x6F, 0x67, 0x3E, 0x00), "1": (0x0C, 0x0E, 0x0C, 0x0C, 0x0C, 0x0C, 0x3F, 0x00),
          "2": (0x1E, 0x33, 0x30, 0x1C, 0x06, 0x33, 0x3F, 0x00), "3": (0x1E, 0x33, 0x30, 0x1C, 0x30, 0x33, 0x1E, 0x00),
          "4": (0x38, 0x3C, 0x36, 0x33, 0x7F, 0x30, 0x78, 0x00), "5": (0x3F, 0x03, 0x1F, 0x30, 0x30, 0x33, 0x1E, 0x00),
          "6": (0x1C, 0x06, 0x03, 0x1F, 0x33, 0x33, 0x1E, 0x00), "7": (0x3F, 0x33, 0x30, 0x18, 0x0C, 0x0C, 0x0C, 0x00),
          "8": (0x1E, 0x33, 0x33, 0x1E, 0x33, 0x33, 0x1E, 0x00), "9": (0x1E, 0x33, 0x33, 0x3E, 0x30, 0x18, 0x0E, 0x00),
          ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C, 0x00), "-": (0x00, 0x00, 0x00, 0x3F, 0x00, 0x00, 0x00, 0x00),
          "%": (0x00, 0x63, 0x33, 0x18, 0x0C, 0x66, 0x63, 0x00), "?": (0x1E, 0x33, 0x30, 0x18, 0x0C, 0x00, 0x0C, 0x00)}
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"
BLOCKS = "▁▂▃▄▅▆▇█"
# braille: a cell is 2 x 4 dots, so a line graph gets twice the columns and four times the rows.  [x][y] -> its bit.
BRAILLE = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def mix(a: str, b: str, t: float) -> str:
    """The colour `t` of the way from `a` to `b`."""
    pa, pb = ([int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def gradient(s: str, style: str = "") -> Text:
    """`s` in the dashboard's brand gradient (violet to sky), column by column."""
    return Text.assemble(*((ch, f"{style} {mix(ACCENT, ACCENT2, i / max(1, len(s) - 1))}") for i, ch in enumerate(s)))


def big(s: str) -> list[str]:
    """`s` (digits, ".", "-", "%", "?") in font8x8, 2 x 2 pixels to a character; a "." keeps only its own columns."""
    rows = [""] * 4
    for c in s:
        px = [[DIGITS.get(c, DIGITS["?"])[r] >> b & 1 for b in range(8)] for r in range(8)]
        cells = range(1, 2) if c == "." else range(4)
        for i, r in enumerate(range(0, 8, 2)):
            rows[i] += "".join(QUADRANTS[px[r][2 * x] | px[r][2 * x + 1] << 1 | px[r + 1][2 * x] << 2 |
                                         px[r + 1][2 * x + 1] << 3] for x in cells)
    return rows


def huge(value: str, colour: str, unit: str = "", caption: Text | None = None, width: int = 0) -> Text:
    """A headline number: `value` in big digits, `unit` on its baseline, `caption` under it.  With a `width` the
    whole block is centred in it - every row by the same amount, so the digits stay square."""
    rows = [Text(r, f"bold {colour}") for r in big(value)]
    if unit:
        rows[3].append_text(Text(" " + unit, MUTED))
    pad = " " * max(0, (width - max(r.cell_len for r in rows)) // 2)
    if pad:
        rows = [Text.assemble((pad, ""), r) for r in rows]
    return Text("\n").join(rows + ([caption] if caption is not None else []))


def bar(frac: float, width: int = 14, colour: str | None = None) -> Text:
    """A meter: the brand gradient, or one colour when the value has a severity of its own."""
    done = round(min(max(frac, 0.0), 1.0) * width)
    full = Text("━" * done, colour) if colour else gradient("━" * width)[:done]
    return Text.assemble(full, ("━" * (width - done), SURFACE3))


def spark(values, width: int, lo: float = 0.0, hi: float | None = None, colour: str | None = None) -> Text:
    """A sparkline, one character a column, the newest on the right; nothing to draw: a faint baseline."""
    v = _columns(values, width, max)
    top = hi if hi is not None else max([x for x in v if x is not None] or [1.0])
    t = Text()
    for i, x in enumerate(v):
        if x is None:
            t.append("─", FAINT)
            continue
        f = (x - lo) / (top - lo) if top > lo else 0.0
        t.append(BLOCKS[min(len(BLOCKS) - 1, max(0, round(f * (len(BLOCKS) - 1))))],
                 colour or mix(ACCENT, ACCENT2, i / max(1, width - 1)))
    return t


def title(text: str, style: str = f"bold {INK}") -> Text:
    """A box title the way binsider sets it into the border: │text│."""
    return Text.assemble(("│", EDGE), (text, style), ("│", EDGE))


def hints(*keys) -> Text:
    """The keys for the bottom border: [Key→ what it does] ..."""
    t = Text()
    for i, (key, what) in enumerate(keys):
        t.append_text(Text.assemble((" [" if i else "[", FAINT), (key, INFO_TEXT), ("→ ", FAINT), (what, INK),
                                    ("]", FAINT)))
    return t


def dot(text: str, sev: str, bold: bool = True) -> Text:
    """● text, in the severity's colour."""
    c = SEVERITY.get(sev, MUTED)
    return Text.assemble(("● ", c), (text, f"bold {c}" if bold else c))


def card(rows: list) -> Text:
    """The card's lines: Label: value (binsider's info box)."""
    return Text("\n").join(Text.assemble((f"{k}: ", ACCENT_TEXT), v) for k, v in rows)


def meter(label: str, frac: float, value: Text, width: int = 12, colour: str | None = None, pad: int = 6) -> Text:
    """Label ━━━━━━━░░░░░ value - a row of a GPU's card."""
    return Text.assemble((f"{label:<{pad}}", MUTED), bar(frac, width, colour), " ", value)


def pad(t: Text, width: int) -> Text:
    """`t` filled out to `width` cells, so what follows it lines up with the row above."""
    return Text.assemble(t, (" " * max(1, width - t.cell_len), "")) if t.cell_len < width else t


def clock(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s" if s < 3600 else f"{s // 3600}h {s % 3600 // 60:02d}m"


def span(secs: int) -> str:
    return f"{secs}s" if secs < 60 else f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h"


# ---------------------------------------------------------------------------------------------- the line graphs
def _columns(values, cols: int, agg=max) -> list:
    """`values` (oldest first, None where it could not be read) over `cols` columns, the newest on the right.
    Fewer samples than columns: the left stays empty.  More: each column is `agg` of the samples that fall in it."""
    v = list(values or [])
    if not v:
        return [None] * cols
    if len(v) <= cols:
        return [None] * (cols - len(v)) + v
    step = len(v) / cols
    out = []
    for i in range(cols):
        chunk = [x for x in v[int(i * step):max(int(i * step) + 1, int((i + 1) * step))] if x is not None]
        out.append(agg(chunk) if chunk else None)
    return out


def _line(ys: list, ph: int) -> dict:
    """Pixel rows (one a pixel column, None: a gap) -> {(cell x, cell y): dot bits}, consecutive points joined."""
    dots: dict = {}
    prev = None
    for x, y in enumerate(ys):
        if y is None:
            prev = None
            continue
        y = min(ph - 1, max(0, y))
        for yy in range(min(prev, y), max(prev, y) + 1) if prev is not None else (y,):
            dots[(x // 2, yy // 4)] = dots.get((x // 2, yy // 4), 0) | BRAILLE[x % 2][yy % 4]
        prev = y
    return dots


def _render(layers: list, cols: int, height: int, gutter: int, labels: dict, grid: dict) -> list[Text]:
    """The graph itself.  `layers`: (dots, colour) drawn together - a cell takes the colour of whichever layer put
    the most dots in it.  `labels`: cell row -> the y axis label.  `grid`: pixel row -> colour of a dotted line,
    drawn only where no data is (so a limit line never hides the curve)."""
    rows = []
    for cy in range(height):
        lab = labels.get(cy, "")
        t = Text.assemble((f"{lab:>{gutter - 1}}", MUTED if lab else LINE), ("┤" if lab else "│", LINE))
        here = {py - cy * 4: c for py, c in grid.items() if cy * 4 <= py < cy * 4 + 4}
        for cx in range(cols):
            bits, best, colour = 0, 0, None
            for dots, c in layers:
                b = dots.get((cx, cy), 0)
                if b:
                    bits |= b
                    if (n := b.bit_count()) > best:
                        best, colour = n, c
            if bits:
                t.append(chr(0x2800 | bits), colour)
            elif here:
                yy, c = next(iter(here.items()))
                t.append(chr(0x2800 | BRAILLE[0][yy]), c)       # the left dot only: a dotted line
            else:
                t.append(" ")
        rows.append(t)
    return rows


def _bounds(values, lo, hi, pad: float = 0.08) -> tuple[float, float]:
    v = [x for x in values if x is not None]
    if not v:
        return (lo if lo is not None else 0.0), (hi if hi is not None else 1.0)
    bot = lo if lo is not None else min(v)
    top = hi if hi is not None else max(v)
    if lo is None or hi is None:                        # a little air so the line is not on the frame
        room = max((top - bot) * pad, 0.5)
        bot, top = (bot if lo is not None else bot - room), (top if hi is not None else top + room)
    return bot, (top if top > bot else bot + 1.0)


def graph(series: list, width: int, height: int, lo=None, hi=None, limits=(), fmt="{:.0f}", gutter: int = 5,
          agg=max) -> list[Text]:
    """A line graph of time series over the same window.  `series`: [{"values": [...], "colour": "#..."}], oldest
    first.  `lo` / `hi` fix the scale (else it follows the data).  `limits`: [(value, colour)] as dotted lines."""
    cols = max(8, width - gutter)
    ph = height * 4
    bot, top = _bounds([x for s in series for x in s["values"]] + [v for v, _ in limits], lo, hi)
    def row_of(v):                                      # a value -> its pixel row (0 = the top)
        return round((1 - (v - bot) / (top - bot)) * (ph - 1))
    layers = [(_line([None if v is None else row_of(v) for v in _columns(s["values"], cols * 2, agg)], ph),
               s["colour"]) for s in series]
    grid = {row_of(v): c for v, c in limits if bot <= v <= top}
    marks = [bot + (top - bot) * f for f in (1.0, 0.5, 0.0)]            # the axis: top, middle, bottom
    labels = {}
    for v in marks:
        labels.setdefault(min(height - 1, max(0, row_of(v) // 4)), fmt.format(v)[:gutter - 1])
    for v, c in limits:                                 # a limit's own value, where it does not crowd the axis
        if bot <= v <= top:
            labels.setdefault(min(height - 1, max(0, row_of(v) // 4)), fmt.format(v)[:gutter - 1])
    return _render(layers, cols, height, gutter, labels, grid)


def xaxis(width: int, left: str, right: str, gutter: int = 5) -> Text:
    """The line under a graph: └────────── left ... right."""
    cols = max(8, width - gutter)
    mid = f"{left}{' ' * max(1, cols - len(left) - len(right) - 2)}{right}"
    return Text.assemble((" " * (gutter - 1) + "└", LINE), (mid[:cols].center(cols), FAINT))


def legend(series: list) -> Text:
    t = Text()
    for i, s in enumerate(series):
        t.append_text(Text.assemble(("  " if i else "", ""), ("━━ ", s["colour"]), (s["label"], MUTED)))
    return t


def curve_graph(curve: list, point, width: int, height: int, gutter: int = 5, floor: float = 20.0,
                ceiling: float = 100.0) -> list[Text]:
    """The configured fan curve (temperature -> duty) with "you are here" on it: the curve as a line, the live
    point as a cross of dotted lines meeting at a bright marker.  The line is clamped to `floor`..`ceiling`
    (the BMC's 20..100 %, as gpu-fan-control clamps it), so it is the duty the service would really drive."""
    cols = max(8, width - gutter)
    ph, pw = height * 4, cols * 2
    xs = [t for t, _ in curve]
    x0, x1 = min(xs) - 5, max(xs) + 7                   # a little either side of the configured points
    if point:
        x0, x1 = min(x0, point[0] - 2), max(x1, point[0] + 2)
    def duty_at(t):
        if t <= curve[0][0]:
            d = curve[0][1]
        elif t >= curve[-1][0]:
            d = curve[-1][1]
        else:
            d = curve[-1][1]
            for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
                if t <= t1:
                    d = d0 + (d1 - d0) * (t - t0) / (t1 - t0)
                    break
        return min(ceiling, max(floor, d))
    def row_of(d):
        return round((1 - min(max(d, 0), 100) / 100) * (ph - 1))
    def col_of(t):
        return round((t - x0) / (x1 - x0) * (pw - 1))
    ys = [row_of(duty_at(x0 + (x1 - x0) * x / max(1, pw - 1))) for x in range(pw)]
    layers = [(_line(ys, ph), ACCENT)]
    grid = {}
    if point:
        temp, duty = point
        px, py = min(pw - 1, max(0, col_of(temp))), row_of(duty)
        grid = {py: OK_TEXT}                            # the duty it asks for, across the graph
        here = {(px // 2, py // 4): BRAILLE[px % 2][py % 4]}
        for dy in (-2, -1, 1, 2):                       # a short vertical whisker through the point
            if 0 <= py + dy < ph:
                here[(px // 2, (py + dy) // 4)] = here.get((px // 2, (py + dy) // 4), 0) | BRAILLE[px % 2][(py + dy) % 4]
        layers.append((here, OK))
    labels = {0: "100%", height // 2: "50%", height - 1: "0%"}
    rows = _render(layers, cols, height, gutter, labels, grid)
    return rows + [xaxis(width, f"{x0:.0f}C", f"{x1:.0f}C", gutter)]


# ------------------------------------------------------------------------------------------------ the widgets
class Panel(Vertical):
    """A box with a title in its border and lines that are drawn again only when they change."""
    DEFAULT_CSS = """
    Panel { height: auto; border: solid $maya-line; padding: 0 1; border-title-align: center; }
    Panel > Static { height: auto; text-wrap: nowrap; text-overflow: ellipsis; }
    """
    LINES = 8

    def __init__(self, heading: str = "", lines: int | None = None, **kw):
        super().__init__(**kw)
        self.heading, self.lines = heading, lines or self.LINES

    def compose(self):
        self.shown: list = [None] * self.lines
        for _ in range(self.lines):
            row = Static()
            row.display = False
            row.auto_links = False                      # (no links: the mouse over a line would draw it again)
            yield row

    def on_mount(self) -> None:
        if self.heading:
            self.border_title = title(self.heading)

    def show(self, text: Text | list) -> None:
        got = list(text.split("\n")) if isinstance(text, Text) else list(text)
        got = (got + [None] * self.lines)[:self.lines]
        for k, (row, line) in enumerate(zip(self.query(Static), got)):
            if line != self.shown[k]:
                self.shown[k] = line
                row.display = line is not None
                row.update(line if line is not None else "")


# the screen's layout: the top bar, the headline numbers, the graphs, the GPU cards, the status line
DASH_CSS = """
    Screen { background: $background; }
    #top { height: 3; border: solid $maya-edge; border-title-align: center; padding: 0 1; }
    #when { width: 1fr; text-wrap: nowrap; }
    #where { width: auto; padding-left: 2; color: $maya-muted; text-style: italic; text-wrap: nowrap; }
    .narrow #where { display: none; }
    #body { height: 1fr; border: solid $maya-edge; border-subtitle-align: center; padding: 0 1;
            scrollbar-size-vertical: 1; scrollbar-color: $maya-edge; scrollbar-background: $background; }
    #hero { height: 7; margin-bottom: 1; }
    #hero Panel { width: 1fr; margin-right: 1; }
    #hero Panel:last-of-type { margin-right: 0; }
    .narrow #hero { height: auto; }
    #graphs { height: auto; margin-bottom: 1; }
    #graphs Panel { width: 1fr; margin-right: 1; }
    #graphs Panel:last-of-type { margin-right: 0; }
    #lower { height: auto; }                    /* the cards' own margin-bottom spaces #status */
    #cards { width: 2fr; height: auto; }
    #cards Panel { margin-bottom: 1; }
    #curve { width: 1fr; margin-left: 1; }
    .narrow #curve { display: none; }
    #status { height: auto; border: solid $maya-line; padding: 0 1; border-title-align: center; }
    .alarm #body { border: solid $error; }
"""
