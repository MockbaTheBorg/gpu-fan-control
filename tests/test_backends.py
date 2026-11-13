#!/usr/bin/python3
"""Pin down exactly what each backend writes, without any hardware.

    python3 tests/test_backends.py

The IPMI transport is replaced by a recorder, so these are assertions about the bytes that
would go to the BMC: the command, the order, and the read-modify-write. That matters because
a wrong byte on real hardware either does nothing or does something else, and the two are
hard to tell apart from the far end of a phone call.

What this cannot check is whether a given board accepts those bytes at all. Only the board
can answer that: `sudo gpu-fan-detect` asks it, and `--spin` proves a fan moves.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, HERE)
import fan_backend as fb                                                        # noqa: E402

SENT = []
REAL_SDR_FANS = fb.sdr_fans          # cases that stub it out must not blind the ones after


def recorder(reply):
    """Replace fan_backend.ipmi: record the words sent, answer with `reply(words)`."""
    def fake(*words, timeout=fb.TIMEOUT):
        SENT.append(" ".join(w if isinstance(w, str) else f"0x{w:02x}" for w in words))
        return reply(words)
    return fake


CASES = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


# ------------------------------------------------------------------------------ asrock
@case("asrock writes the duty before taking over, and only its own header's byte")
def _():
    mode = [0] * 16
    duty = [50] * 16

    def reply(words):
        cmd = words[1]
        if cmd == 0xd9:
            return list(mode)
        if cmd == 0xd7:
            return list(duty)
        if cmd == 0xd8:
            mode[:] = list(words[2:])
            return []
        if cmd == 0xd6:
            duty[:] = list(words[2:])
            return []
        return []
    fb.ipmi = recorder(reply)
    SENT.clear()
    b = fb.make_backend({"backend": "asrock", "fan": 2})
    b.drive(45)
    return (SENT == ["0x3a 0xd7",                                     # read all 16 duties
                     "0x3a 0xd6 " + " ".join(["0x32"] + ["0x2d"] + ["0x32"] * 14),
                     "0x3a 0xd9",                                     # read all 16 modes
                     "0x3a 0xd8 " + " ".join(["0x00"] + ["0x01"] + ["0x00"] * 14)]
            and duty == [50, 45] + [50] * 14                          # FAN1 and FAN3.. untouched
            and mode == [0, 1] + [0] * 14)


@case("asrock skips the write when the byte is already right")
def _():
    fb.ipmi = recorder(lambda w: [0] * 16 if w[1] == 0xd9 else [45] * 16)
    SENT.clear()
    b = fb.make_backend({"backend": "asrock", "fan": 2})
    b.set_duty(45)                      # already 45: read, no write
    return SENT == ["0x3a 0xd7"]


@case("asrock hands the header back to Default mode")
def _():
    fb.ipmi = recorder(lambda w: [1] * 16)
    SENT.clear()
    fb.make_backend({"backend": "asrock", "fan": 3}).release()
    return SENT == ["0x3a 0xd9", "0x3a 0xd8 " + " ".join(["0x01", "0x01", "0x00"] + ["0x01"] * 13)]


@case("asrock rejects a header it doesn't have")
def _():
    for bad in (0, 17, 99):
        try:
            fb.make_backend({"backend": "asrock", "fan": bad})
        except ValueError:
            continue
        return False
    return True


# -------------------------------------------------------------------------- supermicro
@case("supermicro switches to Full mode BEFORE setting the zone's duty")
def _():
    state = {"mode": 0, "duty": {0: 50, 1: 50}}

    def reply(words):
        if words[:3] == (0x30, 0x45, 0x00):
            return [state["mode"]]
        if words[:3] == (0x30, 0x45, 0x01):
            state["mode"] = words[3]
            return []
        if words[:4] == (0x30, 0x70, 0x66, 0x00):
            return [state["duty"][words[4]]]
        if words[:4] == (0x30, 0x70, 0x66, 0x01):
            state["duty"][words[4]] = words[5]
            return []
        return []
    fb.ipmi = recorder(reply)
    SENT.clear()
    b = fb.make_backend({"backend": "supermicro", "fan": 1})
    b.drive(45)
    # mode first (switching to Full resets the duty), then the duty, and 45 % is 0x2d
    return (SENT == ["0x30 0x45 0x00", "0x30 0x45 0x01 0x01", "0x30 0x45 0x00",
                     "0x30 0x70 0x66 0x00 0x01", "0x30 0x70 0x66 0x01 0x01 0x2d",
                     "0x30 0x70 0x66 0x00 0x01"]
            and state == {"mode": 1, "duty": {0: 50, 1: 45}})     # zone 0 untouched


@case("supermicro puts the board back in the mode it found at startup")
def _():
    state = {"mode": 2}                 # the board was in Optimal

    def reply(words):
        if words[:3] == (0x30, 0x45, 0x00):
            return [state["mode"]]
        if words[:3] == (0x30, 0x45, 0x01):
            state["mode"] = words[3]
        return []
    fb.ipmi = recorder(reply)
    b = fb.make_backend({"backend": "supermicro", "fan": 1})
    b.take_over()                       # notices Optimal, switches to Full
    assert state["mode"] == 1
    SENT.clear()
    b.release()
    return (state["mode"] == 2 and SENT[-2:] == ["0x30 0x45 0x01 0x02", "0x30 0x45 0x00"]
            and b.restore_name() == "Optimal")


@case("supermicro falls back to Standard if it was already in Full")
def _():
    state = {"mode": 1}
    fb.ipmi = recorder(lambda w: [state["mode"]] if w[:3] == (0x30, 0x45, 0x00)
                       else (state.update(mode=w[3]) or []) if w[:3] == (0x30, 0x45, 0x01) else [])
    b = fb.make_backend({"backend": "supermicro", "fan": 1})
    b.take_over()
    b.release()
    return state["mode"] == 0


@case("supermicro's duty floor is 30 %, and an explicit min_duty wins")
def _():
    a = fb.make_backend({"backend": "supermicro", "fan": 1})
    b = fb.make_backend({"backend": "supermicro", "fan": 1, "min_duty": 40})
    c = fb.make_backend({"backend": "asrock", "fan": 2})
    return (a.min_duty, b.min_duty, c.min_duty) == (30, 40, 20)


@case("supermicro guesses the zone's tachs from the sensor names")
def _():
    fb.sdr_fans = lambda: {"FAN1": 1200, "FAN2": 1100, "FAN5": None, "FANA": 3400, "FANB": None}
    zone1 = fb.make_backend({"backend": "supermicro", "fan": 1}).tachs()
    zone0 = fb.make_backend({"backend": "supermicro", "fan": 0}).tachs()
    named = fb.make_backend({"backend": "supermicro", "fan": 1, "tach": ["FANB"]}).tachs()
    # only populated headers (a reading), lettered for zone 1 and numbered for zone 0
    return zone1 == ["FANA"] and zone0 == ["FAN1", "FAN2"] and named == ["FANB"]


@case("a tach with no reading comes back as None, not as a missing key")
def _():
    fb.sdr_fans = lambda: {"FANA": 3400, "FANB": None}
    b = fb.make_backend({"backend": "supermicro", "fan": 1, "tach": ["FANA", "FANB", "FANZ"]})
    return b.rpms() == {"FANA": 3400, "FANB": None, "FANZ": None}


# ------------------------------------------------------------------------------- hwmon
@case("hwmon takes manual control first, then scales the duty to 0-255")
def _():
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "name"), "w").write("faketio\n")
        open(os.path.join(d, "pwm2"), "w").write("128")
        open(os.path.join(d, "pwm2_enable"), "w").write("5")
        open(os.path.join(d, "fan2_input"), "w").write("2200")
        b = fb.make_backend({"backend": "hwmon", "fan": 2,
                             "hwmon": {"pwm": os.path.join(d, "pwm2")}})
        b.drive(45)
        enable = open(os.path.join(d, "pwm2_enable")).read()
        pwm = int(open(os.path.join(d, "pwm2")).read())
        rpms, live = b.rpms(), b.live_duty()
        b.release()                     # back to 5, the automatic mode it was found in
        return (enable == "1" and pwm == round(45 * 255 / 100) == 115
                and rpms == {"fan2_input": 2200} and live == 45
                and open(os.path.join(d, "pwm2_enable")).read() == "5")


@case("hwmon finds the chip by name and refuses one that isn't there")
def _():
    try:
        fb.make_backend({"backend": "hwmon", "fan": 1, "hwmon": {"chip": "definitely-not-a-chip"}})
    except ValueError:
        pass
    else:
        return False
    try:
        fb.make_backend({"backend": "hwmon", "fan": 1})          # neither pwm nor chip
    except ValueError:
        return True
    return False


# ----------------------------------------------------------------------------- command
@case("command substitutes {duty}, in decimal and in hex, and runs without a shell")
def _():
    ran = []
    fb.run = lambda cmd, timeout=fb.TIMEOUT: (ran.append(cmd), "7200\n")[1]
    b = fb.make_backend({"backend": "command", "label": "GPU fan", "command": {
        "take_over": "fanctl --manual",
        "set_duty": "fanctl --pct {duty} --hex {duty:#x}",
        "release": "fanctl --auto",
        "rpm": "fanctl --rpm"}})
    b.drive(45)
    rpm = b.rpms()
    b.release()
    return (ran == [["fanctl", "--manual"],
                    ["fanctl", "--pct", "45", "--hex", "0x2d"],
                    ["fanctl", "--rpm"],
                    ["fanctl", "--auto"]]
            and rpm == {"GPU fan": 7200} and b.label == "GPU fan")


# --------------------------------------------------------------------------- SDR cache
def sdr_harness(d, answer=None):
    """Point the cache at a temp dir, record the ipmitool argv, answer `sdr type Fan`.

    Returns (calls, restore). The BMC's repository id is fixed, so a stamp written by
    one call is still valid for the next unless a test changes it."""
    calls = []
    answer = answer or (lambda cmd: "FAN2_1 | 61h | ok | 7.0 | 2400 RPM\n")

    def fake_run(cmd, timeout=fb.TIMEOUT):
        calls.append(cmd)
        if "dump" in cmd:
            open(cmd[-1], "w").write("not-a-real-dump-but-not-empty")
            return ""
        return answer(cmd)
    saved = fb.run, fb.ipmi, fb._sdr_cache_dirs, fb.sdr_fans, dict(fb._sdr)
    fb.run = fake_run
    # Get SDR Repository Info, then Get Device ID: together they fingerprint the catalogue
    fb.ipmi = lambda *w, **k: [0x51, 0x41] if w[0] == 0x0a else [0x20, 0x81]
    fb._sdr_cache_dirs = lambda: [d]
    fb.sdr_fans = REAL_SDR_FANS
    fb._sdr.update(path=None, checked=0.0)

    def restore():
        fb.run, fb.ipmi, fb._sdr_cache_dirs, fb.sdr_fans = saved[:4]
        fb._sdr.clear()
        fb._sdr.update(saved[4])
    return calls, restore


@case("the fan sensors are read against a dumped catalogue, dumped once")
def _():
    with tempfile.TemporaryDirectory() as d:
        calls, restore = sdr_harness(d)
        try:
            first = fb.sdr_fans()
            fb._sdr.update(checked=0.0)         # force the next call to revalidate
            second = fb.sdr_fans()
        finally:
            restore()
        cache = os.path.join(d, fb.SDR_CACHE)
        dumps = [c for c in calls if "dump" in c]
        reads = [c for c in calls if "dump" not in c]
        return (first == second == {"FAN2_1": 2400}
                and len(dumps) == 1                             # the slow walk happens once
                and len(reads) == 2                             # both readings are still live
                and all(r[1:3] == ["-S", cache] for r in reads)
                and os.path.exists(cache + ".id"))


@case("a catalogue the BMC no longer matches is dumped again, not trusted")
def _():
    with tempfile.TemporaryDirectory() as d:
        calls, restore = sdr_harness(d)
        try:
            fb.sdr_fans()
            open(os.path.join(d, fb.SDR_CACHE + ".id"), "w").write("a different bmc\n")
            fb._sdr.update(path=None, checked=0.0)
            fans = fb.sdr_fans()
            stamp = open(os.path.join(d, fb.SDR_CACHE + ".id")).read().strip()
        finally:
            restore()
        return (len([c for c in calls if "dump" in c]) == 2
                and fans == {"FAN2_1": 2400} and stamp == "51 41 20 81")


@case("a catalogue another user could rewrite is refused")
def _():
    with tempfile.TemporaryDirectory() as d:
        calls, restore = sdr_harness(d)
        try:
            fb.sdr_fans()
            cache = os.path.join(d, fb.SDR_CACHE)
            os.chmod(cache, 0o666)                              # world-writable: untrusted
            trusted = fb._sdr_trusted(cache)
            fb._sdr.update(path=None, checked=0.0)
            fans = fb.sdr_fans()
            mode = os.stat(cache).st_mode & 0o777
        finally:
            restore()
        return not trusted and fans == {"FAN2_1": 2400} and mode == 0o644


@case("an empty answer from the catalogue falls back to walking the repository")
def _():
    with tempfile.TemporaryDirectory() as d:
        # a cached read that finds no fan sensor must not silently disable the tach watch
        calls, restore = sdr_harness(d, answer=lambda cmd: (
            "" if "-S" in cmd else "FAN2_1 | 61h | ok | 7.0 | 2400 RPM\n"))
        try:
            fans = fb.sdr_fans()
        finally:
            restore()
        reads = [c for c in calls if "dump" not in c]
        return (fans == {"FAN2_1": 2400} and len(reads) == 2
                and "-S" in reads[0] and "-S" not in reads[1]
                and fb._sdr["path"] is None)                    # and the cache is dropped


@case("GPU_FAN_SDR_CACHE=0 reads the repository directly")
def _():
    with tempfile.TemporaryDirectory() as d:
        calls, restore = sdr_harness(d)
        os.environ["GPU_FAN_SDR_CACHE"] = "0"
        try:
            fans = fb.sdr_fans()
        finally:
            del os.environ["GPU_FAN_SDR_CACHE"]
            restore()
        return (fans == {"FAN2_1": 2400} and len(calls) == 1
                and "-S" not in calls[0] and not os.listdir(d))


@case("command insists on set_duty")
def _():
    try:
        fb.make_backend({"backend": "command", "command": {"release": "x"}})
    except ValueError:
        return True
    return False


def main():
    passed = 0
    for name, fn in CASES:
        try:
            ok = fn()
        except Exception as e:
            print(f"ERROR  {name}: {type(e).__name__}: {e}")
            continue
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
        passed += bool(ok)
    print(f"\n{passed}/{len(CASES)} passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
