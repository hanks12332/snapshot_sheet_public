#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "missing $PYTHON; create the virtual environment and install requirements.txt first" >&2
  exit 1
fi

cd "$SCRIPT_DIR"
exec "$PYTHON" main.py "$@"
