#!/bin/bash
# Remove gpu-fan-control from this machine, putting the hardware back as it was.
#
#   sudo ./uninstall.sh            stop and remove everything; keep the settings files
#   sudo ./uninstall.sh --purge    also remove the settings files and the guard's latch
#
# Stopping the fan service hands the fan back to the board's own control, and stopping
# gpu-power-limit puts the GPUs back on their full default power limit.
set -euo pipefail
PURGE=no
[ "${1:-}" = "--purge" ] && PURGE=yes
[ "$(id -u)" = 0 ] || exec sudo -- "$0" "$@"

CONFDIR=$(sed -n 's/^CONFIG_DIR=//p' /etc/gpu-fan-control.conf 2>/dev/null | head -1)

for u in gpu-fan-control gpu-thermal-guard gpu-power-limit; do
  if systemctl list-unit-files "$u.service" >/dev/null 2>&1; then
    systemctl disable --now "$u" 2>/dev/null || true
    echo "stopped and disabled $u"
  fi
  rm -f "/etc/systemd/system/$u.service"
done
systemctl daemon-reload

rm -f /usr/local/bin/gpu-fan-control /usr/local/bin/gpu-fan-status /usr/local/bin/gpu-fan-detect \
      /usr/local/bin/gpu-power-limit /usr/local/bin/gpu-thermal-guard
rm -rf /usr/local/lib/gpu-fan-control
rm -f /etc/modules-load.d/gpu-fan-control.conf /etc/gpu-fan-control.conf
echo "removed the programs and the generated units"

if [ "$PURGE" = yes ]; then
  rm -rf /var/lib/gpu-thermal-guard
  if [ -n "$CONFDIR" ] && [ "$CONFDIR" != "$(dirname "$(realpath "$0")")" ]; then
    rm -f "$CONFDIR"/fan-curve.toml "$CONFDIR"/thermal-guard.toml "$CONFDIR"/power-limit.toml
    rmdir --ignore-fail-on-non-empty "$CONFDIR" 2>/dev/null || true
    echo "removed the settings in $CONFDIR and the guard's latch"
  else
    echo "removed the guard's latch; the settings files in this folder were left alone"
  fi
else
  echo "kept the settings files${CONFDIR:+ in $CONFDIR} and /var/lib/gpu-thermal-guard"
fi

cat <<EOM

The fan is back under the board's own control and the GPUs are back on their default
power limit. Check with:  sudo ipmitool sdr type Fan   (or: sensors)
EOM
