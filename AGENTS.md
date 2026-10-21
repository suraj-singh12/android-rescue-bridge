# Working on Rescue Bridge

## Read first

1. `README.md` — setup and operator workflow.
2. `ARCHITECTURE.md` — implementation, protocol, trust boundaries and limitations.
3. `VALIDATION.md` — historical validation; distinguish recorded checks from checks you run.

## Scope and invariants

- This is owner-authorized, visible Android support through an ADB transport bridge.
- Preserve explicit enablement, the foreground notification with a stop action, `START_NOT_STICKY`, and no boot auto-start. Do not add hidden persistence or bypass Android ADB authorization.
- Keep TLS with certificate verification/pinning as the normal deployment path. Plaintext and unverified TLS are development options, not production defaults.
- Keep the Mac listener and phone ADB destination on loopback by default. Never recommend publishing ADB port 5555 to the internet.
- Never commit or print real tokens, enrollment configs or private keys. The relay registry stores token hashes; the Android saved config uses Keystore-backed AES-GCM.
- Reuse the existing relay, protocol, config, secure storage and service lifecycle. Avoid parallel implementations and unnecessary dependencies.
- Protocol changes must account for all three participants: Android, relay and Mac. Preserve the exact control-line/raw-byte boundary; a buffered Android control reader must not swallow ADB bytes.
- Keep setup in README, architecture in ARCHITECTURE, and validation evidence in VALIDATION rather than duplicating documents.

## Validation expectations

- For Python changes, check syntax and exercise affected relay/admin/Mac behavior with temporary credentials and simulated streams. Include authentication rejection, binary round trips, disconnect handling and TLS pinning when affected.
- For shell changes, use `bash -n` on the changed scripts.
- For Android changes, build using `android/RescueBridge/build_apk.sh` when the toolchain is available. The checkout has wrapper properties but no complete Gradle wrapper; do not report a build from source inspection alone.
- Real-device acceptance must cover local ADB reachability, RSA authorization, foreground start/stop, network changes and reboot behavior. Report unavailable checks explicitly.
- Do not infer production readiness from historical simulated tunnel tests or syntax checks.
