#!/bin/bash
# Build the distributable zip of this folder: dist/gpu-fan-control-<VERSION>.zip
#
#   ./make-zip.sh              build it
#   ./make-zip.sh --wheels     also vendor Textual's wheels into wheels/, so the live view
#                              can be set up on a machine with no internet
#
# What goes in: everything tracked here except .venv, caches, logs, the dist folder itself
# and any settings the recipient should write for their own hardware... with one exception -
# fan-curve.toml ships as it is, because it doubles as the worked ASRock Rack example. The
# first thing INSTALL.md says is to replace it with the one for their board.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
VERSION=$(cat VERSION)
NAME="gpu-fan-control-$VERSION"
OUT="dist/$NAME.zip"

if [ "${1:-}" = "--wheels" ]; then
  echo "Vendoring Textual wheels into wheels/ ..."
  mkdir -p wheels
  python3 -m pip download --quiet --dest wheels "${GPU_FAN_TEXTUAL:-textual==8.2.8}"
  echo "  $(ls wheels/*.whl | wc -l) wheels"
fi

command -v zip >/dev/null || { echo "zip is not installed: sudo apt install zip" >&2; exit 1; }

echo "Checking the package before building it"
for t in tests/test_loop.py tests/test_backends.py; do
  python3 "$t" >/dev/null || { echo "$t fails; not packaging" >&2; exit 1; }
done
for f in examples/*.toml fan-curve.toml; do
  python3 - "$f" <<'PYEOF' || exit 1
import importlib.machinery, importlib.util, sys
spec = importlib.util.spec_from_loader("ctl", importlib.machinery.SourceFileLoader("ctl", "gpu-fan-control"))
ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
try:
    ctl.make_backend(ctl.load_config(sys.argv[1]))
except Exception as e:
    sys.exit(f"  {sys.argv[1]}: {e}")
PYEOF
done
echo "  tests pass, every settings file parses"

rm -rf "dist/$NAME" "$OUT"
mkdir -p "dist/$NAME"
# A single top-level folder in the zip, so unzipping never scatters files into the cwd.
rsync -a --exclude-from=- ./ "dist/$NAME/" <<'EOF2'
.venv/
__pycache__/
dist/
wheels/*.tar.gz
*.pyc
*.log
*.swp
.git/
EOF2
(cd dist && zip -qr "$NAME.zip" "$NAME")
rm -rf "dist/$NAME"
echo
echo "Built $OUT  ($(du -h "$OUT" | cut -f1))"
unzip -l "$OUT" | tail -1
echo
echo "Hand that file over. On the other machine:"
echo "  unzip $NAME.zip && cd gpu-fan-control-$VERSION && less INSTALL.md"
