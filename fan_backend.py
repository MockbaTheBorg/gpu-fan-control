"""How a duty percentage reaches the fan.

gpu-fan-control knows about GPU temperatures, curves and ramps; everything that actually
touches a fan goes through one backend from this module, so the same controller runs on
boards with quite different fan plumbing:

  asrock      ASRock Rack BMC, OEM IPMI netfn 0x3a. Duty and mode are PER FAN HEADER,
              16 bytes at a time, so other headers keep their own settings.
  supermicro  Supermicro BMC, OEM IPMI netfn 0x30. Duty is per fan ZONE and the mode is
              a single BOARD-WIDE setting, so every fan in the zone follows our duty and
              the other zones follow the BMC's curve for the mode we select (Full).
  hwmon       The Super I/O chip through Linux hwmon sysfs (pwmN + pwmN_enable), the path
              fancontrol and CoolerControl use. For boards with no BMC.
  command     Shell-free command templates from the settings file: for a board whose
              vendor commands are none of the above.

A backend is built from the settings file with make_backend(cfg) and drives exactly one
target: a header, a zone or a pwm output. It owns the vendor's quirks - the duty floor,
whether the mode or the duty has to be written first, what "hand it back" means and which
tachometers belong to the target - and the controller stays vendor-neutral.

Read-only probing for gpu-fan-detect is at the bottom (probe_* functions).

The IPMI backends cache the BMC's sensor catalogue on disk - see SDR_CACHE - because
walking it costs seconds per tachometer read and the readings themselves do not.
"""
import os
import re
import shlex
import stat
import subprocess
import time

VERSION = "1.00"                                # the whole package: shown by --version
TIMEOUT = 15
INSTALL_CONF = "/etc/gpu-fan-control.conf"      # written by install.sh: CONFIG_DIR=/where/the/toml/files/are
ETC_DIR = "/etc/gpu-fan-control"


class FanError(Exception):
    """A fan setting could not be read or written. Transport failure, not a dead fan."""


