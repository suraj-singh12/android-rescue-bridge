#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ -x ./gradlew && -f ./gradle/wrapper/gradle-wrapper.jar ]]; then
  ./gradlew assembleDebug
elif command -v gradle >/dev/null 2>&1; then
  echo "Generating Gradle 9.6.0 wrapper..."
  gradle wrapper --gradle-version 9.6.0
  ./gradlew assembleDebug
else
  cat <<'MSG'
No Gradle executable/wrapper is present yet.
Open this RescueBridge directory in current Android Studio (recommended), let it sync,
then use Build > Build APK(s). Alternatively install Gradle and rerun this script.
MSG
  exit 2
fi

echo
echo "APK: app/build/outputs/apk/debug/app-debug.apk"
