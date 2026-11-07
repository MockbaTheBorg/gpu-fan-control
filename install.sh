#!/bin/bash
# Install (or update) gpu-fan-control from this folder, and start it.
#
#   sudo ./install.sh                      install, settings stay in this folder
#   sudo ./install.sh --config-dir /etc/gpu-fan-control
#   sudo ./install.sh --no-start            copy everything, don't enable or start anything
#   sudo ./install.sh --no-power-limit      skip cool mode at boot
#   sudo ./install.sh --force               install even if the settings don't validate
#
# A curve change needs none of this: edit fan-curve.toml and the running service reloads
# it. Re-run install.sh after editing one of the scripts or a .service.in template.
#
# What it does:
#   * copies the programs to /usr/local/bin and fan_backend.py to /usr/local/lib/gpu-fan-control
#   * records the settings folder in /etc/gpu-fan-control.conf, so those copies find it
#   * generates the three systemd units from the .service.in templates with real paths
#   * asks for the IPMI kernel modules in /etc/modules-load.d if an IPMI backend is configured
#   * never overwrites an existing settings file: a tuned curve survives an update
set -euo pipefail

HERE="$(dirname "$(realpath "$0")")"
BINDIR=/usr/local/bin
LIBDIR=/usr/local/lib/gpu-fan-control
UNITDIR=/etc/systemd/system
CONFDIR="$HERE"
START=yes POWER=yes GUARD=yes FORCE=no

while [ $# -gt 0 ]; do
  case "$1" in
    --config-dir) CONFDIR="$(realpath -m "$2")"; shift 2 ;;
    --no-start) START=no; shift ;;
    --no-power-limit) POWER=no; shift ;;
    --no-guard) GUARD=no; shift ;;
    --force) FORCE=yes; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//; $d'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" != 0 ]; then
  echo "This needs root; re-running with sudo ..."
  exec sudo -- "$0" ${CONFDIR:+--config-dir "$CONFDIR"} \
       $([ "$START" = no ] && echo --no-start) \
       $([ "$POWER" = no ] && echo --no-power-limit) \
       $([ "$GUARD" = no ] && echo --no-guard) \
       $([ "$FORCE" = yes ] && echo --force)
fi

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ pre-flight
say "Checking this machine"
PY=$(command -v python3 || true)
[ -n "$PY" ] || die "python3 is not installed (3.11 or newer is needed for tomllib)"
"$PY" - <<'PYEOF' || die "python3 is older than 3.11, which gpu-fan-control needs for tomllib"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYEOF
echo "  python3        $("$PY" -V | cut -d' ' -f2)"
command -v nvidia-smi >/dev/null \
  || die "nvidia-smi is not installed: there is no GPU temperature to drive a fan from"
echo "  nvidia-smi     $(nvidia-smi --query-gpu=count --format=csv,noheader | head -1) GPU(s)"

BACKEND=$(sed -n 's/^[[:space:]]*backend[[:space:]]*=[[:space:]]*"\([a-z]*\)".*/\1/p' \
          "$CONFDIR/fan-curve.toml" 2>/dev/null | head -1)
[ -n "$BACKEND" ] || BACKEND=$(sed -n 's/^[[:space:]]*backend[[:space:]]*=[[:space:]]*"\([a-z]*\)".*/\1/p' \
          "$HERE/fan-curve.toml" 2>/dev/null | head -1)
[ -n "$BACKEND" ] || BACKEND=asrock
echo "  fan backend    $BACKEND"
case "$BACKEND" in
  asrock|supermicro)
    command -v ipmitool >/dev/null \
      || die "the $BACKEND backend needs ipmitool: apt install ipmitool (or dnf install ipmitool)"
    ls /dev/ipmi0 /dev/ipmi/0 /dev/ipmidev/0 >/dev/null 2>&1 \
      || warn "no /dev/ipmi* yet: the kernel modules are loaded below, but if this is not a
    board with a BMC, run gpu-fan-detect and pick a backend that fits it"
    ;;
esac

# ------------------------------------------------------------------ settings files
say "Settings"
mkdir -p "$CONFDIR"
for f in fan-curve.toml thermal-guard.toml power-limit.toml; do
  if [ -e "$CONFDIR/$f" ]; then
    echo "  keeping       $CONFDIR/$f"
  else
    install -m 644 "$HERE/$f" "$CONFDIR/$f"
    echo "  installed     $CONFDIR/$f"
  fi
done
if [ "$CONFDIR" = "$HERE" ]; then
  OWNER=$(stat -c %U "$HERE")
  [ "$OWNER" = root ] || warn "the services will read their settings from $HERE, which belongs to
    $OWNER. That is what --config-dir defaults to and it is fine for a machine you alone use,
    but anyone who can write fan-curve.toml can then influence a root service. Use
    --config-dir /etc/gpu-fan-control if that matters here."