def run(cmd, timeout=TIMEOUT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise FanError(f"{cmd[0]} is not installed")
    except subprocess.TimeoutExpired:
        raise FanError(f"{cmd[0]} timed out after {timeout} s")
    if p.returncode != 0:
        detail = (p.stderr or p.stdout).strip().splitlines()
        raise FanError(f"{' '.join(cmd)} failed: {detail[-1] if detail else f'exit {p.returncode}'}")
    return p.stdout


def ipmi(*words, timeout=TIMEOUT):
    """ipmitool raw ... -> list of returned bytes."""
    out = run(["ipmitool", "raw", *(w if isinstance(w, str) else f"0x{w:02x}" for w in words)], timeout)
    try:
        vals = [int(b, 16) for b in out.split()]
        if any(not 0 <= value <= 255 for value in vals):
            raise ValueError('reply contains a non-byte value')
        return vals
    except ValueError:
        raise FanError(f"unexpected reply to raw {' '.join(map(str, words))}: {out.strip()!r}")


# ------------------------------------------------------------------------------ SDR cache
# Reading a tachometer asks the BMC two different questions: where the sensor lives in the
# Sensor Data Repository, and what it reads right now. Over the KCS interface the first
# costs seconds - the whole repository is walked a few bytes per packet - and the second
# costs milliseconds. The repository only changes when sensors are added or erased or the
# BMC firmware is replaced, so dump it once and hand the dump back to ipmitool with -S:
# ipmitool still fetches every value live, it just stops re-reading the catalogue.
# Measured on a TRX40D8-2N2T (BMC 1.30, 65 records): 8-10 s per call becomes 0.4 s, which
# matters because gpu-fan-control reads the tach on every check of its interval.
SDR_CACHE = "sdr-cache"                 # in _sdr_cache_dirs(), with a ".id" file beside it
SDR_RECHECK = 300                       # seconds between re-asking the BMC if it changed
_sdr = {"path": None, "checked": 0.0}   # per process: the validated dump, and when


def _sdr_cache_dirs():
    """Where a dump may live. The shared one first: root writes it, any uid may read it."""
    dirs = ["/var/cache/gpu-fan-control"]
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    if os.geteuid() != 0 and os.path.isabs(base):
        dirs.append(os.path.join(base, "gpu-fan-control"))
    return dirs


def _sdr_trusted(path):
    """True if path is a plain file, owned by root or us, that no one else can write.

    ipmitool parses this file, so a dump another user could have swapped is not one we
    hand it: a bogus catalogue would rename sensors and so misdirect the tach watch."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
            and st.st_uid in (0, os.geteuid()) and not st.st_mode & 0o022)


def _sdr_id():
    """What the BMC says about its repository; changes when the catalogue would."""
    info = ipmi(0x0a, 0x20, timeout=10)         # Get SDR Repository Info: version, count
    dev = ipmi(0x06, 0x01, timeout=10)          # Get Device ID: firmware revision
    return " ".join(f"{b:02x}" for b in info + dev)


def _sdr_write(want):
    """Dump the repository once, atomically, and stamp it with `want`. None if we can't."""
    for d in _sdr_cache_dirs():
        cache = os.path.join(d, SDR_CACHE)
        tmp, tmp_id = f"{cache}.{os.getpid()}", f"{cache}.id.{os.getpid()}"
        try:
            os.makedirs(d, mode=0o755, exist_ok=True)
            run(["ipmitool", "sdr", "dump", tmp], timeout=max(TIMEOUT, 30))
            if not os.path.getsize(tmp):
                raise FanError("ipmitool wrote an empty SDR dump")
            with open(tmp_id, "w") as f:
                f.write(want + "\n")
            os.chmod(tmp, 0o644)
            os.chmod(tmp_id, 0o644)
            os.replace(tmp, cache)              # the dump lands before the stamp that
            os.replace(tmp_id, f"{cache}.id")   # vouches for it, never the other way
            return cache
        except (OSError, FanError):
            for leftover in (tmp, tmp_id):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
    return None


def sdr_cache():
    """A dump that still matches the BMC, for ipmitool -S, or None to walk the repository.

    Revalidated every SDR_RECHECK seconds, so a long-running controller notices a BMC
    that was reflashed or had sensors added underneath it. Never raises: every failure
    here just means reading the repository the slow, always-correct way."""
    if os.environ.get("GPU_FAN_SDR_CACHE", "").strip().lower() in ("0", "no", "off"):
        return None
    now = time.monotonic()
    if _sdr["checked"] and now - _sdr["checked"] < SDR_RECHECK:
        return _sdr["path"]
    path = None
    try:
        want = _sdr_id()
        for d in _sdr_cache_dirs():
            cache = os.path.join(d, SDR_CACHE)
            if not (_sdr_trusted(cache) and _sdr_trusted(f"{cache}.id")):
                continue
            try:
                with open(f"{cache}.id") as f:
                    if f.read().strip() == want and os.path.getsize(cache):
                        path = cache
                        break
            except OSError:
                continue
        if path is None:
            path = _sdr_write(want)
    except (FanError, OSError):
        path = None
    _sdr.update(path=path, checked=now)
    return path


def sdr_fans():
    """{sensor name: rpm or None} for every fan sensor the BMC knows.

    None means the sensor exists but has no reading (empty header, or a fan that has
    stopped). One ipmitool call for all of them, against a cached copy of the sensor
    catalogue when there is a valid one - see SDR_CACHE above."""
    cache = sdr_cache()
    fans = _sdr_fans_via(cache)
    if not fans and cache:
        # An empty answer from a cached catalogue is indistinguishable from a board with
        # no fan sensors, and silently losing the tach watch is worse than being slow.
        _sdr.update(path=None, checked=0.0)
        fans = _sdr_fans_via(None)
    return fans


def _sdr_fans_via(cache):
    """Parse `ipmitool sdr type Fan`, optionally against a dumped catalogue."""
    fans = {}
    args = ["-S", cache] if cache else []
    for line in run(["ipmitool", *args, "sdr", "type", "Fan"]).splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 2 or not cols[0]:
            continue
        m = re.search(r"(\d+)\s*RPM", cols[-1])
        fans[cols[0]] = int(m.group(1)) if m else None
    return fans


# --------------------------------------------------------------------------------- backends
class Backend:
    """One fan control path. Subclasses fill in the vendor specifics.

    duty_first: write the duty before taking control (ASRock keeps a stored per-header
    duty, so writing it first stops the fan dipping to a stale value for one check).
    Where taking control is a mode switch that itself resets the duty (Supermicro) or
    where the duty register is only read once we own it (hwmon), control comes first."""
    kind = "?"
    min_duty, max_duty = 20, 100
    duty_first = True
    OPTIONS = ()                    # keys allowed in this backend's own [table]

    def options(self):
        """This backend's [table] from the settings, with its keys checked.

        The check earns its keep: in TOML every key after a [table] header belongs to that
        table, so a `min_duty = 30` written below [supermicro] silently becomes
        supermicro.min_duty and the real setting keeps its default. Here that is an error
        with the reason attached, instead of a fan curve that quietly does the wrong thing."""
        opts = self.cfg.get(self.kind, {})
        if unknown := set(opts) - set(self.OPTIONS):
            raise ValueError(
                f"unknown setting(s) in [{self.kind}]: {', '.join(sorted(unknown))}"
                + (f" (this table takes {', '.join(self.OPTIONS)})" if self.OPTIONS else "")
                + f". Note that any setting written after the [{self.kind}] line lands inside"
                  " it - in TOML, tables go last in the file.")
        return opts

    def __init__(self, cfg):
        self.cfg = cfg
        for key in ('min_duty', 'max_duty'):
            if cfg.get(key) is not None and (isinstance(cfg[key], bool) or not isinstance(cfg[key], int)):
                raise ValueError(f'{key} must be an integer')
        if cfg.get("min_duty") is not None:
            self.min_duty = int(cfg["min_duty"])
        if cfg.get("max_duty") is not None:
            self.max_duty = int(cfg["max_duty"])
        if not 0 <= self.min_duty <= self.max_duty <= 100:
            raise ValueError(f"need 0 <= min_duty ({self.min_duty}) <= max_duty ({self.max_duty}) <= 100")

    # -- what the controller calls -------------------------------------------------
    @property
    def label(self):
        """How this target is named in logs and on screen, e.g. "FAN2"."""
        raise NotImplementedError

    def drive(self, duty, write_duty=True):
        """Hold the target at `duty` under our control. Called every check, so it also
        re-asserts control if something else (a BMC web UI, a reboot of the BMC) took it
        back. Writes only what has changed."""
        if self.duty_first:
            if write_duty:
                self.set_duty(duty)
            self.take_over()
        else:
            self.take_over()
            if write_duty:
                self.set_duty(duty)

    def take_over(self):
        """Put the target under manual control (idempotent)."""
        raise NotImplementedError

    def release(self):
        """Hand the target back to the board's own fan control."""
        raise NotImplementedError

    def set_duty(self, duty):
        raise NotImplementedError

    def mode(self):
        """(code, text, ours) - what is controlling the fan now. code None if unknown."""
        return None, "unknown", False

    def live_duty(self):
        """The duty the board is applying now, or None if it can't be read."""
        return None

    def tachs(self):
        """Sensor names watched for fan_min_rpm. From the config, else the backend's guess."""
        names = self.cfg.get("tach")
        if isinstance(names, str):
            names = [names]
        return list(names) if names else self.default_tachs()

    def default_tachs(self):
        return []

    def rpms(self):
        """{name: rpm or None} for this target's tachs. Raises FanError if unreadable."""
        return {}


class IpmiBackend(Backend):
    def all_fans(self):
        return sdr_fans()

    def rpms(self):
        fans = self.all_fans()
        out = {}
        for name in self.tachs():
            if name in fans:
                out[name] = fans[name]
            else:                       # tolerate case differences between BMC versions
                hit = next((k for k in fans if k.lower() == name.lower()), None)
                out[name] = fans[hit] if hit else None
        return out


class AsrockIpmi(IpmiBackend):
    """ASRock Rack OEM commands (netfn 0x3a), as on the TRX40D8-2N2T with BMC fw 1.30.

    Every command carries one byte per fan header (FAN1 first), so the controller reads
    all of them, changes only its own and writes them back: the other headers keep their
    mode and duty. The BMC rejects a manual duty below 20 % with rsp=0xcc.

    Modes: 0 = Default (BMC curve), 1 = Manual, 2 = Customize.
    """
    kind = "asrock"
    min_duty, max_duty = 20, 100
    duty_first = True
    MODES = {0: "Default (BMC curve)", 1: "Manual (gpu-fan-control)", 2: "Customize"}
    CMDS = {"mode_get": 0xd9, "mode_set": 0xd8, "duty_get": 0xd7, "duty_set": 0xd6, "duty_live": 0xda}
    OPTIONS = ("netfn", "headers", *CMDS)

    def __init__(self, cfg):
        super().__init__(cfg)
        opts = self.options()
        self.netfn = opts.get("netfn", 0x3a)
        self.cmd = {**self.CMDS, **{k: v for k, v in opts.items() if k in self.CMDS}}
        self.headers = int(opts.get("headers", 16))
        self.fan = int(cfg["fan"])
        if not 1 <= self.fan <= self.headers:
            raise ValueError(f"fan must be 1..{self.headers} for the asrock backend")

    @property
    def label(self):
        return f"FAN{self.fan}"

    def _bytes(self, cmd):
        vals = ipmi(self.netfn, self.cmd[cmd])
        if len(vals) != self.headers:
            raise FanError(f"expected {self.headers} bytes from {cmd}, got {len(vals)}")
        return vals

    def _set_byte(self, get_cmd, set_cmd, value):
        vals = self._bytes(get_cmd)
        if vals[self.fan - 1] != value:
            vals[self.fan - 1] = value
            ipmi(self.netfn, self.cmd[set_cmd], *vals)

    def take_over(self):
        self._set_byte("mode_get", "mode_set", 1)

    def release(self):
        self._set_byte("mode_get", "mode_set", 0)

    def set_duty(self, duty):
        self._set_byte("duty_get", "duty_set", int(duty))

    def mode(self):
        code = self._bytes("mode_get")[self.fan - 1]
        return code, self.MODES.get(code, f"mode {code}"), code == 1

    def live_duty(self):
        return self._bytes("duty_live")[self.fan - 1]

    def default_tachs(self):
        return [f"FAN{self.fan}_1"]     # FANn_2 is the second rotor of a dual-rotor fan


class SupermicroIpmi(IpmiBackend):
    """Supermicro OEM commands (netfn 0x30), as on the H11SSL-i and the X9/X10/X11 family.

    Two differences from ASRock that matter:

      * Duty is set per fan ZONE, not per header. Zone 0 is the numbered headers
        (FAN1..FAN5 on an H11SSL-i), zone 1 the lettered ones (FANA, FANB). Every fan in
        the zone we drive follows our duty, so the GPU fan wants a zone of its own.
      * The mode is one board-wide setting, not per header. A manual duty only sticks
        while the board is in Full mode (1), so taking over switches the whole board to
        Full: the zones we don't drive then run on the BMC's Full-mode curve rather than
        the Standard one. Releasing puts the mode back to what it was at startup.

      0x30 0x45 0x00                    get mode
      0x30 0x45 0x01 <mode>             set mode (0 Standard, 1 Full, 2 Optimal, 4 HeavyIO)
      0x30 0x70 0x66 0x00 <zone>        get the zone's duty (%)
      0x30 0x70 0x66 0x01 <zone> <duty> set it (0x00..0x64)

    Duties below about 30 % stall some fans, and a tach reading under the BMC's own lower
    critical threshold makes the BMC panic and blast every fan to full - which then reads
    as a drop in GPU temperature, so the controller lowers the duty and it happens again.
    min_duty defaults to 30 here for that reason; lower it only after watching the fan
    hold steady at the new floor (gpu-fan-detect --spin does exactly that).
    """
    kind = "supermicro"
    min_duty, max_duty = 30, 100
    duty_first = False                  # switching to Full resets the zone duty
    MODES = {0: "Standard (BMC curve)", 1: "Full (gpu-fan-control)", 2: "Optimal",
             3: "PUE / Heavy IO", 4: "Heavy IO"}
    OPTIONS = ("full_mode", "restore_mode")

    def __init__(self, cfg):
        super().__init__(cfg)
        opts = self.options()
        self.full_mode = opts.get("full_mode", 1)
        self.zone = cfg["fan"]
        for key, value in (('zone', self.zone), ('full_mode', self.full_mode),
                           ('restore_mode', opts.get('restore_mode'))):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f'supermicro {key} must be an integer')
        if self.full_mode not in self.MODES or opts.get('restore_mode', 0) not in self.MODES:
            raise ValueError('supermicro mode must be one of 0, 1, 2, 3, 4')
        if not 0 <= self.zone <= 3:
            raise ValueError("fan (the zone) must be 0..3 for the supermicro backend")
        self._restore_mode = opts.get("restore_mode")   # None: whatever the mode was at startup
        self._found_mode = None

    @property
    def label(self):
        return f"zone {self.zone}"

    def _mode_code(self):
        vals = ipmi(0x30, 0x45, 0x00)
        if len(vals) != 1 or vals[0] not in self.MODES:
            raise FanError(f"invalid Supermicro fan mode reply: {vals!r}")
        return vals[0]

    def drive(self, duty, write_duty=True):
        # Mode switches can reset duty, and BMC resets/other controllers can change
        # it without changing our software demand. Compare live duty every cycle;
        # set_duty still skips a SET when the readback already matches.
        super().drive(duty, write_duty=True)

    def take_over(self):
        code = self._mode_code()
        if self._found_mode is None:
            self._found_mode = code     # remember the board's own mode for release()
        if code != self.full_mode:
            ipmi(0x30, 0x45, 0x01, self.full_mode)
            if self._mode_code() != self.full_mode:
                raise FanError('Supermicro did not enter the requested fan mode')

    def release(self):
        back = self._restore_mode
        if back is None:
            back = self._found_mode if self._found_mode not in (None, self.full_mode) else 0
        if self._mode_code() != int(back):
            ipmi(0x30, 0x45, 0x01, int(back))
            if self._mode_code() != int(back):
                raise FanError('Supermicro fan-mode restoration was not confirmed by readback')

    def restore_name(self):
        """What release() will set, for logging."""
        back = self._restore_mode
        if back is None:
            back = self._found_mode if self._found_mode not in (None, self.full_mode) else 0
        return self.MODES.get(int(back), f"mode {back}")

    def set_duty(self, duty):
        want = int(duty)
        if self.live_duty() == want:
            return
        # Verify the board really took the duty - but give it a moment to. Entering full
        # mode resets the duty register, and this BMC can report the old value for up to
        # a second after the write, so a single immediate readback reports a phantom
        # mismatch on the first write after take_over(). Re-issue the SET a couple of
        # times before believing it: a duty that never lands is a real fault.
        actual = None
        for _ in range(3):
            ipmi(0x30, 0x70, 0x66, 0x01, self.zone, want)
            for _ in range(4):
                time.sleep(0.25)
                actual = self.live_duty()
                if actual == want:
                    return
        raise FanError(f'Supermicro duty readback {actual}% differs from requested {want}%')

    def mode(self):
        code = self._mode_code()
        return code, self.MODES.get(code, f"mode {code}"), code == self.full_mode

    def live_duty(self):
        vals = ipmi(0x30, 0x70, 0x66, 0x00, self.zone)
        if len(vals) != 1 or not 0 <= vals[0] <= 100:
            raise FanError(f"invalid Supermicro zone duty reply: {vals!r}")
        return vals[0]

    def default_tachs(self):
        """The populated headers of this zone: numbered for zone 0, lettered for zone 1."""
        want = re.compile(r"FAN\d" if self.zone == 0 else r"FAN[A-Z]", re.I)
        try:
            fans = self.all_fans()
        except FanError:
            return []
        return [n for n, rpm in fans.items() if want.fullmatch(n.split()[0]) and rpm is not None]


