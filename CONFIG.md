# Deployed configuration

Two machines run this: **`silent`** (§1–8) and **`TRipper`** (§9). They have different
boards, different fans and different curves, so very little carries across.

Commissioned 2026-11-07. This records what was measured on this machine and what was
applied as a result, so the numbers in `fan-curve.toml` can be understood (and argued
with) later rather than taken on faith.

> The curve here is **specific to this chassis**. It was derived from measurements on
> this box, not copied from the development machine. Another machine needs its own
> pass through `gpu-fan-detect` and its own load test.

---

## 1. Hardware, as verified

| | |
|---|---|
| Board | Supermicro H11SSL-i, BMC firmware 1.52, IPMI product 0x1a03 |
| GPUs | 2 × Tesla V100 32 GB (`Tesla PG500-216`), driver 580.178.04 |
| GPU fan | ARCTIC S8038-10K on physical **FANB** |
| Backend | `supermicro`, **zone 1** |
| Other fans | FAN2 (zone 0, normally stopped), FAN4 1700 RPM, FAN5 1600 RPM |
| Empty headers | FAN1, FAN3, FANA |

**Zone 1 drives FANB and nothing else.** Established with a four-zone spin sweep
(`gpu-fan-detect --spin-all --dwell 5`): zone 1 at 100% put FANB at 9200 RPM and at 50%
at 5400 RPM, while FAN4/FAN5 held 1600 RPM throughout. Zone 0 drives FAN2 only. Zones 2
and 3 answer the duty GET but control nothing — zone 2's apparent "result" was inverted
(9900 RPM at the low step, 8600 at the high step), which is tach lag from the preceding
zone-1 test, not control.

**Restoring the board's own control works.** Every spin target reported
`restored: Optimal`, with the mode confirmed by readback. `ExecStopPost` on the service
runs `--restore`, and a `systemctl stop` was observed logging
`zone 1 handed back to the board's own fan control (Optimal)`.

This resolves the two hardware gates that the earlier (Codex) preparation left open:
`physical_pwm_zone_verified` and `safe_restoration_verified`. Both were treated there as
facts needing vendor documentation; both are directly measurable, and now measured.

## 2. Measured duty → fan speed

| Zone 1 duty | FANB |
|---|---|
| 20% (floor) | 2300 RPM |
| 50% | 5300 RPM |
| 62% | 6400 RPM |
| 79% | 8200 RPM |
| 100% | 9200–9900 RPM |

For reference the BMC's own Optimal mode idled FANB at 2200 RPM, so the 20% floor is
**as quiet as stock**. At 20% the fan still turns at 3.3× the 700 RPM stall threshold,
and driving the zone at 20% did not trigger the Supermicro low-RPM panic: FAN2/FAN4/FAN5
were unchanged over a two-minute observation.

## 3. Thermal behaviour under load

Load was 100% FP32/FP64 occupancy on both GPUs (a driver-API PTX loop; no CUDA toolkit
or pip packages involved), sustained 200–240 s per run.

| | Idle | Under load |
|---|---|---|
| GPU power | ~27 W each | ~118 W each |
| Hottest GPU | 35–39 °C | **61 °C** |
| Zone 1 duty | 20% | 59–62% |
| FANB | 2300 RPM | 6200–6500 RPM |

61 °C leaves **22 °C of headroom** to the V100's 83 °C maximum operating temperature.
The thermal guard never came near its 80 °C warn threshold, and no fan fault, duty
readback error or GPU-count failure was logged in any run.

For comparison, the curve inherited from the earlier preparation (floor 50%, 40 °C →
50%) held 57 °C under the same load but ran the fan at 79% / 8200 RPM continuously, and
never dropped below 5300 RPM even at idle. The curve below trades 4 °C for that noise.

**Caveat on worst case.** The load generator drew ~118 W of the 150 W cap — roughly 79%
of capped power. A real training workload will reach the cap and run hotter than 61 °C.
Extrapolating the measured rise (~0.2 °C/W) puts a fully capped load near 68 °C, where
the curve asks for ~75% duty and has plenty left. Worth one confirmation run under your
actual workload; the guard backstops it either way.

## 4. Settings applied

Config directory is **`/etc/gpu-fan-control`**, chosen with `install.sh --config-dir` so
the running root services do not read settings out of the group-writable project tree
under `/srv/ai/projects`.

### `/etc/gpu-fan-control/fan-curve.toml`

```toml
backend = "supermicro"
zone = 1
tach = ["FANB"]
interval = 5
min_duty = 20
max_duty = 100
failsafe_duty = 100
expected_gpu_count = 2

curve = [
  [45, 20], [50, 30], [55, 42], [60, 56], [65, 70], [70, 85], [75, 100],
]

hold_secs = 60
hold_band = 10
step_down = 2
fan_min_rpm = 700

[supermicro]
full_mode = 1
restore_mode = 2
```

Why these: the floor is 20% at your instruction, and measured safe above. The knee sits
well above the 61 °C operating point so normal work stays quiet, and the curve reaches
100% at 75 °C — 5 °C before the guard's warn threshold and 8 °C before its trip, so the
fan is already at maximum before any protective action is considered. `step_down = 2`
with `hold_secs = 60` means the fan eases off over a couple of minutes rather than
cycling. `expected_gpu_count = 2` makes a disappeared GPU a failsafe event rather than
something to run blind through.

The previous curve is preserved at `/etc/gpu-fan-control/fan-curve.toml.codex-bak`.

### `/etc/gpu-fan-control/power-limit.toml`

`cool_watts = 150` (shipped default, matches the requested cap). Both GPUs now report
`limit 150 W (cool), default 250 W`.

