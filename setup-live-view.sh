#!/bin/bash
# Install what the live view (gpu-fan-status -w) needs: Textual, in a .venv next to this
# script. gpu-fan-status re-runs itself with that interpreter, so this is the only setup
# needed - and it works under sudo too (root reads the same .venv). Nothing else on the
# system is touched, and the plain `gpu-fan-status` never needs any of it.
#
# Wheels are taken from a local folder when there is one, so an offline machine works:
#   ./wheels/ next to this script, or whatever $GPU_FAN_WHEELS points at.
# Otherwise they come from PyPI.
#
# Re-run install.sh afterwards if you have already installed: it links the new .venv so
# the copy in /usr/local/bin can find it.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
WHEELS="${GPU_FAN_WHEELS:-./wheels}"
WANT="${GPU_FAN_TEXTUAL:-textual==8.2.8}"

python3 -c 'import venv' 2>/dev/null || {
  echo "python3's venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv" >&2
  exit 1
}

python3 -m venv .venv
if [ -d "$WHEELS" ] && compgen -G "$WHEELS/*.whl" >/dev/null; then
  echo "Installing $WANT from $WHEELS (offline) ..."
  .venv/bin/python -m pip install --quiet --disable-pip-version-check \
    --no-index --find-links "$WHEELS" "$WANT"
else
  echo "Installing $WANT from PyPI ..."
  .venv/bin/python -m pip install --quiet --disable-pip-version-check "$WANT"
fi
.venv/bin/python - <<'PYEOF'
import importlib.metadata as m
print(f"  textual {m.version('textual')} · rich {m.version('rich')} - ready: ./gpu-fan-status -w")
PYEOF
