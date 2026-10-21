# Android Rescue for macOS architecture

## Purpose and boundaries

Rescue ADB-readable data from an authorized Android phone into an organized Mac case directory. This tool orchestrates ordinary ADB commands; it does not implement a remote relay, Android app, rooting, deleted-file recovery or a full-device restore system.

USB and already reachable wireless ADB devices work through the same serial-based interface. `--pair` and `--connect` invoke Android's normal wireless pairing/connection commands. Local Wi-Fi/hotspot use needs no internet. A separately established Rescue Bridge endpoint could be selected with `--serial 127.0.0.1:15555`; that composition has not been tested in this review.

## Files and dependencies

| File/component | Responsibility |
| --- | --- |
| `android-rescue.command` | Locate the adjacent script, add common Homebrew paths and forward arguments to Python |
| `android_rescue.py` | Standard-library Python CLI, currently version 2.1.0 |
| `Adb` | Serial-scoped subprocess commands, directory probes, NUL-delimited scans, pulls and transfer timeout wrappers |
| `StateDB` | Local SQLite WAL state for storage files and APK artifacts |
| Discovery/selection functions | Find readable roots, index paths, filter extensions/subtrees and register destinations |
| Acquisition functions | Batched storage copies, individual retries, APK copies, run-as archives, provider exports and diagnostics |
| Report functions | SHA-256 manifests, path mappings, failure CSV and human-readable summary |

Requires Python 3 and external `adb`; `scrcpy` is optional. There are no third-party Python imports. The launcher itself does not install prerequisites or establish a relay.

## Session flow

1. Parse CLI options and obtain authorization confirmation (or explicit `--authorized`). Start ADB; optionally pair/connect; select one authorized device.
2. Read device identity, create or reopen a case and open its SQLite database. Resume compares available build fingerprint, hardware serial and model; explicit `--force-resume` overrides mismatches.
3. Choose Auto or Selective mode. Optionally launch scrcpy independently.
4. Discover readable emulated/user-profile and removable-storage roots. Resolve canonical paths to avoid duplicate aliases.
5. Register selected files, assign local paths, copy, and commit status after batches/individual attempts.
6. Generate requested exports/manifests and final path map, failures report, summary and acquisition metadata. On interruption, commit resumable state and write reports where possible.

Auto collects metadata, all discovered readable storage, selected APK coverage (all by default), permitted private app archives, best-effort provider queries, a bugreport and local hashes, subject to flags.

Selective mode lets the operator prioritize photos, videos, audio, documents, downloads, messaging media, archives, APKs, permitted app data, providers, metadata or diagnostics. A manual browser copies chosen files/subtrees. Its extension catalog is cached in memory; Refresh clears it so new exports can be found. The catalog and copy queues are not fully memory-bounded for very large collections.

## Copy and resume mechanism

`storage_files` is keyed by storage-root label and remote path. `app_artifacts` is keyed by kind, package and remote path. Both record local destination, pending/done/error, error text and update time.

Storage transfers group paths by local parent and copy up to 32 files per ADB invocation. Failed batches retry individually. Files with collision-renamed basenames use exact destination pulls. SQLite's case-insensitive destination lookup handles common filename case collisions by adding a stable source-path-derived suffix; this is not a guarantee against every directory/Unicode filesystem collision.

On resume, done rows with an existing local path are skipped. Missing or unsuccessful files are retried. There is no byte-offset resume, source-change detection or automatic hash verification of already done files. Resume is continuing a live collection, not synchronizing a mirror or restoring an atomic snapshot.

When usable `find -print0` is unavailable, Auto/all-storage can fall back to a recursive ADB pull. That fallback does not populate per-file SQLite status/path mappings and has weaker resume/reporting guarantees. Scan errors can also leave incomplete discovery despite the pass finishing.

## Other acquisition paths

- APKs: query package paths and preserve base/split installers. These are not the user's app account/session data.
- Private data: test third-party packages with `run-as`; archive only those Android permits, normally debuggable apps. Existing nonempty archives are skipped on resume; interrupted archive validation needs particular care before trusting them.
- Providers: text queries for call logs, SMS, MMS records, phone contacts and calendar events; permissions vary. MMS queries do not themselves download every attachment.
- Bugreport: optional Android diagnostic export, potentially large and sensitive.
- scrcpy: independent screen viewing/control for legitimate unlock/export work. The tool leaves it running at acquisition exit. scrcpy deploys/runs its own phone-side server; this is not hardware write blocking.

## Output and interpretation

Default base: `~/AndroidRescue`; each case contains `storage/`, `apps/`, `providers/`, `metadata/`, `bugreport/`, `manifests/`, `reports/`, `logs/` and `.state/rescue.sqlite3`.

SHA-256 hashes describe local files at hashing time; they are not a source-versus-copy verification. Metadata can change after hashing during finalization. Reports combine SQLite file errors with separate provider/status/log evidence; a top-level complete status does not prove every requested export succeeded or all source data was discovered.

Android sandboxing, locked profiles, vaults and hardware-bound keys remain effective. This cannot clone a phone's authenticated state into another phone/emulator. Keep the source until important recovered files have been opened and checked independently.

## Validation evidence

The README records historical simulated checks for selective copying, resume, subtree acquisition, filename collisions and child-process timeouts. Those checks were not rerun as part of this documentation task.

On 2026-10-21 this review read all three supplied files and checked Python syntax, CLI help/version and launcher shell syntax. No acquisition, real-device test or parent-relay integration was performed. No committed test suite is supplied here.
