# Installing gpu-fan-control on a new machine

This drives **one chassis fan from the GPU temperature**, for passively cooled cards
(Tesla V100 and friends) that have no fan of their own. The board's own fan control can't
see the GPU, so a service reads `nvidia-smi` and sets the fan duty itself.

Two things are specific to your machine: **which fan** cools the GPU, and **how the board
lets software set a fan speed**. `gpu-fan-detect` answers both. Work through this once and
you can forget about it.

> Everything here is reversible. `sudo ./uninstall.sh` puts the fan back under the board's
> own control and the GPUs back on their default power limit.

---

## 0. What you need

| | |
|---|---|
| Linux with the NVIDIA driver | `nvidia-smi` must list the GPU(s) |
| Python 3.11 or newer | no packages needed; `tomllib` is in the standard library |
| `ipmitool` | only for the BMC backends: `sudo apt install ipmitool` |
| A 4-pin (PWM) fan | a 3-pin fan ignores speed control and runs flat out |

The GPU fan should be on a header the board can control, and ideally on one **nothing else
needs**. On a Supermicro board that means `FANA` or `FANB` — see step 2.

```bash
unzip gpu-fan-control-*.zip
cd gpu-fan-control-*
python3 tests/test_loop.py && python3 tests/test_backends.py    # 25 checks, no hardware needed
```

The tests take a second and need nothing but Python: they drive the control loop and every
backend against stubs. If they pass, anything that goes wrong from here is about this
machine's hardware, not about the code — which is a useful thing to know up front.

Unpack it somewhere permanent: `/opt/gpu-fan-control` or a folder in your home is fine, but
the installed services read their settings from wherever you leave it, so don't unpack it
in `/tmp` and don't move it afterwards. (`sudo ./install.sh --config-dir /etc/gpu-fan-control`
copies the settings to `/etc` instead, if you'd rather they didn't live next to the code.)

## 1. See what the board offers

```bash
sudo gpu-fan-detect        # or ./gpu-fan-detect before installing
```

Run it with `sudo`: the IPMI device is root-only, and without root it can't tell a board
with no BMC from a board it isn't allowed to ask. It prints the GPUs, what the BMC says
about itself, which OEM fan commands it answers, every fan sensor with its current RPM, any
hwmon PWM outputs, and which backend to use.

| Backend | When |
|---|---|
| `supermicro` | Supermicro BMC. Duty per **zone**, mode board-wide. |
| `asrock` | ASRock Rack BMC. Duty and mode per **header**. |
| `hwmon` | No BMC: the Super I/O chip through `/sys/class/hwmon` (what `fancontrol` uses). |
| `command` | Anything else: you supply the command lines. See `examples/fan-curve.command.toml`. |

If it finds no BMC but the board should have one, load the drivers and try again:

```bash
sudo modprobe ipmi_devintf ipmi_si
```

## 2. Find out which fan cools the GPU

This is the part worth doing properly. The header numbering on your board has nothing to do
with anyone else's, and a fan that doesn't respond to PWM looks exactly like the wrong
header.

```bash
sudo gpu-fan-detect --spin-all
```

It takes each header or zone in turn, drives it to full, then to its minimum, and reports
which tachometers followed. About 25 seconds per target, and each one is put back as it was
afterwards — including on `Ctrl+C`. Fan noise will go up and down while it runs; nothing is
left changed.

The one that moved is the one to configure. If you're not sure which fan is physically on
the GPU, run it again with your hand near the card.

### Supermicro: zones, not headers

A Supermicro BMC cannot set one header on its own — it sets a **zone**:

* **zone 0** — the numbered headers, `FAN1`…`FAN5`
* **zone 1** — the lettered headers, `FANA`, `FANB`

Every fan in the zone you drive follows the GPU curve. So put the GPU fan on **`FANA` or
`FANB`** and use `zone = 1`: the CPU and case fans on the numbered headers stay under the
BMC's own control. If the GPU fan shares zone 0 with the CPU fan it still works, but the
whole machine gets loud whenever the GPU warms up.

One more Supermicro quirk, worth knowing before you lower `min_duty`: each fan has a *lower
critical threshold* in the BMC's sensor table, and if a tach reads below it the BMC decides
the fan has failed and blasts **every** fan to full. The GPU then cools, this service lowers
the duty, and it happens again — a loop that sounds like the machine breathing. That's why
`min_duty` defaults to 30 % here. Check what your board expects with
`sudo ipmitool sensor get FANA`.

