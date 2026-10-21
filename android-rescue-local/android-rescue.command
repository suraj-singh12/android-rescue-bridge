#!/bin/bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 was not found. Install Python 3, then run this launcher again."
  read -r -p "Press Enter to close..." _
  exit 1
fi

exec python3 "$SCRIPT_DIR/android_rescue.py" "$@"
