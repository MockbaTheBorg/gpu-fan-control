#!/usr/bin/python3
"""Exercise the control loop against a stub backend: no hardware, no IPMI, no waiting.

Run it after changing the controller, or on a new machine to see that the logic works
there before letting it near a real fan:

    python3 tests/test_loop.py

The stub stands in for a fan: it records the duties written to it. Time and nvidia-smi are
replaced too, so a 60-second hold takes no time at all. This tests the curve, the hold, the
ramp-down, the failsafe and fault detection - not any vendor's commands, which need the
hardware (use `gpu-fan-detect --spin` for those).
"""
import importlib.machinery
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, HERE)
import fan_backend as fb                                                        # noqa: E402

spec = importlib.util.spec_from_loader(
    "ctl", importlib.machinery.SourceFileLoader("ctl", os.path.join(HERE, "gpu-fan-control")))
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)


class Fake(fb.Backend):
    """A fan that only remembers what it was told."""
    kind, min_duty, max_duty, duty_first, OPTIONS = "fake", 20, 100, True, ()
    written, rpm = [], 3000
    label = property(lambda self: "FAKE")

    def take_over(self): pass
    def release(self): Fake.written.append("released")
    def set_duty(self, duty): Fake.written.append(duty)
    def mode(self): return 1, "manual", True
    def live_duty(self): return next((d for d in reversed(Fake.written) if isinstance(d, int)), None)
    def default_tachs(self): return ["FAKE_1"]
    def rpms(self): return {"FAKE_1": Fake.rpm}


fb.BACKENDS["fake"] = Fake
CURVE = [[40, 20], [50, 30], [60, 45], [70, 70], [78, 100]]     # the shipped default


def run_loop(temps, checks, rpm=3000, **settings):
    """Feed one temperature per check through the loop; return the duties it wrote."""
    Fake.written, Fake.rpm = [], rpm
    clock, seq, n = [1000.0], list(temps), [0]
    ctl.gpu_temps = lambda: ({0: seq.pop(0)} if seq else {0: temps[-1]})
    ctl.time.monotonic = lambda: clock[0]

    def sleep(secs):
        clock[0] += secs
        n[0] += 1
        if n[0] >= checks:
            raise ctl.Stop
    ctl.time.sleep = sleep

    cfg = ctl.load_defaults()
    cfg.update({"backend": "fake", "fan": 1, "tach": [], "min_duty": None, "max_duty": None,
                "label": None, "asrock": {}, "supermicro": {}, "hwmon": {}, "command": {},
                **settings})
    conf = ctl.Config.__new__(ctl.Config)
    conf.path, conf.mtime, conf.error, conf.data = "<test>", None, None, cfg
    conf.refresh = lambda: False
    try:
        ctl.control_loop(conf, {})
    except ctl.Stop:
        pass
    return [d for d in Fake.written if isinstance(d, int)]


CASES = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


@case("the curve interpolates between its points")
def _():
    duty = lambda t: ctl.curve_duty([(float(a), float(b)) for a, b in CURVE], t)
    return ([duty(t) for t in (30, 40, 45, 55, 65, 75, 78, 95)]
            == [20, 20, 25, 38, 58, 89, 100, 100])


@case("a rising temperature raises the duty on every check")
def _():
    return run_loop([45, 55, 65, 75], 4) == [25, 38, 58, 89]


@case("a falling temperature is held for hold_secs")
def _():
    # 75 C asks 89 %, 41 C asks 20 %. hold_secs 60 at interval 5 is 12 checks of holding, and
    # a held check writes nothing, so ten checks of cold GPU still leave the duty where it was.
    return run_loop([75] + [41] * 20, 10) == [89]


@case("after the hold, the duty steps down by step_down and no faster")
def _():
    got = run_loop([75] + [41] * 30, 20)
    steps = [b - a for a, b in zip(got, got[1:])]
    return got[0] == 89 and len(steps) >= 3 and set(steps) == {-2}