class Hwmon(Backend):
    """The Super I/O chip through Linux hwmon sysfs: what fancontrol and CoolerControl use.

    For a board with no BMC. pwmN takes 0..255 (written as a percentage of it) and
    pwmN_enable picks who drives it: 0 = full on, 1 = manual, 2+ = the chip's own
    automatic modes. Releasing writes back whatever pwmN_enable held at startup.

    Caveat: an hwmon pwm output is only useful if it is physically wired to a header. On
    boards whose headers go to a BMC (this project's own TRX40D8-2N2T) the nct6798 driver
    still offers pwm1..7, but nothing is connected to them and the tachs read 0 RPM.
    gpu-fan-detect --spin is the way to tell: a real output changes a tach reading.
    """
    kind = "hwmon"
    min_duty, max_duty = 20, 100
    duty_first = False                  # the pwm value is only ours once enable = manual
    OPTIONS = ("pwm", "chip", "manual_value", "restore_value")

    def __init__(self, cfg):
        super().__init__(cfg)
        opts = self.options()
        self.pwm = opts.get("pwm")
        if not self.pwm:
            chip = opts.get("chip")
            if not chip:
                raise ValueError('the hwmon backend needs hwmon.pwm = "/sys/.../pwm2" '
                                 'or hwmon.chip = "nct6798"')
            base = find_hwmon(chip)
            if not base:
                raise ValueError(f"no hwmon chip named {chip!r} under /sys/class/hwmon")
            self.pwm = os.path.join(base, f"pwm{int(cfg['fan'])}")
        if not os.path.exists(self.pwm):
            raise ValueError(f"{self.pwm} does not exist")
        self.enable = self.pwm + "_enable"
        self.manual = int(opts.get("manual_value", 1))
        self._restore = opts.get("restore_value")
        self._found = None

    @property
    def label(self):
        return f"{os.path.basename(self.pwm)} on {chip_name(os.path.dirname(self.pwm))}"

    def _read(self, path):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError) as e:
            raise FanError(f"can't read {path}: {e}")

    def _write(self, path, value):
        try:
            with open(path, "w") as f:
                f.write(str(int(value)))
        except OSError as e:
            raise FanError(f"can't write {value} to {path}: {e} "
                           f"{'(needs root)' if isinstance(e, PermissionError) else ''}".strip())

    def take_over(self):
        if not os.path.exists(self.enable):
            return                      # some drivers have no enable file: pwm is always manual
        now = self._read(self.enable)
        if self._found is None:
            self._found = now
        if now != self.manual:
            self._write(self.enable, self.manual)

    def release(self):
        if not os.path.exists(self.enable):
            return
        back = self._restore
        if back is None:
            back = self._found if self._found not in (None, self.manual) else 2
        self._write(self.enable, back)

    def set_duty(self, duty):
        self._write(self.pwm, round(int(duty) * 255 / 100))

    def mode(self):
        if not os.path.exists(self.enable):
            return None, "manual (no enable file)", True
        code = self._read(self.enable)
        text = {0: "full on (no control)", 1: "manual (gpu-fan-control)"}.get(code, f"automatic (mode {code})")
        return code, text, code == self.manual

    def live_duty(self):
        return round(self._read(self.pwm) * 100 / 255)

    def default_tachs(self):
        base = os.path.dirname(self.pwm)
        n = re.sub(r"\D", "", os.path.basename(self.pwm))
        path = os.path.join(base, f"fan{n}_input")
        return [path] if os.path.exists(path) else []

    def rpms(self):
        out = {}
        for t in self.tachs():
            path = t if t.startswith("/") else os.path.join(os.path.dirname(self.pwm), t)
            out[os.path.basename(path)] = self._read(path) if os.path.exists(path) else None
        return out


