# Android Rescue for macOS — v2.1.0

For implementation details and limitations, see [ARCHITECTURE.md](ARCHITECTURE.md). Contributors should read [AGENTS.md](AGENTS.md).

A read-only, resumable Android acquisition/rescue tool for devices you own, test devices, or devices whose owner has explicitly authorized access.

It works with normal ADB, including **Wireless debugging**, and can launch **scrcpy** at the same time. It does not root the phone or bypass screen locks, vault passwords, Private Space, Secure Folder, app sandboxing, or Android Keystore protections.

## 1. Install the Mac prerequisites

With Homebrew:

```bash
brew install --cask android-platform-tools
brew install scrcpy
```

`adb` is required. `scrcpy` is optional but strongly recommended for interactive rescue/export work.

Verify:

```bash
adb version
scrcpy --version
python3 --version
```

## 2. Connect the Android phone wirelessly

On the phone, enable:

**Settings → Developer options → Wireless debugging**

For first-time pairing, Android shows a pairing IP/port and a pairing code:

```bash
adb pair PHONE_IP:PAIRING_PORT
```

Then connect using the Wireless debugging connection IP/port:

```bash
adb connect PHONE_IP:ADB_PORT
adb devices -l
```

The pairing port and ADB connection port can be different.

You can also let Android Rescue invoke the pair/connect commands:

```bash
python3 android_rescue.py \
  --pair PHONE_IP:PAIRING_PORT \
  --connect PHONE_IP:ADB_PORT \
  --scrcpy
```

No internet connection is required. The Mac only needs network reachability to the phone, such as the phone's hotspot/local Wi-Fi.

## 3. Start normally

```bash
python3 android_rescue.py --scrcpy
```

The tool first asks you to confirm authorization, then asks:

```text
1. Auto Fetch Everything
2. Priority / Selective Fetch
```

You can also choose directly:

```bash
python3 android_rescue.py --mode auto --scrcpy
python3 android_rescue.py --mode selective --scrcpy
```

If multiple Android devices are connected, the tool asks which one to use. You can force one with:

```bash
python3 android_rescue.py --serial 192.168.1.20:40211
```

## MODE 1 — Auto Fetch Everything

Auto mode performs a comprehensive normal-ADB acquisition:

- device/build/storage/user/profile metadata
- all discoverable ADB-readable shared/emulated storage
- removable/external storage visible to the ADB shell
- hidden dot-folders and `.nomedia` locations encountered in readable storage
- installed APKs and split APKs (all packages by default)
- private app archives **only** where Android itself permits `run-as` (normally debuggable apps)
- best-effort call log, SMS, MMS, contacts and calendar provider queries
- Android bugreport unless `--no-bugreport` is supplied
- SHA-256 integrity manifests
- remote-to-local path map
- failure report and acquisition summary

Run:

```bash
python3 android_rescue.py --mode auto --scrcpy
```

For a lighter pass without the large diagnostic bugreport:

```bash
python3 android_rescue.py --mode auto --scrcpy --no-bugreport
```

## MODE 2 — Priority / Selective Fetch

Use this when battery, time, wireless stability, or device health is limited.

The menu offers:

```text
 1. Photos / images
 2. Videos
 3. Audio / music / voice recordings
 4. Documents / text / spreadsheets / presentations
 5. Downloads folder(s)
 6. Messaging & app media (Android/media + common messenger folders)
 7. Backups / archives / shared-storage databases
 8. Installed APKs and split APKs
 9. Private app data where Android explicitly permits run-as
10. Call logs / SMS / MMS / Contacts / Calendar (best effort)
11. Device metadata / package / user / settings inventories
12. Android bugreport
13. ALL ADB-readable shared/removable storage
14. Manually navigate device storage

 S. Launch scrcpy
 H. Generate/refresh SHA-256 manifests now
 R. Refresh selective file catalog
 F. Finish selective session
```

After each operation, the tool returns to the menu so you can choose the next most important category.

### Manual storage browser

Choose **14**.

The browser shows the current Android path, entries, directories and file sizes. Commands:

```text
N       Open directory N, or copy file N
c N     Copy file/directory N immediately
c .     Copy the entire current directory
..      Go to parent directory
n / p   Next / previous page
r       Refresh current directory
q       Leave browser
```

Anything copied through the browser is saved under the same structured storage tree used by Auto mode.

## 4. Interruption and resume

### Selective mode

Press **Ctrl-C** during an operation. The active ADB copy/scan is terminated, SQLite state is committed, completed files stay complete, and the tool returns to the menu where possible.

### Auto mode

Press **Ctrl-C** to stop safely. The case remains resumable.

Resume later:

```bash
python3 android_rescue.py \
  --resume ~/AndroidRescue/YOUR_CASE_DIRECTORY \
  --serial PHONE_IP:ADB_PORT \
  --scrcpy
```

You can resume an Auto case in Selective mode, or a Selective case in Auto mode:

