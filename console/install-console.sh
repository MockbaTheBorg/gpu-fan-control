#!/bin/bash
# Set up this machine's text console (the VT on its own monitor) so that
# "gpu-fan-status -w" renders properly on it: the right glyphs, the theme's
# colours, and a resolution whose text is readable from a chair.
#
# Safe to re-run: every step is idempotent.  Run it again after
# setup-live-view.sh, which rebuilds .venv and drops the Rich hook.
#
# Nothing here is needed to run gpu-fan-status over SSH.  See README.md.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

RES=1024x768
DO_FONT=1 DO_PALETTE=1 DO_GRUB=1 DO_VENV=1
while [ $# -gt 0 ]; do
  case "$1" in
    --res)        RES="$2"; shift 2 ;;
    --res=*)      RES="${1#*=}"; shift ;;
    --no-font)    DO_FONT=0; shift ;;
    --no-palette) DO_PALETTE=0; shift ;;
    --no-grub)    DO_GRUB=0; shift ;;
    --no-venv)    DO_VENV=0; shift ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \?//'
      echo
      echo "usage: $0 [--res WxH] [--no-font] [--no-palette] [--no-grub] [--no-venv]"
      echo "  --res   console resolution, must be one the monitor reports (default $RES)"
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }
backup() { [ -e "$1.orig" ] || sudo cp -a "$1" "$1.orig"; }

# Unattended runs (no tty to type a password into): export SUDO_ASKPASS to a
# helper that prints it, and every sudo below picks it up.
if [ -n "${SUDO_ASKPASS:-}" ]; then sudo() { command sudo -A "$@"; }; fi

# ---------------------------------------------------------------- font -------
if [ "$DO_FONT" = 1 ]; then
  say "console font"
  python3 make-font.py
  python3 verify-font.py >/dev/null || { python3 verify-font.py; exit 1; }
  echo "  verify-font.py: OK"
  sudo mkdir -p /usr/local/share/consolefonts
  sudo install -m 644 Uni2-Fixed16-blocks.psf.gz /usr/local/share/consolefonts/
  # setupcon's findfile searches /usr/local/share/consolefonts before /usr/share,
  # so a console-setup package upgrade cannot clobber this.
  if [ -f /etc/default/console-setup ]; then
    backup /etc/default/console-setup
    sudo sed -i '/^FONT=/d' /etc/default/console-setup
    echo 'FONT="Uni2-Fixed16-blocks.psf.gz"' | sudo tee -a /etc/default/console-setup >/dev/null
    sudo setupcon --save 2>/dev/null || echo "  (setupcon reported a problem, continuing)"
    echo "  FONT= set in /etc/default/console-setup, cache rebuilt"
  else
    echo "  !! /etc/default/console-setup is missing: install the console-setup package"
  fi
  loaded=0
  for t in /dev/tty[1-6]; do
    [ -c "$t" ] || continue
    sudo setfont -C "$t" /usr/local/share/consolefonts/Uni2-Fixed16-blocks.psf.gz \
      2>/dev/null && loaded=$((loaded + 1))
  done
  echo "  loaded live on $loaded VT(s)"
fi

# ------------------------------------------------------------- palette -------
if [ "$DO_PALETTE" = 1 ]; then
  say "console palette (16 colours, the most the kernel VT can show)"
  python3 make-palette.py
  sudo install -m 644 vtrgb.gpufan /etc/console-setup/vtrgb.gpufan
  sudo update-alternatives --install /etc/vtrgb vtrgb /etc/console-setup/vtrgb.gpufan 100 \
    >/dev/null 2>&1 || true
  sudo update-alternatives --set vtrgb /etc/console-setup/vtrgb.gpufan >/dev/null 2>&1 || true
  echo "  /etc/vtrgb -> $(readlink -f /etc/vtrgb)"
  if [ -c /dev/tty0 ]; then
    sudo setvtrgb /etc/vtrgb && echo "  applied live"
  fi
  # setvtrgb.service ships enabled with kbd and reapplies /etc/vtrgb at boot.
  sudo systemctl is-enabled setvtrgb.service >/dev/null 2>&1 \
    && echo "  setvtrgb.service enabled: survives reboot" \
    || echo "  !! setvtrgb.service is not enabled: sudo systemctl enable --now setvtrgb.service"
