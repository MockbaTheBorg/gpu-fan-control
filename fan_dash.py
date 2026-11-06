"""gpu-fan-status on a screen of its own: the GPU temperatures, the fan and the curve as they happen.

Read-only, like the plain report - it runs the same reading functions from gpu-fan-status (handed in as `st`), in a
thread of its own, and keeps what it read so the graphs have a past.  Nothing here writes a fan setting.

The screen is laid out like binsider (github.com/orhun/binsider) in the dashboard colours of fan_look.py:
the headline numbers in big font8x8 digits, braille line graphs of the last minutes, a card for each GPU, and the
configured fan curve with "you are here" on it.
"""
from __future__ import annotations

import os
import queue
import socket
import threading
import time
from collections import deque

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from fan_look import (ACCENT, ACCENT2, ACCENT_TEXT, DANGER, DASH_CSS, EDGE, FAINT, INK, INK_SOFT, LINE, MUTED, OK,
                      OK_TEXT, SERIES, SEVERITY, THEME, TINT, WARN, Panel, clock, curve_graph, dot, graph, gradient,
                      hints, huge, legend, meter, pad, spark, span, title, xaxis)

WINDOWS = (240, 600, 3600)                 # the graphs' window: 4 minutes, 10 minutes, an hour
GRAPH_ROWS = 9                             # braille rows in a line graph (x 4 pixels)
CURVE_ROWS = GRAPH_ROWS + 1                # the fan curve's: one taller, to fill the GPU cards' column
HOUR = 3600
# the right-hand graph, in the order "g" walks through them
VIEWS = ("duty", "rpm", "power", "util")
VIEW_TITLE = {"duty": "Fan duty", "rpm": "Fan RPM", "power": "Power draw", "util": "GPU utilization"}


# ---------------------------------------------------------------------------------------------- the readings
class Sampler(threading.Thread):
    """Reads the GPUs every `interval` seconds and the fan (IPMI, root only) a little less often, and puts each
    reading on a queue for the screen.  A reading that fails is reported in the snapshot, never raised."""

    def __init__(self, st, interval: float, fan: int):
        super().__init__(daemon=True)
        self.st, self.interval, self.fan = st, interval, fan
        self.q: queue.Queue = queue.Queue()
        self.done = threading.Event()
        self.root = os.geteuid() == 0
        self.slow = {"mode": None, "duty": None, "duty_src": "", "rpms": {}, "svc": "?", "guard": None, "err": None}

    def stop(self) -> None:
        self.done.set()

    def run(self) -> None:
        every = lambda secs: max(1, round(secs / max(0.5, self.interval)))
        fan_every, svc_every = every(4), every(5)
        log_every = every(20)                           # journalctl, when there is no root: expensive
        tick = 0
        while not self.done.is_set():
            if tick % (fan_every if self.root else log_every) == 0:
                self.read_fan()
            if tick % svc_every == 0:
                self.read_services()
            self.q.put(self.read_gpus())
            tick += 1
            self.done.wait(self.interval)

    def read_fan(self) -> None:
        """The fan half of a reading: whatever the configured backend can tell us.  Without root
        the BMC backends can't be read at all, so the duty comes from the service's own log."""
        st = self.st
        if not self.root:
            duty = st.last_logged_duty(self.fan)
            self.slow.update(duty=int(duty.split("%")[0]) if duty else None,
                             duty_src="from the service log" if duty else "",
                             mode=None, mode_text="needs sudo", mode_ok=False)
            return
        snap = st.fan_snapshot()
        self.slow.update(mode=snap["mode"], mode_text=snap["mode_text"], mode_ok=snap["mode_ok"],
                         duty=snap["duty"], duty_src=snap["duty_src"], err=snap["err"],
                         rpms={os.path.basename(k): ("no reading" if v is None else f"{v} RPM")
                               for k, v in snap["rpms"].items()})

    def read_services(self) -> None:
        self.slow["svc"] = self.st.service_state("gpu-fan-control")
        self.slow["guard"] = self.st.guard_data()

    def read_gpus(self) -> dict:
        st = self.st
        snap = {"t": time.time(), "gpus": [], "err": None, "hottest": None, "power": 0.0, "unreadable": False,
                "curve": None, "points": st.fan_curve(), **self.slow}
        try:
            rows = st.gpus()
        except Exception as e:
            snap["err"] = f"nvidia-smi unavailable ({e})"
            return snap
        for g in rows:
            temp, power = st.num(g["temperature.gpu"]), st.num(g["power.draw"])
            limit, util = st.num(g["power.limit"]), st.num(g["utilization.gpu"])
            sm, sm_max = st.num(g["clocks.sm"]), st.num(g["clocks.max.sm"])
            text, sev = st.throttle(g["clocks_event_reasons.active"])
            snap["gpus"].append({"index": g["index"], "name": g["name"], "temp": temp, "power": power,
                                 "limit": limit, "util": util, "sm": sm, "sm_max": sm_max,
                                 "throttle": text, "sev": sev})
            snap["power"] += power or 0.0
            if temp is None:
                snap["unreadable"] = True
            elif snap["hottest"] is None or temp > snap["hottest"][0]:
                snap["hottest"] = (int(temp), g["index"])
        if snap["unreadable"]:
            snap["curve"] = st.curve_duty(200)          # what the service would do: failsafe
        elif snap["hottest"]:
            snap["curve"] = st.curve_duty(snap["hottest"][0])
        return snap