class Command(Backend):
    """Command templates from the settings file, for a board none of the others fit.

    Each is a command line with {duty} substituted; it is split like a shell would split
    it but run without one, so pipes, redirection and $VARS are not available - put
    anything that needs them in a script of its own and call that.

        [command]
        take_over = "ipmitool raw 0x30 0x45 0x01 0x01"
        set_duty  = "ipmitool raw 0x30 0x70 0x66 0x01 0x01 {duty:#x}"
        release   = "ipmitool raw 0x30 0x45 0x01 0x00"
        live_duty = "my-fan-duty"       # optional: must print a number
        rpm       = "my-fan-rpm"        # optional: must print a number

    set_duty is required; the rest are optional. The commands run as root, from the
    settings file, which is why install.sh keeps that file out of a user-writable folder.
    """
    kind = "command"
    min_duty, max_duty = 0, 100
    duty_first = False
    OPTIONS = ("take_over", "set_duty", "release", "live_duty", "rpm")

    def __init__(self, cfg):
        super().__init__(cfg)
        self.opts = self.options()
        if not self.opts.get("set_duty"):
            raise ValueError('the command backend needs command.set_duty = "..."')

    @property
    def label(self):
        return str(self.cfg.get("label", "custom fan command"))

    def _run(self, key, duty=None):
        tmpl = self.opts.get(key)
        if not tmpl:
            return None
        try:
            line = tmpl.format(duty=int(duty) if duty is not None else 0)
        except (KeyError, IndexError, ValueError) as e:
            raise FanError(f"command.{key} template {tmpl!r} is not usable: {e}")
        return run(shlex.split(line))

    def take_over(self):
        self._run("take_over")

    def release(self):
        self._run("release")

    def set_duty(self, duty):
        self._run("set_duty", duty)

    def _number(self, key):
        out = self._run(key)
        if out is None:
            return None
        m = re.search(r"-?\d+", out)
        if not m:
            raise FanError(f"command.{key} printed no number: {out.strip()!r}")
        return int(m.group())

    def mode(self):
        return None, "custom commands", True

    def live_duty(self):
        return self._number("live_duty")

    def rpms(self):
        return {self.label: self._number("rpm")} if self.opts.get("rpm") else {}


