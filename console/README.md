# console/ — running the live view on the machine's own screen

`gpu-fan-status -w` is built for a terminal emulator. On the machine's **text console**
(the VT on its own monitor, `tty1`–`tty6`) three things break, and this folder fixes all
three. Nothing here is needed over SSH.

```bash
./install-console.sh                 # all of it, idempotent
./install-console.sh --res 800x600   # a different resolution
./install-console.sh --no-grub       # leave the resolution alone
```

Re-run it freely. **Run it again after `setup-live-view.sh`**, which rebuilds `.venv` and
drops the Rich hook. Resolution changes need a reboot; the rest applies immediately.

## What breaks, and why

### 1. The graphs come out as random letters

`fan_look.py` draws its curve and line graphs with **braille**, generated at runtime:

```python
BRAILLE = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
...
t.append(chr(0x2800 | bits), colour)
```

Grepping the source for literal non-ASCII finds **no** braille — the codepoints only exist
at runtime, which makes this easy to misdiagnose. The Linux VT renders from one bitmap
page **capped at 512 glyphs**, and a codepoint absent from that page's unicode map falls
through to a raw font position, so you get letters. Stock `Uni2-Fixed16` also lacks every
Block Element past `█ ░ ▒`, and **no** font in `/usr/share/consolefonts` has the
eighth-blocks the bars and sparklines need.

`make-font.py` spends the 512-glyph budget on what the console actually draws:

| | slots | |
|---|---|---|
| ASCII + Latin-1 + box drawing U+2500–257F | 212 | kept from the stock font |
| Block elements U+2580–259F | 32 | **drawn**, geometrically → pixel exact |
| Braille U+2800–28FF | 256 | **drawn**, all of them, 2×4 dots → full resolution |
| `⏸` U+23F8 (`fan_dash.py`) | 1 | drawn |
| spare | 11 | |

Greek and Cyrillic pay for it. Anything still missing (emoji, exotic arrows, spinner
shapes) gets a **many-to-one unicode mapping** onto the nearest glyph present — 39 of
them, costing zero slots, because PSF lets any number of codepoints share one glyph. So
nothing degrades to garbage.

It installs to `/usr/local/share/consolefonts/`, which `setupcon`'s `findfile` searches
**before** `/usr/share`, so a `console-setup` upgrade cannot clobber it.

### 2. The colours are wrong

The kernel VT shows **16 colours at once**, full stop — against truecolor's 16.7 million.
That is a kernel limit, not a setting, and it cannot be lifted. What *is* ours to choose is
which 16.

`make-palette.py` reads the theme constants straight out of `../fan_look.py` (so it cannot
drift) and writes `vtrgb.gpufan`, mapping each slot to the theme's own value. Slots were
picked from the targets Rich's downgrade measurably lands on, so `ACCENT`, `ACCENT2`, `OK`,
`WARN`, `DANGER`, `OK_TEXT`, `ACCENT_TEXT`, `INK`, `INK_SOFT` and `FAINT` all render at
their **exact** RGB instead of a generic ANSI approximation. Slots 1–3 stay recognisably
red/green/yellow, just darkened, so `ls` and `systemd` still read correctly on the console.

It installs as an `update-alternatives` entry for `/etc/vtrgb`. The `setvtrgb.service` that
ships enabled with `kbd` reapplies it at boot.

> Do **not** set `COLORTERM=truecolor` to try to get more. The kernel's own truecolor
> approximation is worse than Rich's: measured against `/dev/vcsa1`, it renders the
> violet→sky gradient as **green and cyan**. Let Rich do the mapping.

### 3. The gradient is flat

`gradient()` interpolates `ACCENT` → `ACCENT2` per column. Rich picks the nearest of 16 by
comparing against its own built-in table of standard ANSI values — which is wrong here,
because this console runs a custom palette. So the whole ramp collapses onto 2 slots.

`gpufan_vt_palette.py` hands Rich the palette that is **actually loaded**, read from
`/etc/vtrgb` rather than hardcoded:

| | gradient steps | slots |
|---|---|---|
| stock Rich | 2 | 12, 14 |
| with the hook | **4** | 12 → 5 → 4 → 14 (violet → mid-violet → mid-blue → sky) |

It is loaded by `zz_gpufan_vt_palette.pth`, **not** `sitecustomize.py`: Ubuntu ships its own
`/usr/lib/python3*/sitecustomize.py`, which sits earlier on `sys.path` and shadows anything
of that name in a venv. A `.pth` whose line starts with `import` is executed during site
initialisation — before anything imports Rich, which matters because Rich caches colour
downgrades.

The hook only acts when a standard stream is a real VT (`/dev/tty1`–`tty63`), so it is a
no-op over SSH, where truecolor is used and no downgrade happens. Every failure path is
silent: the dashboard must still run if the hook is wrong.

### Resolution

`install-console.sh` detects the connected DRM connector, checks the mode against what the
monitor reports, then sets both `video=<connector>:<mode>` in `GRUB_CMDLINE_LINUX_DEFAULT`
(for the DRM driver) and `GRUB_GFXMODE` (so the boot menu matches), with
`GRUB_GFXPAYLOAD_LINUX=keep` carrying it into the kernel. A bad `--res` is refused with the
list of modes the monitor actually reports.

At 8×16, `1024x768` gives a 128×48 grid and `800x600` gives 100×37.

## Files

| File | |
|---|---|
| `install-console.sh` | Does all of it. Idempotent. `--res`, `--no-font`, `--no-palette`, `--no-grub`, `--no-venv`. |
| `make-font.py` | Builds `Uni2-Fixed16-blocks.psf.gz` from the stock font. |
| `verify-font.py` | Asserts nothing went missing and rasterises a braille curve as ASCII art. Runs inside the installer; a failure aborts before anything is installed. |
| `psf1.py` | PSF1 font reader/writer. |
| `make-palette.py` | Builds `vtrgb.gpufan` from `../fan_look.py`'s theme constants. |
| `gpufan_vt_palette.py` | The Rich hook. Installed into `.venv`. |
| `zz_gpufan_vt_palette.pth` | Loads the hook at interpreter startup. |

Generated (not checked in by hand): `Uni2-Fixed16-blocks.psf.gz`, `vtrgb.gpufan`.

## Unattended / scripted runs

`install-console.sh` calls `sudo`. With no tty to type into, point `SUDO_ASKPASS` at a
helper that prints the password and it is picked up automatically:

```bash
SUDO_ASKPASS=/path/to/askpass.sh ./install-console.sh
```

## Checking it

```bash
python3 make-font.py && python3 verify-font.py   # font proof, without installing
gpu-fan-status -w                                # on the machine's own screen
```

Quick glyph smoke test on the console:

```bash
printf '%s\n' '▁▂▃▄▅▆▇█ ▏▎▍▌▋▊▉█ ░▒▓█ ┌─┬┐├┼┤└┴┘ ⠁⠂⠄⡀⢁⣿ ° · – → ● ⏸' > /dev/tty1
```

## Undoing it

```bash
sudo sed -i '/^FONT=/d' /etc/default/console-setup && sudo setupcon --save
sudo update-alternatives --auto vtrgb && sudo setvtrgb /etc/vtrgb
rm -f ../.venv/lib/python3*/site-packages/{gpufan_vt_palette.py,zz_gpufan_vt_palette.pth}
```

`install-console.sh` keeps a one-time `.orig` of each file it edits
(`/etc/default/console-setup.orig`, `/etc/default/grub.orig`) from the first run.

## Known limits

- **16 colours.** Hard kernel cap. The gradient is 4 steps, not smooth.
- **512 glyphs.** No Greek/Cyrillic, no emoji; emoji degrade to a nearest-shape glyph.
- 512-glyph fonts cost the VT its bright *background* colours — the attribute bit selects
  the font page. The stock font was already 512, so nothing regressed.
- Assumes an 8×16 console font. Other sizes need the geometry in `make-font.py` adjusted.