### `/etc/gpu-fan-control/thermal-guard.toml`

Shipped defaults: check every 2 s; at 80 °C warn and cap every GPU at its 100 W minimum;
release after 60 s below 75 °C; at 85 °C kill GPU processes and latch until
`sudo gpu-thermal-guard --reset`.

## 5. Services

All three installed by `install.sh`, generated against the real paths, **enabled at boot
and active**:

| Service | State | Does |
|---|---|---|
| `gpu-fan-control` | enabled / active | the curve above |
| `gpu-thermal-guard` | enabled / active | 80 °C power cap, 85 °C kill + latch |
| `gpu-power-limit` | enabled / active | applies the 150 W cap at boot |

Programs in `/usr/local/bin`, modules in `/usr/local/lib/gpu-fan-control`, config
location recorded in `/etc/gpu-fan-control.conf`, IPMI modules requested at boot via
`/etc/modules-load.d/gpu-fan-control.conf`.

## 6. Unprivileged access to status

`/etc/udev/rules.d/60-ipmi-gpu-fan.rules`:

```
KERNEL=="ipmi*", GROUP="gpu-fan-dev", MODE="0660"
```

`/dev/ipmi0` and `/dev/ipmi1` are now `root:gpu-fan-dev 0660`, so members of
`gpu-fan-dev` get live duty, mode and RPM from `gpu-fan-status` without sudo.

**This is not read-only.** IPMI is a single read/write interface; it cannot be opened for
reading alone. Anyone in `gpu-fan-dev` can therefore also issue fan writes. That was an
accepted trade for convenience here — add accounts to the group deliberately
(`sudo usermod -aG gpu-fan-dev <user>`, effective at next login).

`gpu-power-limit` with no arguments needs no privileges; *setting* a limit still needs
root, which is unchanged.

## 7. Everyday commands

```sh
gpu-fan-status                        # temps, curve target, live duty and RPM
gpu-fan-status -w                     # live view (Textual, vendored)
sudo journalctl -fu gpu-fan-control   # duty changes, reloads, faults
gpu-power-limit                       # show the caps
sudo gpu-power-limit full             # back to 250 W for a benchmark
sudo gpu-thermal-guard --status
```

Editing `/etc/gpu-fan-control/fan-curve.toml` is picked up within seconds — no restart.
Changing `backend`, `zone` or `expected_gpu_count` is deliberately refused on reload and
needs a stop, edit and start.

To undo everything: `sudo ./uninstall.sh` returns the fan to the board and the GPUs to
their default power limit; remove the udev rule separately.

## 8. Not yet verified

- **Boot behaviour.** The services are enabled but the machine has not been rebooted, so
  the boot ordering (IPMI modules → BMC reachable → service start) is untested in
  practice. Worth one scheduled reboot while someone is watching.
- **Worst-case thermal load**, as described in §3.
- Zones 2 and 3 answer duty GETs but control nothing; do not configure them.

---

## 9. Host `TRipper` (the development workstation)

Brought up to the same standard on 2026-11-07. Different board, different backend.

| | |
|---|---|
| Board | ASRockRack TRX40D8-2N2T, ASRock Rack BMC firmware 1.30 |
| GPUs | 2 × Tesla V100 (`Tesla PG500-216`) |
| Backend | `asrock`, header **FAN2**, tach **FAN2_1** |
| Other fans | FAN1_1 (4200 RPM) and FAN3_1 (2000 RPM) stay on the BMC's own curve |

ASRock sets duty and mode **per header**, not per zone, which is the main practical
difference from `silent`: driving FAN2 leaves the other headers alone by construction,
so there is no zone-sharing question to answer.

**Measured.** FAN2 at 20 % turns 2300 RPM and at 100 % turns 9900 RPM, and the header was
restored to `Default (BMC curve)` afterwards. During the sweep FAN1_1 appeared to "follow"
inversely (4300 → 3700 RPM) — that is the BMC reacting to the extra airflow, not this
header. Idle sits at 21 % / 2200 RPM with the GPUs at 41 °C.

**The curve is unchanged** (`40→20, 50→30, 60→45, 70→70, 78→100`). It was measured on this
chassis and had been holding these cards for days; there was no reason to touch it.

**What did change:**

- **`fan_min_rpm` 300 → 700.** The fan never turns slower than 2300 RPM when driven, so
  the old floor would have let a fan limping at 400 RPM pass as healthy. 700 keeps a 3.3×
  margin.
- **An explicit `backend = "asrock"`,** plus `tach = ["FAN2_1"]` and
  `expected_gpu_count = 2`. The old config named only `fan = 2` and let the backend be
  inferred, which the hardened loader refuses.
- **Config moved out of the git working tree.** The service previously read
  `/media/mockba/Raid/gpu-fan-control/fan-curve.toml` — so checking out a branch silently
  changed the live fan curve of a root service. It now reads `/etc/gpu-fan-control`.
- **Programs upgraded** from a 3 November build that predated `fan_backend.py` being a
  separate module; `gpu-fan-status` and `gpu-fan-detect` were not installed at all.
- **Persistence mode.** The 150 W cap read as applied while persistence was `Disabled`,
  so it was reverting whenever the cards went idle. Both GPUs now report `Enabled`.

**Unprivileged status** uses `GROUP="adm"` rather than a new `gpu-fan-dev` group, because
this account is already in `adm` and that avoided a re-login. The same caveat applies: it
grants BMC *write* too.

**Not verified here:** no sustained load test was run (deliberately — it is a desk
machine), and it has not been rebooted since the change, so boot ordering is untested.
The curve's behaviour under load is evidenced only by its three days of prior service.