BACKENDS = {b.kind: b for b in (AsrockIpmi, SupermicroIpmi, Hwmon, Command)}


def make_backend(cfg):
    kind = cfg.get("backend", "asrock")
    if kind not in BACKENDS:
        raise ValueError(f"unknown backend {kind!r}: use one of {', '.join(sorted(BACKENDS))}")
    return BACKENDS[kind](cfg)


# --------------------------------------------------------------- finding the settings files
def config_dirs(script_file=None):
    """Where the .toml settings files are looked for, best first.

    GPU_FAN_CONFIG_DIR wins; then the folder the running script sits in (so the package
    folder works as-is, before or without an install); then CONFIG_DIR out of
    /etc/gpu-fan-control.conf, which install.sh writes so the copies in /usr/local/bin
    can find the settings wherever the package was unpacked; then /etc/gpu-fan-control."""
    dirs = []
    if env := os.environ.get("GPU_FAN_CONFIG_DIR"):
        dirs.append(env)
    if script_file:
        dirs.append(os.path.dirname(os.path.realpath(script_file)))
    try:
        with open(INSTALL_CONF) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "CONFIG_DIR" and value:
                    dirs.append(value.strip().strip('"\''))
    except OSError:
        pass
    dirs.append(ETC_DIR)
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def find_config(name, script_file=None):
    """The settings file to use. Returns the last candidate even when nothing exists, so
    the caller reports a sensible path and falls back to its built-in defaults."""
    tried = config_dirs(script_file)
    for d in tried:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return path
    return os.path.join(tried[-1], name)


