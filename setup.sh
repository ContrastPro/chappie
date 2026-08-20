#!/usr/bin/env sh
# Native bootstrap only; the Python setup engine owns Chappie setup semantics.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)' || {
  echo "Chappie requires Python 3.13 or newer. Install a supported Python, then re-run setup.sh." >&2
  exit 2
}
if [ ! -f "$ROOT/.venv/pyvenv.cfg" ]; then "$PYTHON" -m venv "$ROOT/.venv"; fi
"$ROOT/.venv/bin/python" -m pip install -e "$ROOT"
"$ROOT/.venv/bin/chappie" setup --yes --interactive "$@"