fi

# ------------------------------------------------------------------ programs
say "Programs"
install -d -m 755 "$LIBDIR"
# fan_dash/fan_look are the live view: gpu-fan-status adds $LIBDIR to sys.path and
# imports them, so without these an installed copy can never run `gpu-fan-status -w`.
for mod in fan_backend.py fan_dash.py fan_look.py; do
  install -m 644 "$HERE/$mod" "$LIBDIR/$mod"
  echo "  $LIBDIR/$mod"
done
for prog in gpu-fan-control gpu-fan-status gpu-fan-detect gpu-power-limit gpu-thermal-guard; do
  install -m 755 "$HERE/$prog" "$BINDIR/$prog"
  echo "  $BINDIR/$prog"
done
printf '# Written by gpu-fan-control install.sh - where the .toml settings files live.\nCONFIG_DIR=%s\n' \
  "$CONFDIR" > /etc/gpu-fan-control.conf
echo "  /etc/gpu-fan-control.conf  (CONFIG_DIR=$CONFDIR)"

# The live view needs Textual. gpu-fan-status imports it from a root-owned vendor
# directory beside the modules rather than re-running itself out of a venv that any
# user could write to. Populate one with:
#   python3 -m pip install --target vendor --no-index --find-links wheels textual
if [ -d "$HERE/vendor" ]; then
  install -d -m 755 "$LIBDIR/vendor"
  cp -a "$HERE/vendor/." "$LIBDIR/vendor/"
  chown -R root:root "$LIBDIR/vendor"
  chmod -R go-w "$LIBDIR/vendor"
  echo "  $LIBDIR/vendor  (the live view's Textual)"
fi

# ------------------------------------------------------------------ IPMI modules
case "$BACKEND" in
  asrock|supermicro)
    printf '# gpu-fan-control: in-band IPMI, so ipmitool can reach the BMC.\nipmi_devintf\nipmi_si\n' \
      > /etc/modules-load.d/gpu-fan-control.conf
    modprobe ipmi_devintf 2>/dev/null || true
    modprobe ipmi_si 2>/dev/null || true
    echo "  /etc/modules-load.d/gpu-fan-control.conf  (ipmi_devintf, ipmi_si)"
    ;;
esac

# ------------------------------------------------------------------ validate
say "Validating the settings"
if "$BINDIR/gpu-fan-control" --config "$CONFDIR/fan-curve.toml" --check; then
  :
elif [ "$FORCE" = yes ]; then
  warn "the settings did not validate, but --force was given: installing anyway"
else
  die "
Not starting anything, because the settings above are not usable on this machine.
Work out what this board needs, then re-run:
    sudo gpu-fan-detect                 what controls the fans here
    sudo gpu-fan-detect --spin-all      which header or zone is the GPU fan
    gpu-fan-detect --write-config $CONFDIR/fan-curve.toml.new
(or pass --force to install it as it is)"
fi

# ------------------------------------------------------------------ units
say "systemd units"
units=(gpu-fan-control)
[ "$POWER" = yes ] && units+=(gpu-power-limit)
[ "$GUARD" = yes ] && units+=(gpu-thermal-guard)
for u in "${units[@]}"; do
  sed -e "s|@BINDIR@|$BINDIR|g" -e "s|@CONFIGDIR@|$CONFDIR|g" "$HERE/$u.service.in" \
    > "$UNITDIR/$u.service"
  chmod 644 "$UNITDIR/$u.service"
  echo "  $UNITDIR/$u.service"
done
systemctl daemon-reload

if [ "$START" = no ]; then
  say "Installed, nothing started (--no-start)"
  echo "When you are ready:  sudo systemctl enable --now ${units[*]}"
  exit 0
fi

say "Starting"
systemctl enable gpu-fan-control >/dev/null
systemctl restart gpu-fan-control
echo "  gpu-fan-control enabled and restarted"
if [ "$GUARD" = yes ]; then
  # The guard only acts at warn/trip temperatures, and a trip latch survives the restart.
  systemctl enable gpu-thermal-guard >/dev/null
  systemctl restart gpu-thermal-guard
  echo "  gpu-thermal-guard enabled and restarted"
fi
if [ "$POWER" = yes ]; then
  # Cool mode is applied at the next boot; this doesn't change the limit now.
  # Apply it now with: sudo systemctl start gpu-power-limit
  systemctl enable gpu-power-limit >/dev/null
  echo "  gpu-power-limit enabled for the next boot (start it now to apply cool mode today)"
fi

say "Status"
systemctl --no-pager --lines=5 status "${units[@]}" || true
cat <<EOM

Next:
  gpu-fan-status                       GPU temperatures and what the fan is doing
  sudo journalctl -fu gpu-fan-control  the live log: duty changes, reloads, faults
  $CONFDIR/fan-curve.toml   the curve; saved changes apply within seconds
EOM