def bounds(values, span_min: float, pad: float = 2.0, floor=None, ceiling=None):
    """A graph's scale: the data with a little air, never narrower than `span_min` (so a steady temperature does
    not look like a mountain range)."""
    v = [x for x in values if x is not None]
    if not v:
        return (floor if floor is not None else 0.0), (floor if floor is not None else 0.0) + span_min
    lo, hi = min(v) - pad, max(v) + pad
    if hi - lo < span_min:
        mid = (lo + hi) / 2
        lo, hi = mid - span_min / 2, mid + span_min / 2
    if floor is not None:
        lo = max(floor, lo)
    if ceiling is not None:
        hi = min(ceiling, hi)
    return lo, max(hi, lo + span_min / 2)


# ------------------------------------------------------------------------------------------------ the screen
class FanApp(App):
    CSS = DASH_CSS
    BINDINGS = [Binding("q", "quit", show=False, priority=True),
                Binding("ctrl+c", "quit", show=False, priority=True),
                Binding("space", "pause", show=False),
                Binding("g", "view(1)", show=False),
                Binding("1", "window(0)", show=False),
                Binding("2", "window(1)", show=False),
                Binding("3", "window(2)", show=False)]
    ENABLE_COMMAND_PALETTE = False
    TITLE = "gpu-fan-status"            # on_mount puts the version in the top panel's title

    def __init__(self, st, interval: float):
        super().__init__()
        self.st, self.interval = st, interval
        self.fan = st.fan_label()
        self.curve = st.fan_curve()
        self.sampler = Sampler(st, interval, self.fan)
        self.hist: dict = {}                            # what to draw: "temp0", "duty", "rpm", "power", "util0" ...
        self.keep = int(HOUR / max(0.5, interval)) + 8  # an hour of samples
        self.snap: dict | None = None
        self.win, self.view, self.paused = 0, 0, False
        self.started = time.monotonic()
        self.samples, self.dirty = 0, True

    def get_css_variables(self) -> dict:
        return {**super().get_css_variables(), "maya-edge": EDGE, "maya-line": LINE, "maya-tint": TINT,
                "maya-muted": MUTED, "maya-accent-text": ACCENT_TEXT}

    # ------------------------------------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(id="when")
            yield Static(f"{socket.gethostname()} · {self.fan}", id="where")
        with VerticalScroll(id="body"):
            with Horizontal(id="hero"):
                yield Panel("Hottest GPU", 5, id="t-temp")
                yield Panel(f"{self.fan} duty", 5, id="t-duty")
                yield Panel("Fan speed", 5, id="t-rpm")
                yield Panel("Board power", 5, id="t-power")
            with Horizontal(id="graphs"):
                yield Panel("Temperature", GRAPH_ROWS + 2, id="g-temp")
                yield Panel("Fan duty", GRAPH_ROWS + 2, id="g-right")
            with Horizontal(id="lower"):
                yield Vertical(id="cards")
                yield Panel("Fan curve", CURVE_ROWS + 4, id="curve")
            yield Panel("", 2, id="status")

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "maya"
        self.query_one("#top").border_title = Text.assemble(
            ("│", EDGE), gradient("gpu fan status", "bold"),
            (f" v{getattr(self.st, 'VERSION', '?')}", ACCENT_TEXT), (" · read-only", MUTED), ("│", EDGE))
        self.query_one("#body").border_subtitle = hints(("q", "Quit"), ("Space", "Pause"), ("g", "Graph"),
                                                        ("1/2/3", "/".join(span(w) for w in WINDOWS)))
        self.query_one("#status").border_title = title("Fan control")
        self.cards: dict = {}
        self.sampler.start()
        self.set_interval(0.25, self.pump)
        self.set_interval(1.0, self.draw_top)

    def on_resize(self, event: events.Resize) -> None:
        self.screen.set_class(event.size.width < 104, "narrow")
        self.dirty = True

    # ------------------------------------------------------------------------------------------- the ticks
    def pump(self) -> None:
        """Whatever the sampler read since the last tick (the newest wins), then draw if anything changed."""
        fresh = None
        while True:
            try:
                fresh = self.sampler.q.get_nowait()
            except queue.Empty:
                break
        if fresh is not None and not self.paused:
            self.snap, self.dirty = fresh, True
            self.samples += 1
            self.remember(fresh)
        if self.dirty and self.snap is not None:
            self.dirty = False
            self.draw()

    def remember(self, s: dict) -> None:
        def put(key, value):
            self.hist.setdefault(key, deque(maxlen=self.keep)).append(value)
        for g in s["gpus"]:
            put(f"temp{g['index']}", g["temp"])
            put(f"power{g['index']}", g["power"])
            put(f"util{g['index']}", g["util"])
        put("duty", s.get("duty"))
        rpms = [int(v.split()[0]) for v in (s.get("rpms") or {}).values() if v[:1].isdigit()]
        put("rpm", max(rpms) if rpms else None)
        put("power", s["power"])
        put("curve", s.get("curve"))

    def recent(self, key: str) -> list:
        """The chosen window of a series, oldest first."""
        want = max(4, int(WINDOWS[self.win] / max(0.5, self.interval)))
        return list(self.hist.get(key, ()))[-want:]

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.dirty = True

    def action_window(self, n: int) -> None:
        self.win = n
        self.dirty = True

    def action_view(self, step: int) -> None:
        self.view = (self.view + step) % len(VIEWS)
        self.dirty = True

    def on_unmount(self) -> None:
        self.sampler.stop()

    # ------------------------------------------------------------------------------------------- drawing
    def draw_top(self) -> None:
        s = self.snap
        when = Text.assemble((time.strftime("%Y-%m-%d %H:%M:%S"), INK_SOFT),
                             (f"  up {clock(time.monotonic() - self.started)}", MUTED),
                             (f" · {self.samples} samples every {self.interval:g}s", FAINT))
        if self.paused:
            when.append_text(Text("  ⏸ PAUSED", f"bold {WARN}"))
        if s and s["err"]:
            when.append_text(Text(f"  {s['err']}", f"bold {DANGER}"))
        self.query_one("#when").update(when)

    def alarming(self, s: dict) -> bool:
        guard = s.get("guard") or {}
        return bool(s["unreadable"] or guard.get("stage") not in (None, "normal") or s["svc"] != "active"
                    or any(g["sev"] == "bad" for g in s["gpus"])
                    or any(g["temp"] is not None and g["temp"] >= self.st.TEMP_HOT for g in s["gpus"]))

    def draw(self) -> None:
        s = self.snap
        self.screen.set_class(self.alarming(s), "alarm")
        self.draw_top()
        self.draw_hero(s)
        self.draw_graphs()
        self.draw_cards(s)
        self.draw_curve(s)
        self.draw_status(s)

    def draw_hero(self, s: dict) -> None:
        st = self.st
        # the hottest GPU, and how far it is from the limits the GPU itself acts on
        wide = max(16, self.query_one("#t-temp").content_size.width or 24)
        if s["hottest"]:
            temp, which = s["hottest"]
            colour = SEVERITY[st.level(temp, st.TEMP_WARN, st.TEMP_HOT)]
            caption = Text.assemble((f"GPU{which}", ACCENT_TEXT), (f" · {83 - temp:+d} C to max operating", MUTED)
                                    if temp < 83 else (" · OVER max operating", f"bold {DANGER}"))
            self.query_one("#t-temp").show(huge(str(temp), colour, "°C", caption, wide))
        else:
            self.query_one("#t-temp").show(huge("?", DANGER, "°C", Text("no temperature", f"bold {DANGER}"),
                                                wide))

        duty, curve = s.get("duty"), s.get("curve")
        colour = SEVERITY[st.level(duty, st.DUTY_WARN, st.DUTY_HIGH)] if duty is not None else MUTED
        bits = [Text(s.get("mode_text", "?").split(" (")[0], OK_TEXT if s.get("mode_ok") else WARN)
                if s.get("mode") is not None else Text("mode needs sudo", FAINT)]
        if curve is not None:
            bits.append(Text.assemble(("curve asks ", MUTED), (f"{curve}%", INK)))
        self.query_one("#t-duty").show(huge(str(duty) if duty is not None else "?", colour, "%",
                                            Text(" · ", MUTED).join(bits), wide))

        rpm = next((v for v in (s.get("rpms") or {}).values() if v[:1].isdigit()), None)
        tile = self.query_one("#t-rpm")
        if rpm:
            n = int(rpm.split()[0])
            rotors = len(s.get("rpms") or {})
            tile.border_title = title("Fan speed")
            tile.show(huge(str(n), OK if n else DANGER, "rpm",
                           Text.assemble((self.fan, ACCENT_TEXT),
                                         (f" · {rotors} tach" + ("s" if rotors > 1 else ""), MUTED)), wide))
        else:                                           # no BMC without root: the window's peak instead
            peak = [x for k in self.hist if k.startswith("temp") for x in self.recent(k) if x is not None]
            tile.border_title = title(f"Peak · {span(WINDOWS[self.win])}")
            top = max(peak) if peak else None
            tile.show(huge(f"{top:.0f}" if top is not None else "-",
                           SEVERITY[st.level(top, st.TEMP_WARN, st.TEMP_HOT)] if top is not None else MUTED, "°C",
                           Text("sudo shows the fan's RPM here", FAINT), wide))

        cap = s["gpus"][0]["limit"] if s["gpus"] and s["gpus"][0]["limit"] else None
        caption = Text.assemble((f"{len(s['gpus'])} GPU" + ("s" if len(s["gpus"]) != 1 else ""), ACCENT_TEXT),
                                (f" · {cap:.0f} W cap each", MUTED) if cap else ("", ""))
        self.query_one("#t-power").show(huge(f"{s['power']:.0f}", ACCENT2, "W", caption, wide))

    def draw_graphs(self) -> None:
        st = self.st
        win = span(WINDOWS[self.win])
        keys = sorted(k for k in self.hist if k.startswith("temp"))
        series = [{"values": self.recent(k), "colour": SERIES[i % len(SERIES)], "label": f"GPU{k[4:]}"}
                  for i, k in enumerate(keys)]
        panel = self.query_one("#g-temp")
        panel.border_title = title(f"Temperature · {win}")
        width = max(24, panel.content_size.width or 40)
        limits = [(83.0, WARN), (88.0, DANGER)]
        lo, hi = bounds([x for s in series for x in s["values"]], span_min=16, floor=20.0, ceiling=95.0)
        if hi > 83:                                     # the limit lines are in view: keep room for them
            lo = min(lo, 81.0)
        rows = graph(series, width, GRAPH_ROWS, lo=lo, hi=hi, limits=[l for l in limits if lo <= l[0] <= hi],
                     fmt="{:.0f}")
        note = legend(series)
        if hi > 83:
            note.append_text(Text.assemble(("  ·· ", WARN), ("83 max op", MUTED)))
        panel.show(rows + [xaxis(width, f"-{win}", "now"), note])

        view = VIEWS[self.view]
        panel = self.query_one("#g-right")
        width = max(24, panel.content_size.width or 40)
        if view == "duty":
            series = [{"values": self.recent("duty"), "colour": ACCENT, "label": f"{self.fan} duty %"},
                      {"values": self.recent("curve"), "colour": FAINT, "label": "curve asks"}]
            rows = graph(series, width, GRAPH_ROWS, lo=0, hi=100,
                         limits=[(float(st.DUTY_WARN), WARN), (float(st.DUTY_HIGH), DANGER)], fmt="{:.0f}")
        elif view == "rpm":
            series = [{"values": self.recent("rpm"), "colour": OK, "label": f"{self.fan} RPM"}]
            lo, hi = bounds(series[0]["values"], span_min=800, pad=100, floor=0.0)
            rows = graph(series, width, GRAPH_ROWS, lo=lo, hi=hi, fmt="{:.0f}")
        elif view == "power":
            keys = sorted(k for k in self.hist if k.startswith("power") and k != "power")
            series = [{"values": self.recent(k), "colour": SERIES[i % len(SERIES)], "label": f"GPU{k[5:]}"}
                      for i, k in enumerate(keys)]
            series.append({"values": self.recent("power"), "colour": ACCENT_TEXT, "label": "total"})
            lo, hi = bounds([x for s in series for x in s["values"]], span_min=60, pad=10, floor=0.0)
            rows = graph(series, width, GRAPH_ROWS, lo=lo, hi=hi, fmt="{:.0f}")
        else:
            keys = sorted(k for k in self.hist if k.startswith("util"))
            series = [{"values": self.recent(k), "colour": SERIES[i % len(SERIES)], "label": f"GPU{k[4:]}"}
                      for i, k in enumerate(keys)]
            rows = graph(series, width, GRAPH_ROWS, lo=0, hi=100, fmt="{:.0f}")
        unit = {"duty": "%", "rpm": "RPM", "power": "W", "util": "%"}[view]
        panel.border_title = title(f"{VIEW_TITLE[view]} · {win} · {unit}")
        panel.show(rows + [xaxis(width, f"-{win}", "now"), legend(series)])

    def draw_cards(self, s: dict) -> None:
        st = self.st
        box = self.query_one("#cards")
        for g in s["gpus"]:
            if g["index"] not in self.cards:
                panel = Panel(f"GPU{g['index']} · {g['name']}", 5, id=f"gpu{g['index']}")
                self.cards[g["index"]] = panel
                box.mount(panel)
                continue                                # (its lines on the next tick, once it is on the screen)
            panel = self.cards[g["index"]]
            width = max(30, panel.content_size.width or 50)
            col = 32                                    # bar + value, fixed so the sparklines line up
            room = max(4, width - col - 8)
            temp, lines = g["temp"], []
            if temp is None:
                lines.append(Text("temperature unreadable - the service is in failsafe", f"bold {DANGER}"))
            else:
                colour = SEVERITY[st.level(temp, st.TEMP_WARN, st.TEMP_HOT)]
                lines.append(Text.assemble(
                    pad(meter("Temp", (temp - 25) / 65, Text(f"{temp:.0f} °C", f"bold {colour}"), 12, colour), col),
                    spark(self.recent(f"temp{g['index']}"), room, colour=colour)))
            if g["power"] is not None and g["limit"]:
                hot = g["power"] >= st.POWER_WARN * g["limit"]
                lines.append(Text.assemble(
                    pad(meter("Power", g["power"] / g["limit"], Text.assemble(
                        (f"{g['power']:.0f}", f"bold {WARN if hot else INK}"), (f" / {g['limit']:.0f} W", MUTED)),
                        12, WARN if hot else None), col),
                    spark(self.recent(f"power{g['index']}"), room, hi=g["limit"])))
            if g["util"] is not None:
                lines.append(Text.assemble(
                    pad(meter("Util", g["util"] / 100, Text(f"{g['util']:.0f} %", INK), 12), col),
                    spark(self.recent(f"util{g['index']}"), room, hi=100)))
            if g["sm"] is not None and g["sm_max"]:
                lines.append(meter("Clock", g["sm"] / g["sm_max"], Text.assemble(
                    (f"{g['sm']:.0f}", INK), (f" / {g['sm_max']:.0f} MHz", MUTED),
                    (f"  {100 * g['sm'] / g['sm_max']:.0f} %", FAINT)), 12))
            why = g["throttle"]                         # the plain report's wording, said as a state here
            why = "running free" if why == "no" else "not slowed" + why[2:] if why.startswith("no (") else \
                why[5:] if why.startswith("YES: ") else why
            lines.append(Text.assemble(("State ", MUTED), dot(why, g["sev"], g["sev"] != "ok")))
            panel.show(lines)

    def draw_curve(self, s: dict) -> None:
        panel = self.query_one("#curve")
        width = max(20, panel.content_size.width or 30)
        curve = s.get("points") or self.curve           # re-read with every sample: an edit shows up here
        if not curve:
            panel.show([Text("no curve in fan-curve.toml", WARN)])
            return
        point = None
        if s["hottest"] and s.get("curve") is not None:
            point = (float(s["hottest"][0]), float(s["curve"]))
        rows = curve_graph(curve, point, width, CURVE_ROWS)
        panel.border_title = title("Fan curve · duty vs GPU temp")
        note = Text.assemble(("━━ ", ACCENT), ("configured", MUTED))
        if point:
            note.append_text(Text.assemble(("   ● ", OK), (f"{point[0]:.0f} C → {point[1]:.0f}%", OK_TEXT)))
        live = s.get("duty")
        if live is not None and point and live != point[1]:
            note = Text("\n").join([note, Text.assemble(
                (f"fan is at {live}%", INK), (" · held while ramping down", FAINT))])
        panel.show(rows + list(note.split("\n")))

    def draw_status(self, s: dict) -> None:
        st = self.st
        svc = s["svc"]
        guard = s.get("guard") or {"svc": "?", "stage": "?"}
        stage = {"normal": ("normal", "ok"), "capped": (f"POWER CAPPED since {guard.get('since')}", "warn"),
                 "tripped": (f"TRIPPED since {guard.get('since')} - sudo gpu-thermal-guard --reset", "bad"),
                 }.get(guard.get("stage"), (str(guard.get("stage")), "warn"))
        first = Text("   ").join([
            Text.assemble(("gpu-fan-control ", MUTED), dot(svc, "ok" if svc == "active" else "bad")),
            Text.assemble(("guard ", MUTED), dot(guard["svc"], "ok" if guard["svc"] == "active" else "bad"),
                          (" · ", MUTED), dot(*stage)),
            Text.assemble((f"{self.fan} ", MUTED),
                          Text(s.get("mode_text", "needs sudo"), OK_TEXT if s.get("mode_ok") else WARN)
                          if s.get("mode") is not None else Text("mode needs sudo", FAINT))])
        bits = []
        if s.get("duty") is not None:
            bits.append(Text.assemble((f"duty {s['duty']}%", INK), (f" ({s['duty_src']})", FAINT)))
        for name, rpm in (s.get("rpms") or {}).items():
            stopped = not rpm[:1].isdigit() or rpm.split()[0] == "0"
            bits.append(Text.assemble((f"{name} ", MUTED), (rpm, f"bold {DANGER}" if stopped else INK)))
        if s["unreadable"]:
            bits.append(Text("failsafe: a GPU temperature cannot be read", f"bold {DANGER}"))
        elif s.get("curve") is not None and s["hottest"]:
            bits.append(Text.assemble(("curve target ", MUTED), (f"{s['curve']}%", INK),
                                      (f" at {s['hottest'][0]} C (GPU{s['hottest'][1]})", MUTED)))
        if s.get("err"):
            bits.append(Text(s["err"], f"bold {DANGER}"))
        if guard.get("stage") not in ("normal", None) and guard.get("reason"):
            bits.append(Text(guard["reason"], WARN))
        if not self.sampler.root:
            bits.append(Text("run with sudo for live duty, RPM and mode", FAINT))
        self.query_one("#status").show([first, Text("   ").join(bits) if bits else Text("–", MUTED)])


def run(st, interval: float) -> None:
    """Show the live view (gpu-fan-status -w).  `st`: the gpu-fan-status module, for its reading functions."""
    FanApp(st, max(0.5, interval)).run()
