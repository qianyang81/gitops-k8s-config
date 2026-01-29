#!/usr/bin/env bash
set -euo pipefail

# check python3 is installed
if ! command -v python3 >/dev/null 2>&1; then
  echo "✖ Missing tool: python3"
  exit 1
fi

# execute the python verification script
exec python3 scripts/validate.py "$@"