# ------------------------------------------------------------------------- hwmon helpers
def hwmon_chips():
    """[(path, name)] for every hwmon device."""
    out = []
    base = "/sys/class/hwmon"
    for entry in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        path = os.path.join(base, entry)
        out.append((path, chip_name(path)))
    return out


def chip_name(path):
    try:
        with open(os.path.join(path, "name")) as f:
            return f.read().strip()
    except OSError:
        return "?"


def find_hwmon(name):
    for path, chip in hwmon_chips():
        if chip == name:
            return path
    return None


# ------------------------------------------------------------------ probing, for gpu-fan-detect
def probe_tool(name):
    return bool(run(["sh", "-c", f"command -v {shlex.quote(name)} || true"]).strip())


def ipmi_device():
    """The in-band IPMI device node, if the kernel made one. Its absence means no BMC or
    no ipmi_devintf/ipmi_si; its presence plus a failing ipmitool usually means no root,
    because the node is mode 0600 root:root and ipmitool reports that as if the file were
    missing."""
    for path in ("/dev/ipmi0", "/dev/ipmi/0", "/dev/ipmidev/0"):
        if os.path.exists(path):
            return path
    return None


def probe_bmc():
    """What the BMC says about itself: {found, manufacturer, product, firmware, error}."""
    info = {"found": False, "manufacturer": "", "product": "", "firmware": "", "error": None}
    try:
        out = run(["ipmitool", "mc", "info"], timeout=20)
    except FanError as e:
        info["error"] = str(e)
        return info
    info["found"] = True
    for line in out.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "manufacturer name":
            info["manufacturer"] = value
        elif key == "product name":
            info["product"] = value
        elif key == "firmware revision":
            info["firmware"] = value
    return info


