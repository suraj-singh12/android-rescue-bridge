#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${1:-}"
if [[ -z "$CONFIG" ]]; then
  if command -v osascript >/dev/null 2>&1; then
    CONFIG="$(osascript -e 'POSIX path of (choose file with prompt "Choose the Rescue Bridge .mac.json config")')" || exit 1
  else
    echo "Path to *.mac.json config:"
    read -r CONFIG
  fi
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "Config not found: $CONFIG"
  read -r -p "Press Return to close..." _
  exit 1
fi
if ! command -v adb >/dev/null 2>&1; then
  echo "adb not found. Install: brew install --cask android-platform-tools"
  read -r -p "Press Return to close..." _
  exit 1
fi
if ! command -v scrcpy >/dev/null 2>&1; then
  echo "scrcpy not found. Install: brew install scrcpy"
  read -r -p "Press Return to close..." _
  exit 1
fi
exec python3 "$HERE/rescue_mac.py" --config "$CONFIG" --wait-agent --connect-adb --scrcpy