## 3. Write the settings file

```bash
cp examples/fan-curve.supermicro.toml fan-curve.toml     # or .asrock-rack / .hwmon / .command
$EDITOR fan-curve.toml
```

or let the detector write a starting point:

```bash
gpu-fan-detect --write-config fan-curve.toml
```

Set `backend`, the `zone`/`fan` you found in step 2, and `tach` (the sensor name from the
detector output, so a stalled fan gets noticed). **Leave the curve alone for now** — step 5
is where you tune it.

Check it before installing anything:

```bash
sudo ./gpu-fan-control --check
```

It prints the target, the duty range, the curve, the current fan mode and the tach readings,
and tells you plainly if the board won't let it read the fan. It never changes a setting.

## 4. Install

```bash
sudo ./install.sh
```

It copies the programs to `/usr/local/bin`, records where the settings live, generates the
three systemd units with real paths, asks for the IPMI modules at boot, and starts the
service. It **refuses to start** if `--check` says the settings aren't usable on this
machine, and it never overwrites a settings file you already have.

```bash
gpu-fan-status                        # GPU temps, the curve target, the fan
sudo journalctl -fu gpu-fan-control   # live: duty changes, reloads, faults
```

Three services get installed:

| Service | Does |
|---|---|
| `gpu-fan-control` | the fan curve (this one) |
| `gpu-thermal-guard` | overheating failsafe: caps GPU power at 80 °C, kills GPU jobs and latches at 85 °C |
| `gpu-power-limit` | caps the GPUs at `cool_watts` (150 W) at boot — much less heat for a little less speed. `sudo ./install.sh --no-power-limit` skips it. |

## 5. Tune the curve

The curve that shipped was measured on a 250 W V100 behind one high-static-pressure fan in
a particular chassis. Yours is a different fan in a different box, so the numbers are a
starting point, not an answer.

Load the GPU and watch what happens:

```bash
sudo ./setup-live-view.sh    # once: Textual into .venv, for the live view
sudo ./gpu-fan-status -w     # big digits, graphs, the curve with "you are here"
./gpuburn.py                 # in another shell: load every GPU for four minutes
```

`gpuburn.py` is the load. It needs no CUDA toolkit, no compiler and no pip packages, and
it changes no setting - it drives the GPUs through the driver's own API and stops at
78 C, two degrees before `gpu-thermal-guard` would start capping power. It does not reach
the board's full power limit (a 150 W-capped V100 draws about 118 W under it), so treat
the temperature it reaches as a firm lower bound and confirm the curve against your real
workload before trusting its headroom.

Edit `fan-curve.toml` and save it — the running service picks the change up within seconds,
no restart. Aim for the hottest GPU settling **at least 5 °C below its max operating
temperature** under sustained full load (83 °C on a V100, so 76–78 °C is comfortable). If
it's noisier than you'd like in the 60–75 °C range, lower the middle points; if the GPU
creeps past 80 °C, raise them.

`gpu-thermal-guard` is watching the whole time, so a curve that turns out too lazy caps the
GPUs rather than cooking them.

## If something goes wrong

| Symptom | What it means |
|---|---|
| `--check` can't read the fan mode | Wrong backend for this board, or the IPMI modules aren't loaded. Re-run `sudo gpu-fan-detect`. |
| Duty is set but the fan doesn't change speed | A 3-pin fan, or the wrong header/zone. `sudo gpu-fan-detect --spin-all`. |
| All fans surge up and down every minute or so | The BMC's low-RPM panic. Raise `min_duty` (try 40), or raise the first curve point. |
| `FAN FAULT` in the log | A watched tach read under `fan_min_rpm` while being driven. The fan is held at 100 %. Check the fan is spinning; if `tach` names a header with nothing plugged into it, fix `tach`. |
| Fan goes to 100 % and stays there | `failsafe_duty`: a GPU temperature couldn't be read. `journalctl -u gpu-fan-control` says which. |
| Service restarts in a loop | `systemctl status gpu-fan-control`; the settings error is logged in one line. |

The log is the first place to look, always:

```bash
sudo journalctl -u gpu-fan-control -n 50
sudo journalctl -p crit -u gpu-fan-control -u gpu-thermal-guard   # faults and trips only
```

`README.md` has the rest: what each file does, how the hold and ramp-down work, the
overheating failsafe, the live view, and the exact commands each backend sends.