def probe_vendor_commands():
    """Which OEM command sets this BMC answers: {backend: (works, detail)}."""
    results = {}
    try:
        vals = ipmi(0x3a, 0xd9)
        results["asrock"] = (len(vals) >= 2,
                             f"get-mode returned {len(vals)} bytes: "
                             + " ".join(f"{v:02x}" for v in vals[:8]) + ("..." if len(vals) > 8 else ""))
    except FanError as e:
        results["asrock"] = (False, str(e))
    try:
        vals = ipmi(0x30, 0x45, 0x00)
        mode = vals[0] if vals else None
        results["supermicro"] = (bool(vals),
                                 f"fan mode is {mode} ({SupermicroIpmi.MODES.get(mode, '?')})")
    except FanError as e:
        results["supermicro"] = (False, str(e))
    return results


def probe_zones(limit=3):
    """{zone: duty} for the Supermicro zones that answer."""
    zones = {}
    for z in range(limit + 1):
        try:
            vals = ipmi(0x30, 0x70, 0x66, 0x00, z)
        except FanError:
            continue
        if vals:
            zones[z] = vals[0]
    return zones


def probe_hwmon():
    """[{chip, path, pwms, fans}] for every hwmon chip that has a pwm output or a tach."""
    found = []
    for path, chip in hwmon_chips():
        pwms, fans = [], {}
        for entry in sorted(os.listdir(path)):
            if re.fullmatch(r"pwm\d+", entry):
                pwms.append(entry)
            elif re.fullmatch(r"fan\d+_input", entry):
                try:
                    with open(os.path.join(path, entry)) as f:
                        fans[entry] = int(f.read().strip())
                except (OSError, ValueError):
                    fans[entry] = None
        if pwms or fans:
            found.append({"chip": chip, "path": path, "pwms": pwms, "fans": fans})
    return found
