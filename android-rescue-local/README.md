# Android Rescue Local

Rescue accessible Android data to your Mac over USB or local Wi-Fi. Prioritize important files, resume interrupted sessions, and keep a clear record of what was copied and what failed.

## Capabilities

- **Automatic or selective collection:** photos, videos, audio, documents, downloads and other ADB-readable shared or removable storage.
- **Manual storage browser:** choose individual files or entire folders.
- **Verified resume:** check saved local file sizes and SHA-256 hashes before skipping completed storage files and APKs.
- **App and device exports:** preserve APKs, device inventories and diagnostics; attempt contacts, messages and private app data only where Android permits access.
- **Traceable results:** structured case folders, checksum manifests, source-to-destination mappings and failure reports.
- **Optional screen control:** launch scrcpy alongside collection for authorized interaction and app exports.

## Quick start

Requires macOS, Python 3 and ADB. Install ADB and optional scrcpy with Homebrew:

```bash
brew install --cask android-platform-tools
brew install scrcpy
```

Enable USB debugging, connect the phone and approve Android's authorization prompt. From this directory:

```bash
adb devices -l
python3 android_rescue.py --mode selective --scrcpy
```

Choose Auto mode for a comprehensive pass over accessible data:

```bash
python3 android_rescue.py --mode auto --no-bugreport
```

Cases are saved under `~/AndroidRescue` by default. Continue an interrupted case with:

```bash
python3 android_rescue.py --resume ~/AndroidRescue/YOUR_CASE_DIRECTORY
```

Wireless pairing, device selection, all options and output details are covered in the [operator guide](ANDROID_RESCUE_README.md). Local USB or Wi-Fi use requires no internet connection or relay server.

## Scope and limitations

Use only with devices you own or are explicitly authorized to access. Android's normal ADB permissions remain in force: this tool does not bypass locks, encrypted vaults or app sandboxing, recover deleted files, or create a complete phone clone.

Resume operates at file level. Local integrity checks do not verify source-side hashes or detect later changes on the phone. Collection happens on a live device; independently open and verify important recovered files before retiring the source phone. Operator actions through scrcpy can change phone data.

## Documentation and validation

- [Operator guide](ANDROID_RESCUE_README.md) — setup, workflows and troubleshooting context.
- [Architecture](ARCHITECTURE.md) — implementation, state and integrity handling.
- [Contributor guidance](AGENTS.md) — development and validation expectations.
- [Android Rescue Remote](../android-rescue-remote/README.md) — the separate tool for connections across networks.

Run the synthetic regression suite from this directory:

```bash
python3 -B -m unittest discover -s tests -v
```

**Status:** v2.1.1 has synthetic regression coverage. Real-device acceptance and integration with the remote tool remain pending.
