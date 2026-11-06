# gpu-fan-control

Sets the speed of one chassis fan from the temperature of the passively cooled
Tesla V100(s), using whatever fan control the motherboard offers.

**Installing this on a new machine?** Start with **[INSTALL.md](INSTALL.md)** — it walks
through finding which fan cools the GPU and which backend the board needs. This file is the
reference for how the thing works once it's running.

**Multiple GPUs:** every GPU that `nvidia-smi` lists is read, and the **hottest one
drives the curve**, so one fan can cool several GPUs. Nothing needs configuring when a
GPU is added: the service picks it up on its next check (or after a reboot). All the
scripts here handle one or more GPUs too.

The BMC's own fan curves can't see the GPU temperature, so this service reads it from
`nvidia-smi` and sets the fan duty itself. All other fan headers stay on the BMC's
Default curve.

## Why not CoolerControl / fancontrol / lm-sensors?

Because on a board like this one (ASRock Rack TRX40D8-2N2T) they can't reach the fans.
The headers are wired to the **BMC** (ASPEED AST2500), not to the Super I/O chip. Linux
does load an `nct6798` hwmon device with `pwm1..7`, but its tach inputs read 0 RPM and its
PWM outputs aren't connected to anything. Desktop fan tools only speak hwmon, so they have
nothing to drive. The only control path is the BMC, over IPMI or its web UI.

That's also the one thing no fan curve can be portable about, so it's a **backend**:

| `backend =` | Control path | Granularity |
|---|---|---|
| `asrock` | ASRock Rack BMC, OEM IPMI netfn `0x3a` | per fan **header**; the others keep their own mode and duty |
| `supermicro` | Supermicro BMC, OEM IPMI netfn `0x30` | per fan **zone**; the mode is board-wide |
| `hwmon` | Super I/O chip via `/sys/class/hwmon` (`pwmN`) — no BMC needed | per pwm output |
| `command` | Command lines from the settings file | whatever you write |

