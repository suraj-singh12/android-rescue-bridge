# Validation record

Build/package date: 2026-09-13

Completed in the generation environment:

- `python3 -m py_compile` passed for `relay_server.py`, `rescue_admin.py`, and `rescue_mac.py`.
- XML parsing passed for AndroidManifest.xml, styles.xml, and the vector icon.
- Java source files passed structural/truncation checks.
- End-to-end plaintext development tunnel test: 300,000 random binary bytes round-tripped through simulated phone agent → relay → Mac bridge.
- End-to-end TLS pin test: 100,000 random binary bytes round-tripped using a generated self-signed certificate and SHA-256 certificate pin.
- Authentication failure test passed for an incorrect token.
- Authenticated `STATUS OFFLINE` control request test passed.

Not completed here:

- The Android APK was not compiled because this execution environment does not contain an Android SDK/Android Studio installation.
- Real-device testing is still required, especially `127.0.0.1:5555` reachability after `adb tcpip 5555` on each target OEM/Android version.