```bash
python3 android_rescue.py \
  --resume ~/AndroidRescue/YOUR_CASE_DIRECTORY \
  --mode auto \
  --serial PHONE_IP:ADB_PORT
```

Already completed files recorded in the case database are skipped if their local copy still exists. Failed/pending files are retryable.

The tool compares the resumed phone's identity with the stored case identity and refuses an obvious device mismatch unless you intentionally use `--force-resume`.

## 5. Output structure

A case looks like:

```text
~/AndroidRescue/
└── Samsung_SM-S921B_SERIAL_20260913_101500/
    ├── storage/
    │   ├── user_0_primary/
    │   ├── user_10_emulated/
    │   └── external_XXXX-XXXX/
    │
    ├── apps/
    │   ├── apks/
    │   │   └── com.example.app/
    │   │       ├── base.apk
    │   │       └── split_*.apk
    │   └── debuggable_private/
    │       ├── com.example.debuggable.tar
    │       └── status.json
    │
    ├── providers/
    │   ├── call_log.txt
    │   ├── sms.txt
    │   ├── mms.txt
    │   ├── contacts_phones.txt
    │   ├── calendar_events.txt
    │   └── status.json
    │
    ├── metadata/
    │   ├── acquisition.json
    │   ├── storage_roots.json
    │   ├── storage_root_probe.json
    │   └── device/
    │       ├── getprop.txt
    │       ├── users_cmd.txt
    │       ├── packages_all.txt
    │       ├── settings_*.txt
    │       └── ...
    │
    ├── bugreport/
    ├── manifests/
    │   ├── files.csv
    │   ├── SHA256SUMS.txt
    │   └── remote_to_local.csv
    │
    ├── reports/
    │   ├── SUMMARY.txt
    │   └── failures.csv
    │
    ├── logs/
    │   ├── acquisition.log
    │   └── scrcpy.log
    │
    └── .state/
        └── rescue.sqlite3
```

`remote_to_local.csv` is particularly useful when two Android filenames differ only by case. macOS commonly uses a case-insensitive filesystem, so the tool safely renames the second local copy while retaining the exact Android source path in the manifest.

## 6. Using scrcpy during rescue

Launch at startup:

```bash
python3 android_rescue.py --mode selective --scrcpy
```

Or press **S** from Selective mode.

A useful owner-authorized workflow for private/hidden content is:

1. Rescue urgent normal files first.
2. Open the gallery/vault/profile through scrcpy.
3. Authenticate normally using the owner's credential/recovery method.
4. Use the app's own Export / Move / Save / Share-to-storage function.
5. Back in Android Rescue, press **R** to refresh the selective catalog.
6. Fetch Photos/Videos/App Media again, or choose **13** for all readable storage.

The tool does not guess or bypass vault passwords.

## 7. What cannot be universally cloned

Normal wireless ADB is not root.

Modern Android intentionally prevents a normal ADB shell from reading arbitrary release-app private data under locations such as `/data/data` or `/data/user`. Android 12+ also restricts legacy `adb backup` for normal apps, so Android Rescue does not pretend that command is a complete phone clone.

`run-as` is attempted only where Android itself explicitly permits it. Those app archives may be useful for your own/test/debuggable apps.

Even when app files are available, moving them into an emulator does **not** guarantee the app will open in the same authenticated state. Apps can use device-bound or hardware-backed Android Keystore keys that cannot simply be copied to another device/emulator.

Similarly, Android Private Space, Samsung Secure Folder and third-party encrypted vaults may require legitimate unlock/export/migration while the original phone is still functional.

Keep the source phone until critical rescued material has been independently opened and verified.

## 8. Useful options

```text
--mode auto|selective
--serial SERIAL
--pair HOST:PORT
--connect HOST:PORT
--output DIRECTORY
--case-name NAME
--resume CASE_DIRECTORY
--force-resume
--scrcpy
--apk-mode none|third-party|all
--no-debuggable-data
--no-providers
--bugreport
--no-bugreport
--no-hash
--pull-timeout SECONDS
--authorized
--version
```

For unattended/non-interactive use, `--authorized` explicitly records that you are asserting authorization so the interactive consent prompt can be skipped.

## 9. Recommended dying-phone order

If the phone could stop working at any moment, use Selective mode and roughly prioritize:

1. Photos
2. Videos
3. Documents
4. Downloads
5. Messaging/app media
6. Backups/archives
7. Manually selected critical folders
8. Provider exports
9. APKs/app inventory
10. Metadata
11. Full shared storage
12. Hashing/bugreport after the irreplaceable data is safe

If the phone is stable, Auto mode is simpler.

## Validation performed on this build

The supplied v2.1.0 script was:

- Python syntax/bytecode compiled successfully.
- Exercised against a simulated Android storage tree.
- Tested for selective image acquisition.
- Tested for resume/skip behavior.
- Tested for subtree copying.
- Tested for macOS case-insensitive filename collision handling.
- Tested to ensure timed-out child transfer processes are terminated.

Real-device behavior still depends on the Android version, OEM restrictions, unlocked profiles, permissions granted to ADB, and the health of the source device.