fi

# ---------------------------------------------------------- resolution -------
if [ "$DO_GRUB" = 1 ]; then
  say "console resolution"
  conn=""
  for s in /sys/class/drm/card*-*/status; do
    [ "$(cat "$s" 2>/dev/null)" = connected ] || continue
    conn=$(basename "$(dirname "$s")"); conn=${conn#card*-}
    modes=$(dirname "$s")/modes
    break
  done
  if [ -z "$conn" ]; then
    echo "  no connected display found, skipping (headless? use --no-grub)"
  elif ! grep -qx "$RES" "$modes" 2>/dev/null; then
    echo "  !! $RES is not a mode $conn reports. Available:"
    sed 's/^/       /' "$modes" | sort -u
    echo "     re-run with --res <one of those>, or --no-grub"
    exit 1
  else
    echo "  connector $conn, mode $RES"
    backup /etc/default/grub
    # video= pins the mode for the DRM driver; GRUB_GFXMODE makes the boot menu
    # and early console match, and GFXPAYLOAD=keep carries it into the kernel.
    sudo sed -i \
      -e "s/\bvideo=[^ \"]*/video=$conn:$RES/g" \
      -e "s/^GRUB_GFXMODE=.*/GRUB_GFXMODE=${RES}x32,$RES/" \
      /etc/default/grub
    grep -q '^GRUB_GFXMODE=' /etc/default/grub \
      || echo "GRUB_GFXMODE=${RES}x32,$RES" | sudo tee -a /etc/default/grub >/dev/null
    grep -q '^GRUB_GFXPAYLOAD_LINUX=' /etc/default/grub \
      || echo 'GRUB_GFXPAYLOAD_LINUX=keep' | sudo tee -a /etc/default/grub >/dev/null
    if ! grep -q "video=$conn:$RES" /etc/default/grub; then
      sudo sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=\"/&video=$conn:$RES /" /etc/default/grub
    fi
    grep -E '^(GRUB_CMDLINE_LINUX_DEFAULT|GRUB_GFXMODE|GRUB_GFXPAYLOAD_LINUX)=' \
      /etc/default/grub | sed 's/^/    /'
    sudo update-grub 2>&1 | grep -iE '^(done|error)' || true
    echo "  !! takes effect at the next reboot"
  fi
fi

# ------------------------------------------------------- rich venv hook ------
if [ "$DO_VENV" = 1 ]; then
  say "Rich palette hook (gradient: 2 steps -> 4)"
  sp=$(echo ../.venv/lib/python3*/site-packages)
  if [ -d "$sp" ]; then
    install -m 644 gpufan_vt_palette.py "$sp/gpufan_vt_palette.py"
    install -m 644 zz_gpufan_vt_palette.pth "$sp/zz_gpufan_vt_palette.pth"
    echo "  installed into $(cd "$sp" && pwd)"
    # A .pth, not sitecustomize.py: Ubuntu ships its own sitecustomize in
    # /usr/lib/python3*/ which shadows anything of that name in the venv.
    "$(cd .. && pwd)/.venv/bin/python" -c "
import gpufan_vt_palette as p
print('  self-test: on a VT ->', p._on_linux_vt(), '| /etc/vtrgb slot12 =', p._load_vtrgb()[12])
" || echo "  !! hook self-test failed"
  else
    echo "  no .venv found, skipping (run ./setup-live-view.sh first)"
  fi
fi

say "done"
echo "Check it with:  gpu-fan-status -w        (on the machine's own screen)"
[ "$DO_GRUB" = 1 ] && echo "Resolution changes apply after:  sudo reboot"
exit 0