A backend owns its vendor's quirks — the duty floor the hardware accepts, whether the mode
or the duty has to be written first, what "hand the fan back" means, and which tachometers
belong to the target — so everything else here is about temperatures and ramps. The default
is `asrock`, which is what this machine runs. `gpu-fan-detect` works out which one a given
board needs; see [Another motherboard](#another-motherboard) and the ready-made settings
files in [`examples/`](examples/).

## Files

| File | Purpose |
|---|---|
| `fan-curve.toml` | **Settings**: backend, fan header/zone, curve, ramp-down. The service reloads it automatically. |
| `gpu-fan-control` | The controller (Python 3.11+, no extra packages; uses `nvidia-smi`, and `ipmitool` for the BMC backends). `--check` validates the settings and prints the plan without touching a fan. |
| `fan_backend.py` | The backends: everything that actually writes a fan setting. Shared by the controller and the status tool; installed to `/usr/local/lib/gpu-fan-control`. |
| `gpu-fan-detect` | **Run this first on a new machine.** What controls the fans here, which OEM commands the BMC answers, every fan sensor and its RPM. `--spin-all` drives each header or zone in turn and reports which tach followed, which is how you find the GPU's fan. `--write-config` writes a matching settings file. Read-only unless you ask for a spin test. |
| `gpu-power-limit` | Show or set the GPU power limit: `sudo ./gpu-power-limit cool` (`cool_watts`, 150 W), `full` (back to 250 W), or a number of watts (100–250). With `gpu-power-limit.service` enabled, cool mode is reapplied at every boot. |
| `power-limit.toml` | `cool_watts`: what cool mode caps each GPU at. |
| `gpu-fan-status` | Read-only status, colour coded: GPU temp, power vs limit, SM clock, **throttling and its reason** (power cap, thermal, HW slowdown), curve target, fan duty. With `sudo`, also live RPM, duty and mode from the board. `-w [SECS]` opens the [live view](#the-live-view); `watch -c -n 2 ./gpu-fan-status --color` or `-w --plain` keep the plain text. |
| `fan_look.py`, `fan_dash.py` | The live view: its look (palette, big digits, braille graphs) and its screen. Only `-w` loads them. |
| `setup-live-view.sh` | One-off: installs Textual into `.venv` here, which is all the live view needs. |
| `console/` | Makes the live view work on **this machine's own screen** (the text console), not just over SSH: the braille/block font it needs, the 16-colour palette and the resolution. One idempotent script. See [On the machine's own screen](#on-the-machines-own-screen). |
| `*.service.in` | systemd unit **templates**. `install.sh` fills in the real paths and writes the units to `/etc/systemd/system`, so nothing here hardcodes a folder. Edit these, not the generated units. |
| `examples/` | A ready-made settings file per backend, each with the vendor's traps written down: `fan-curve.supermicro.toml`, `.asrock-rack.toml`, `.hwmon.toml`, `.command.toml`. |
| `gpu-thermal-guard` | Overheating failsafe: caps GPU power at 80 °C, and at 85 °C kills every GPU process and latches until `sudo gpu-thermal-guard --reset`. `--status` shows its state. See [Overheating failsafe](#overheating-failsafe). |
| `gpu-thermal-guard.service` | systemd unit for the guard (state kept in `/var/lib/gpu-thermal-guard`). |
| `thermal-guard.toml` | Guard settings (temperatures, timings). Read when the guard starts. |
| `gpu-power-limit.service` | systemd unit that applies cool mode (`gpu-power-limit cool`) at boot. Stopping it restores the full limit. |
| `install.sh` | Installs the programs to `/usr/local/bin`, records where the settings live, generates the units, loads the IPMI modules, and starts the service. **Refuses to start a service that can't drive the fan** (it runs `--check` first), and never overwrites a settings file you already have. `--config-dir`, `--no-power-limit`, `--no-guard`, `--no-start`, `--force`. |
| `uninstall.sh` | Removes all of it, putting the fan back under the board's own control and the GPUs back on their default power limit. `--purge` also removes the settings and the guard's latch. |
| `INSTALL.md` | Setting this up on a machine it has never run on. |
| `tests/` | `test_loop.py` drives the control loop against a stub fan (curve, hold, ramp-down, failsafe, fault detection); `test_backends.py` pins the exact bytes each backend writes. No hardware, no waiting — `python3 tests/test_loop.py`. |
| `make-zip.sh` | Builds the distributable `.zip` of this folder (excluding `.venv`, caches and logs), after running the tests and parsing every settings file. |
| `VERSION` | `1.00`. `gpu-fan-status --version`, and shown in the live view's title bar. |

Load-test tools and test logs live in `../gpu-stress/` on the machine this was written on;
they are not part of this package. Any sustained GPU load does the same job — a GEMM burn, a
training run, `nvidia-smi dmon` in another window to watch it.

## Tuning the curve

Edit `fan-curve.toml` and save it. The service picks up the change on its next check
(within `interval` seconds, 5 by default). No restart is needed.

```toml
curve = [
  [40, 20],    # [GPU temp C, duty %], straight lines between points
  [50, 30],
  [60, 45],
  [70, 70],
  [78, 100],
]
```

- Below the first point the fan uses its duty, and above the last point it uses that
  point's duty. Temperatures must increase from point to point.
- Duty is clamped to **`min_duty`–`max_duty`**, which default to what the hardware accepts:
  20–100 % on an ASRock Rack BMC (it rejects less with `rsp=0xcc`), 30–100 % on a Supermicro
  one (see the trap in [How it talks to the hardware](#supermicro--supermicro-oem-ipmi)).
- **Speeding up is immediate.** When the GPU cools, the controller:
  - **holds** the current duty for `hold_secs` (60 s), so the fan doesn't drop while
    heat is still soaking out of the heatsink;
  - treats demand within `hold_band` (10 %) of the current duty as "still loaded", so
    1 °C wobbles under steady load don't start the hold timer;
  - then ramps down by at most `step_down` (2 %) per check, all the way to the curve
    value (about 3 minutes from 96 % to 20 %, after the hold). `hold_band` doesn't
    applies once the ramp has started; any rise in demand to the current duty or
    above ends the ramp.
- `failsafe_duty` (100 %) is used whenever any GPU's temperature can't be read.
- With two GPUs sharing FAN2 there is roughly twice the heat in the same airflow, so
  expect higher temperatures at the same duty. Rerun the load test with both GPUs and
  retune the curve if the hottest GPU gets near 83 °C.
- If the file has an error, the service logs it once and **keeps the last good
  settings**.

### Measured fan response (FAN2, 4-pin fan)

| Duty | 20 % | 30 % | 50 % | 65 % | 80 % | 96 % | 100 % |
|---|---|---|---|---|---|---|---|
| RPM | ~1600 | ~1800 | ~3400 | ~5000 | ~6700 | ~8500 | ~8700 |

Most of the speed increase happens above about 40 % duty. If the fan is too loud in
the 60–75 °C range, lower the middle points of the curve.

### Load-test results (2026-10-08, default curve)

- **~150 W** (FP32 FMA load): GPU settles at **68 °C**, FAN2 at 62–65 %.
- **~248 W** (tensor-core GEMM, full board power): GPU settles at **76–77 °C**,
  FAN2 at 92–96 %.
- Cool mode (`gpu-power-limit cool`) caps the board at 150 W, so heavy loads should
  settle close to the ~150 W row above.
- GPU limits for reference: max operating temp 83 °C, slowdown 88 °C,
  shutdown 91 °C.

To repeat a test on the machine that has them: `../gpu-stress/loadtest.sh [load_secs]
[cool_secs] [burn|gemmburn]`. It loads every GPU by default (one burn process per GPU);
`GPUS="1" ../gpu-stress/loadtest.sh` loads only GPU1. Every GPU is logged either way, and the
load stops on all of them if any GPU reaches 85 °C. Elsewhere, use whatever load you have and
watch `gpu-fan-status -w`.

## Overheating failsafe

`gpu-thermal-guard` is a separate service that watches **GPU temperatures only**. It
doesn't care why the GPUs are hot: a dead fan, a blocked filter or intake, a closed-up
case and a hot room all trip it the same way. It uses only `nvidia-smi`, never IPMI,
so a hung BMC (like the one on 2026-10-15) can't stall it. It keeps working if the
fan controller crashes.

| Hottest GPU | What happens |
|---|---|
| ≥ `warn_temp` (80 °C) | Warning logged. Every GPU is capped at its minimum power (100 W), which usually stops the temperature rising without losing work. Once every GPU has been below `release_temp` (75 °C) for `release_secs` (60 s), the previous limits come back (e.g. cool mode's 150 W). |
| ≥ `trip_temp` (85 °C) | **Thermal trip.** Logged as critical and sent to every terminal (`wall`). Every process using either GPU gets SIGTERM, then SIGKILL if it's still there after `kill_grace` (10 s). The guard then **latches**: GPUs stay at 100 W, and any new GPU process (e.g. a service that restarted itself) is killed straight away. |

The latch lasts until you clear it, and it survives restarts and reboots:

```bash
gpu-thermal-guard --status            # NORMAL / CAPPED / TRIPPED, since when, why
sudo journalctl -u gpu-thermal-guard  # what it did
sudo gpu-thermal-guard --reset        # clear a trip once the cause is fixed
```

`gpu-fan-status` also shows the guard's state.

Separately, the fan controller watches the fan's own tach — `FAN2_1` here, or whatever
`tach` names. If it reads below `fan_min_rpm` (300 RPM) for 3 checks while being driven, it
logs a **FAN FAULT** and holds the fan at 100 % in case it restarts. A watched sensor that
stops reporting altogether counts as 0 RPM, because it was reporting when the service
started. That's only an early warning; it doesn't stop the guard
from tripping on temperature. Failed IPMI reads aren't counted as a dead fan.
`sudo journalctl -p crit -u gpu-fan-control -u gpu-thermal-guard` shows faults and trips.

The GPUs' own protection is still the final layer: they slow down at 88 °C and shut
down at 91 °C.

## Install / update

```bash
sudo ./install.sh
```

You only need this after editing a script or a `.service.in` template. A curve change needs
nothing: save `fan-curve.toml` and the running service reloads it.

```bash
sudo ./install.sh --config-dir /etc/gpu-fan-control   # settings in /etc instead of here
sudo ./install.sh --no-power-limit                    # don't cap the GPUs at boot
sudo ./install.sh --no-start                          # copy everything, start nothing
sudo ./install.sh --force                             # install even if --check objects
```

By default the settings stay in this folder and the generated units point at it, which is
why the folder shouldn't move after an install. `install.sh` records the location in
`/etc/gpu-fan-control.conf`, so the copies in `/usr/local/bin` find them wherever they are;
`GPU_FAN_CONFIG_DIR` or `--config PATH` override that for a single run.

Before it enables anything, `install.sh` runs `gpu-fan-control --check` and **stops** if the
settings can't be used on this machine — a wrong backend fails at install time rather than
in a restart loop at three in the morning.

### Cool mode at boot

`install.sh` enables `gpu-power-limit.service`, so the GPU starts each boot capped at
`cool_watts` from `power-limit.toml` (150 W). The install itself doesn't change the current limit.

```bash
sudo systemctl start gpu-power-limit             # apply cool mode now
sudo systemctl stop gpu-power-limit              # full power now (back to cool at next boot)
sudo systemctl disable --now gpu-power-limit     # full power now and at boot
sudo systemctl enable --now gpu-power-limit      # cool mode now and at boot
```

`sudo ./gpu-power-limit full` or `cool` changes the limit until the next boot without
touching the unit. To change the preset, edit `cool_watts` in `power-limit.toml` — it is
read every time `gpu-power-limit` runs, so no reinstall is needed.
The service runs `/usr/local/bin/gpu-fan-control`, an installed copy. It doesn't run
the script in this folder, because a root service shouldn't run code from a folder in
your home directory.

## The live view

`./gpu-fan-status -w` opens a screen of its own: the hottest GPU, the fan duty and the
board power as big digits, braille line graphs of the last 4 minutes / 10 minutes / hour,
a card per GPU (temperature, power, utilization, SM clock, throttle reason, each with a
sparkline), and the configured fan curve with **you are here** marked on it. It is as
read-only as the plain report - it runs the same reading functions, in a thread of its own.

| Key | |
|---|---|
| `q` | quit (`Ctrl+C` too) |
| `Space` | freeze the view |
| `g` | the right-hand graph: fan duty, fan RPM, power draw, GPU utilization |
| `1` `2` `3` | the graphs' window: 4 minutes, 10 minutes, an hour |

Run it with `sudo` for the headline fan RPM, the live duty and the BMC's fan mode; without
root it shows the duty from the service log and the window's peak temperature instead.

It needs Textual, in a `.venv` next to the script:

```bash
./setup-live-view.sh      # Textual 8.2.8 into .venv, from PyPI
```

On a machine with no internet, drop the wheels into `wheels/` next to the script (or point
`GPU_FAN_WHEELS` at a folder of them) and it installs from there instead.

`gpu-fan-status` re-runs itself with that interpreter when it needs it, under `sudo` as
well, so nothing else on the system is touched. Without it, `-w` falls back to the
plain-text refresh, and `./gpu-fan-status` on its own never needs it at all.

The look is borrowed from the author's dashboard theme, laid out like
[binsider](https://github.com/orhun/binsider), so both look like one thing in the same
terminal. `fan_look.py` carries the palette and the big-digit font, so nothing outside this
folder is needed to render it.

### On the machine's own screen

Over SSH the live view just works: your terminal emulator supplies the font and the
colours. On the machine's **text console** (the VT on its own monitor) it does not, and
the fix lives in [`console/`](console/README.md):

```bash
cd console && ./install-console.sh          # idempotent; re-run any time
```

**Re-run it after `setup-live-view.sh`**, which rebuilds `.venv` and drops the Rich hook.
Resolution changes need a reboot; everything else applies immediately.

Three things break on a VT, for one underlying reason — the kernel console is not a
terminal emulator. It renders from a single bitmap page capped at **512 glyphs** and shows
at most **16 colours** at once. Both are kernel limits, not settings.

| Symptom | Cause | Fix |
|---|---|---|
| Graphs render as random letters | `fan_look.py` builds braille at runtime (`chr(0x2800 \| bits)`), and no stock console font has it — or the eighth-blocks the bars use. Unmapped codepoints fall through to raw font positions. | A font with all 256 braille patterns and all 32 block elements drawn in, paid for by dropping Greek and Cyrillic. |
| Colours look generic | Rich downgrades truecolor to the nearest of 16 standard ANSI values. | A 16-colour palette built from `fan_look.py`'s own theme constants, so 10 of the app's colours land on their **exact** RGB. |
| The title gradient is flat | Rich matches against its built-in ANSI table, which is wrong once the palette is custom, so the ramp collapses onto 2 slots. | A venv hook hands Rich the palette actually loaded from `/etc/vtrgb`: 2 steps → **4**. |

Searching the source for the braille will find nothing — it only exists at runtime. That
and the other non-obvious parts are written up in [`console/README.md`](console/README.md).

What you cannot get on a VT: truecolor (16 colours is the hard cap, so the gradient is 4
steps rather than smooth) and emoji (they degrade to a nearest-shape glyph). Do **not** set
`COLORTERM=truecolor` to chase it — the kernel's own approximation is measurably worse,
rendering the violet→sky gradient as green and cyan.

## Day-to-day

```bash
./gpu-fan-status                           # every GPU's temp + the fan's duty (read-only)
sudo ./gpu-fan-control --check             # settings, target, live fan state; writes nothing
sudo ./gpu-power-limit cool                # cool mode: cap every GPU at 150 W (-i 1: only GPU1)
sudo ./gpu-power-limit full                # back to the full 250 W
sudo ./gpu-fan-status -w                   # the live view, incl. RPM and fan mode, every 2 s
sudo journalctl -fu gpu-fan-control        # live log: duty changes, config reloads, errors
systemctl status gpu-fan-control
sudo systemctl stop gpu-fan-control        # the fan goes back to the board's own control
sudo systemctl start gpu-fan-control
sudo ./gpu-fan-detect                      # what controls the fans on this machine
sudo ipmitool sdr type Fan                 # current RPMs (BMC backends)
sudo ipmitool raw 0x3a 0xda                # ASRock: live duty per header (hex %)
```

## Safety behaviour

- **GPU temperature unreadable** (driver issue, `nvidia-smi` error, or `[N/A]` for
  any one GPU): the fan goes to `failsafe_duty` (100 %).
- **A GPU disappears** (seen earlier, no longer listed by `nvidia-smi`, e.g. fell off
  the bus): the fan goes to failsafe until the GPU is back. If it was removed on
  purpose, restart the service.
- **Service stopped, or the process crashes**: the fan is handed back to the board's own
  control — BMC Default mode for the `asrock` backend, the fan mode the board was in at
  startup for `supermicro`, the previous `pwmN_enable` for `hwmon`, `command.release` for
  `command`. The controller does this itself on SIGTERM/SIGINT and on an unhandled error
  (after any `ipmitool` call in progress has been stopped), and the unit's
  `ExecStopPost` runs `--restore` as a backstop. systemd then restarts the service
  after 5 s (`Restart=always`).
- **Fan stops or stalls** (tach below `fan_min_rpm`): FAN FAULT logged, fan held at 100 %
  until it reads OK for 3 checks.
- **GPUs overheat for any reason**: `gpu-thermal-guard` caps power at 80 °C, and at
  85 °C kills all GPU processes and latches (see [Overheating failsafe](#overheating-failsafe)).
- **Something hands the fan back to the board** (the BMC web UI, a BMC reset, another fan
  tool): the controller takes it over again on its next check — it re-asserts control every
  check, not just at startup.
- **A fan write fails**: the error is logged and the duty is resent on the next check. The
  hold/ramp state is kept, so a failed write never makes the fan drop straight to the curve
  value.
- **The settings file names a target this machine can't drive** (wrong backend, missing pwm,
  `fan` out of range): the file is rejected with the reason logged, and the last good
  settings keep running. At install time the same check stops `install.sh` instead.
- **Changing the target fails to release the old one**: the controller keeps driving the old
  one and retries on the next change. Switching backend, header, zone, pwm, `tach` or the
  duty limits all go through the same path: release the old target, take over the new one.

## How it talks to the hardware

Each backend in `fan_backend.py` writes a duty one way. All of them are in-band — no network
access to the BMC and no BMC password.

### `asrock` — ASRock Rack OEM IPMI (this machine)

Through `ipmitool raw`, netfn `0x3a`. Each command takes or returns 16 bytes,
one per fan header (FAN1 first):

| Command | Meaning |
|---|---|
| `0x3a 0xd9` | get mode per header (0 = Default, 1 = Manual, 2 = Customize) |
| `0x3a 0xd8 <16 bytes>` | set mode |
| `0x3a 0xd7` | get manual duty per header (%) |
| `0x3a 0xd6 <16 bytes>` | set manual duty; values below 20 % are rejected (`rsp=0xcc`) |
| `0x3a 0xda` | live duty currently applied to each header |

The controller always reads the current 16 values, changes only its own header's byte,
and writes them back, so other headers keep their settings. It sets the duty *before*
switching to Manual, so the fan never briefly drops to a stale stored duty.

The same settings are in the BMC web UI (`https://<bmc-ip>` → Settings → Fan Settings),
which uses the API `/api/asrr/settings/{get,set}fancontrolmode` and
`{get,set}fandutymanualmode`.

### `supermicro` — Supermicro OEM IPMI

Through `ipmitool raw`, netfn `0x30`. Verified against community documentation for the
X9/X10/X11 and H11 families rather than on hardware here, so check it with
`gpu-fan-detect --spin-all` on the board itself.

| Command | Meaning |
|---|---|
| `0x30 0x45 0x00` | get the board's fan mode |
| `0x30 0x45 0x01 <mode>` | set it: 0 Standard, 1 Full, 2 Optimal, 4 Heavy IO |
| `0x30 0x70 0x66 0x00 <zone>` | get the zone's duty (%) |
| `0x30 0x70 0x66 0x01 <zone> <duty>` | set it, `0x00`–`0x64` |

Two differences from ASRock Rack change how it behaves:

- **Duty is per zone, not per header.** Zone 0 is the numbered headers (`FAN1`–`FAN5` on an
  H11SSL-i), zone 1 the lettered ones (`FANA`, `FANB`). Every fan in the driven zone follows
  the curve, so the GPU fan wants a zone to itself — `FANA`/`FANB` with `zone = 1`.
- **The mode is board-wide.** A manual duty only sticks while the board is in Full mode, so
  taking over switches the whole board to Full and the zones we *don't* drive run on the
  BMC's Full-mode curve instead of the Standard one. On exit the mode goes back to whatever
  it was when the service started (`restore_mode` overrides that). Because switching to Full
  resets the zone's duty, this backend writes the **mode first and the duty second** — the
  opposite order to `asrock`.

And one trap: each fan has a *lower critical threshold* in the BMC's sensor table, and a tach
reading below it makes the BMC declare the fan dead and blast every fan to full. The GPU then
cools, the controller lowers the duty, and round it goes. `min_duty` therefore defaults to
30 % for this backend. `sudo ipmitool sensor get FANA` shows the thresholds.

### `hwmon` — Super I/O through sysfs

`pwmN` takes 0–255 (written as a percentage of it) and `pwmN_enable` picks who drives it
(1 = manual, 2+ = the chip's own automatic modes, restored on exit). No BMC needed. The
catch is that a pwm output existing says nothing about whether it is wired to a header —
on this machine `nct6798` offers `pwm1..7` and not one of them does anything. A spin test is
the only honest check. Stop `fancontrol`, CoolerControl or any vendor daemon first: nothing
stops two programs fighting over the same output.

### `command` — your own command lines

`take_over`, `set_duty`, `release`, and optionally `live_duty` and `rpm`, as templates with
`{duty}` substituted. Each is split the way a shell would split it and then run **without**
one, so there are no pipes, no redirection and no `$VARS` — put anything that needs those in
a script and call that. See [`examples/fan-curve.command.toml`](examples/fan-curve.command.toml).

## Another motherboard

Everything above the backend — the curve, the hold, the ramp-down, failsafe, fault detection,
the guard, the status tool and the live view — is hardware-independent. What isn't: which
fan cools the GPU, and how the board lets software set a speed.

```bash
sudo gpu-fan-detect              # BMC, OEM commands it answers, fan sensors, hwmon, GPUs
sudo gpu-fan-detect --spin-all   # drive each header/zone in turn; see which tach follows
gpu-fan-detect --write-config fan-curve.toml.new
sudo gpu-fan-control --check     # does this machine accept these settings?
```

`--spin-all` is the useful one: header numbering differs board to board, and a fan that
ignores PWM looks exactly like the wrong header. It drives each candidate to full, then to
its minimum, reports which tachometers followed, and puts every one back as it was —
including on `Ctrl+C`. About 25 s per target.

Then copy the matching file out of [`examples/`](examples/), set the target and `tach`, and
**retune the curve**: the one here was measured on a 250 W V100 behind one high-static-pressure
fan in this chassis, and a different fan in a different box behaves differently. See
[INSTALL.md](INSTALL.md) for the whole sequence, and [Tuning the curve](#tuning-the-curve)
for what the knobs do.

## Hardware notes

- The fan headers are 6-pin. From the manual: 1 GND, 2 +12 V, 3 tach, 4 PWM,
  5 SENSOR (second tach for dual-rotor fans, read as `FANx_2`), 6 NC. Standard
  3- and 4-pin fans plug onto pins 1–4.
- Speed control is **PWM only**. A 3-pin fan ignores it and runs at full voltage.
  FAN3 (~1800 RPM) behaves this way.
- Configured with `fan = 2`. To drive a different header, change `fan` in the
  settings file. The controller hands the old header back to Default and takes over
  the new one.
- A **Supermicro H11SSL-i** (the other board this has been set up for) has seven headers:
  `FAN1`–`FAN5` in zone 0 and `FANA`/`FANB` in zone 1, all 4-pin. Put the GPU fan on `FANA`
  or `FANB` so only it follows the curve.

## Uninstall

```bash
sudo ./uninstall.sh            # stop everything, remove it, keep the settings files
sudo ./uninstall.sh --purge    # also remove the settings and the guard's latch
```

Stopping the fan service hands the fan back to the board's own control, and stopping
`gpu-power-limit` puts the GPUs back on their full default limit. By hand, if you'd rather:

```bash
sudo systemctl disable --now gpu-fan-control       # fan goes back to the board's control
sudo systemctl disable --now gpu-power-limit       # GPU back to its full power limit
sudo systemctl disable --now gpu-thermal-guard
sudo rm /etc/systemd/system/gpu-{fan-control,power-limit,thermal-guard}.service
sudo rm /usr/local/bin/gpu-{fan-control,fan-status,fan-detect,power-limit,thermal-guard}
sudo rm -r /usr/local/lib/gpu-fan-control /var/lib/gpu-thermal-guard
sudo rm -f /etc/gpu-fan-control.conf /etc/modules-load.d/gpu-fan-control.conf
sudo systemctl daemon-reload
```