@case("a dip within hold_band does not start the ramp-down")
def _():
    return run_loop([75, 73, 74, 73, 74], 5) == [89]


@case("an unreadable temperature goes to failsafe_duty")
def _():
    def boom():
        raise RuntimeError("nvidia-smi is not having a good day")
    Fake.written = []
    clock, n = [1000.0], [0]
    ctl.gpu_temps = boom
    ctl.time.monotonic = lambda: clock[0]

    def sleep(secs):
        clock[0] += secs
        n[0] += 1
        if n[0] >= 2:
            raise ctl.Stop
    ctl.time.sleep = sleep
    cfg = ctl.load_defaults()
    cfg.update(backend="fake", fan=1, tach=[], min_duty=None, max_duty=None, label=None,
               asrock={}, supermicro={}, hwmon={}, command={}, failsafe_duty=100)
    conf = ctl.Config.__new__(ctl.Config)
    conf.path, conf.mtime, conf.error, conf.data = "<test>", None, None, cfg
    conf.refresh = lambda: False
    try:
        ctl.control_loop(conf, {})
    except ctl.Stop:
        pass
    return [d for d in Fake.written if isinstance(d, int)] == [100]


@case("a GPU that disappears goes to failsafe")
def _():
    Fake.written = []
    clock, n, seen = [1000.0], [0], [{0: 50, 1: 50}, {0: 50}]
    ctl.gpu_temps = lambda: seen.pop(0) if seen else {0: 50}
    ctl.time.monotonic = lambda: clock[0]

    def sleep(secs):
        clock[0] += secs
        n[0] += 1
        if n[0] >= 2:
            raise ctl.Stop
    ctl.time.sleep = sleep
    cfg = ctl.load_defaults()
    cfg.update(backend="fake", fan=1, tach=[], min_duty=None, max_duty=None, label=None,
               asrock={}, supermicro={}, hwmon={}, command={})
    conf = ctl.Config.__new__(ctl.Config)
    conf.path, conf.mtime, conf.error, conf.data = "<test>", None, None, cfg
    conf.refresh = lambda: False
    try:
        ctl.control_loop(conf, {})
    except ctl.Stop:
        pass
    return [d for d in Fake.written if isinstance(d, int)] == [30, 100]


@case("a stalled tach pins the duty at max_duty")
def _():
    return run_loop([45] * 6, 6, rpm=0) == [25, 100]


@case("a healthy tach does not trip the fault")
def _():
    return run_loop([45] * 6, 6, rpm=3000) == [25]


@case("the duty never leaves min_duty..max_duty")
def _():
    hot = run_loop([95] * 3, 3, max_duty=80)
    cold = run_loop([10] * 3, 3, min_duty=35)
    return hot == [80] and cold == [35]


@case("settings that name an undrivable target are refused, not applied")
def _():
    for bad in ({"backend": "nope"},
                {"backend": "asrock", "fan": 99},
                {"backend": "supermicro", "fan": 7},
                {"backend": "hwmon", "hwmon": {"chip": "no-such-chip-here"}},
                {"backend": "command", "command": {}},
                {"backend": "supermicro", "fan": 1, "supermicro": {"min_duty": 30}}):
        cfg = {**ctl.load_defaults(), **bad}
        try:
            fb.make_backend(cfg)
        except (ValueError, KeyError):
            continue
        return False
    return True


def main():
    quiet = open(os.devnull, "w")
    passed = 0
    for name, fn in CASES:
        out, sys.stdout = sys.stdout, quiet          # the loop logs; the results matter
        try:
            ok = fn()
        except Exception as e:
            sys.stdout = out
            print(f"ERROR  {name}: {type(e).__name__}: {e}")
            continue
        sys.stdout = out
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
        passed += bool(ok)
    print(f"\n{passed}/{len(CASES)} passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
