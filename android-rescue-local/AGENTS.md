# Working on Android Rescue for macOS

## Read first

1. `ANDROID_RESCUE_README.md` — installation, operator workflow and options.
2. `ARCHITECTURE.md` — code structure, state, acquisition flow and limitations.
3. `android_rescue.py` — implementation, currently v2.1.0.

## Scope

This directory is an independent Python ADB acquisition tool, not the parent project's relay or Android agent. Parent guidance applies where relevant; relay protocol and Android-service instructions do not describe this tool.

- Preserve owner authorization and source read/query/pull behavior. Do not add rooting, deletion, app clearing, remounting or lock/vault bypasses.
- scrcpy is optional interactive access; operator actions through it can change the phone. Do not claim the entire session is a write-blocked forensic acquisition.
- Reuse `Adb`, `StateDB`, storage registration, acquisition and report functions. Keep new dependencies unnecessary unless justified.
- Maintain Auto, Selective and manual-browser workflows, cancellation handling and resumable case compatibility.
- Preserve exact remote-to-local mappings and collision handling. Do not let phone paths escape the local case directory or silently overwrite distinct source files.
- Preserve pending/done/error accounting. Do not describe an incomplete scan, failed export or partial archive as complete recovery.
- Keep retrieved personal data, logs and case databases out of source control and test output. Use synthetic fixtures for checks.
- Update the existing README for usage and ARCHITECTURE for internals; distinguish historical validation claims from new checks.

## Validation

- Check Python syntax and `--help`/`--version` without connecting to a phone; use `bash -n android-rescue.command` for launcher changes.
- When changing acquisition/state logic, use simulated ADB fixtures to check selection, subtree copy, collisions, retry, resume and cancellation as applicable.
- Resume currently skips done items when a local path exists; do not assume content verification or byte-offset restart.
- Real-device claims require authorized-device evidence. Test USB/wireless transport, permissions, interruption and output integrity when affected. Never infer full-phone recoverability from mocks.
- No automated test suite is included in this directory at review time. Report unavailable checks plainly.
