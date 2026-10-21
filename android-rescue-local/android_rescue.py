#!/usr/bin/env python3
"""
Android Rescue for macOS
========================
Read-only, best-effort Android acquisition over ADB (including wireless ADB).

Goals:
- Preserve all ADB-readable shared/external storage, including dotfiles/.nomedia media.
- Enumerate Android users/profiles and attempt readable emulated-storage roots.
- Inventory installed packages and optionally preserve APK/split APK files.
- Best-effort export of content providers that the ADB shell is permitted to read.
- Best-effort archive of private data ONLY for packages Android explicitly permits
  via `run-as` (normally debuggable apps). No root or security bypass is attempted.
- Two modes: Auto Fetch Everything and Priority/Selective Fetch with interactive storage browsing.
- Optional scrcpy launch for owner-authorized interactive actions/exports.
- Resume interrupted acquisitions using a local SQLite state database.
- Generate SHA-256 manifests and a human-readable acquisition summary.

This tool does NOT bypass screen locks, vault passwords, Android Private Space,
Samsung Secure Folder, app sandboxing, hardware-backed Keystore, or other access
controls. Use only on devices you own or are explicitly authorized to inspect.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

VERSION = "2.1.0"
BATCH_SIZE = 32
HASH_CHUNK = 4 * 1024 * 1024

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp",
    ".tif", ".tiff", ".dng", ".raw", ".cr2", ".nef", ".arw", ".avif",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mkv", ".webm", ".avi",
    ".wmv", ".flv", ".mpeg", ".mpg", ".mts", ".m2ts", ".ts",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".amr",
    ".3ga", ".wma", ".mid", ".midi", ".aiff", ".caf",
}
DOCUMENT_EXTENSIONS = {
    ".pdf", ".txt", ".rtf", ".md", ".csv", ".tsv", ".doc", ".docx",
    ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".pages", ".numbers", ".key", ".epub", ".mobi", ".html", ".htm",
    ".xml", ".json", ".yaml", ".yml", ".tex", ".log",
}
BACKUP_ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
    ".bak", ".backup", ".ab", ".db", ".sqlite", ".sqlite3", ".crypt",
    ".crypt12", ".crypt14", ".crypt15",
}


def human_bytes(value: int) -> str:
    n = float(max(0, value))
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if n < 1024.0 or unit == units[-1]:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{value} B"


def progress_line(label: str, done: int, total: int, extra: str = "") -> None:
    if total <= 0:
        text = f"{label}: {done:,}"
    else:
        pct = min(100.0, (done / total) * 100.0)
        width = 26
        filled = min(width, int(width * done / total))
        bar = "#" * filled + "-" * (width - filled)
        text = f"{label}: [{bar}] {pct:6.2f}%  {done:,}/{total:,}"
    if extra:
        text += f"  {extra}"
    # Clear to end of line so shorter updates do not leave stale characters.
    sys.stdout.write("\r" + text[:220] + "\033[K")
    sys.stdout.flush()


def end_progress(message: Optional[str] = None) -> None:
    if message:
        sys.stdout.write("\r" + message[:220] + "\033[K\n")
    else:
        sys.stdout.write("\n")
    sys.stdout.flush()


def run_cancellable_passthrough(cmd: Sequence[str], *, timeout: int) -> int:
    """Run an interactive/passthrough child and guarantee it is stopped on cancel/timeout."""
    proc = subprocess.Popen(list(cmd))
    started = time.monotonic()
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                return int(rc)
            if timeout > 0 and (time.monotonic() - started) > timeout:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise subprocess.TimeoutExpired(cmd, timeout)
            time.sleep(0.15)
    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        raise


def run_stream_to_file_with_progress(
    cmd: Sequence[str],
    out_path: Path,
    *,
    label: str,
    timeout: int,
) -> Tuple[int, str]:
    """Run a command whose stdout is a file, showing liveness/bytes when total size is unknown."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    spinner = "|/-\\"
    with out_path.open("wb") as fp, tempfile.TemporaryFile() as err_fp:
        proc = subprocess.Popen(list(cmd), stdout=fp, stderr=err_fp)
        try:
            tick = 0
            while proc.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed > timeout:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise subprocess.TimeoutExpired(cmd, timeout)
                size = fp.tell()
                sys.stdout.write(
                    f"\r{label}: {spinner[tick % len(spinner)]}  written {human_bytes(size)}  elapsed {int(elapsed)}s\033[K"
                )
                sys.stdout.flush()
                tick += 1
                time.sleep(0.2)
            rc = int(proc.returncode or 0)
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            raise
        finally:
            sys.stdout.write("\n")
            sys.stdout.flush()
        err_fp.seek(0)
        err = err_fp.read().decode("utf-8", "replace").strip()
    return rc, err


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_name(value: str, default: str = "unknown") -> str:
    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip("._-")
    return value[:120] or default


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def batched(items: Sequence[Tuple[str, str]], n: int) -> Iterator[Sequence[Tuple[str, str]]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


class Logger:
    def __init__(self, log_path: Path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self.fp = log_path.open("a", encoding="utf-8", buffering=1)

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"[{now_iso()}] [{level}] {message}"
        print(line, flush=True)
        self.fp.write(line + "\n")

    def close(self) -> None:
        try:
            self.fp.close()
        except Exception:
            pass


class Adb:
    def __init__(self, adb_bin: str, serial: str, logger: Logger):
        self.adb_bin = adb_bin
        self.serial = serial
        self.logger = logger

    @property
    def prefix(self) -> List[str]:
        return [self.adb_bin, "-s", self.serial]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: Optional[int] = 120,
        text: bool = True,
        capture: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        cmd = self.prefix + list(args)
        kwargs = {
            "timeout": timeout,
            "text": text,
            "check": check,
        }
        if capture:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        return subprocess.run(cmd, **kwargs)  # type: ignore[arg-type]

    def shell(self, command: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return self.run(["shell", command], timeout=timeout, text=True, capture=True)

    def shell_text(self, command: str, timeout: int = 120) -> str:
        p = self.shell(command, timeout=timeout)
        out = p.stdout or ""
        err = p.stderr or ""
        if p.returncode != 0 and err:
            self.logger.log(f"shell command returned {p.returncode}: {command}: {err.strip()}", "WARN")
        return out

    def getprop(self, key: str) -> str:
        return self.shell_text(f"getprop {shell_quote(key)}").strip()

    def pull_one(self, remote: str, local: Path, timeout: int) -> Tuple[bool, str]:
        local.parent.mkdir(parents=True, exist_ok=True)
        p = self.run(["pull", "-a", remote, str(local)], timeout=timeout, text=True, capture=True)
        msg = ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()
        return p.returncode == 0 and local.exists(), msg

    def pull_many(self, remotes: Sequence[str], local_dir: Path, timeout: int) -> Tuple[bool, str]:
        local_dir.mkdir(parents=True, exist_ok=True)
        p = self.run(["pull", "-a", *remotes, str(local_dir)], timeout=timeout, text=True, capture=True)
        msg = ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()
        return p.returncode == 0, msg

    def pull_one_live(self, remote: str, local: Path, timeout: int) -> Tuple[bool, str]:
        """Pull one item while letting adb display its own transfer progress."""
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            rc = run_cancellable_passthrough(self.prefix + ["pull", "-a", remote, str(local)], timeout=timeout)
            return rc == 0 and local.exists(), f"adb pull returned {rc}"
        except subprocess.TimeoutExpired:
            return False, f"adb pull timed out after {timeout}s"

    def pull_many_live(self, remotes: Sequence[str], local_dir: Path, timeout: int) -> Tuple[bool, str]:
        """Pull multiple items while letting adb display its own transfer progress."""
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            rc = run_cancellable_passthrough(self.prefix + ["pull", "-a", *remotes, str(local_dir)], timeout=timeout)
            return rc == 0, f"adb pull returned {rc}"
        except subprocess.TimeoutExpired:
            return False, f"adb pull timed out after {timeout}s"

    def readable_dir(self, remote: str) -> bool:
        p = self.shell(f"if [ -d {shell_quote(remote)} ] && [ -r {shell_quote(remote)} ]; then echo YES; fi")
        return "YES" in (p.stdout or "")

    def path_kind(self, remote: str) -> str:
        p = self.shell(
            f"if [ -d {shell_quote(remote)} ]; then echo D; "
            f"elif [ -f {shell_quote(remote)} ]; then echo F; "
            f"elif [ -e {shell_quote(remote)} ]; then echo O; else echo M; fi",
            timeout=30,
        )
        return (p.stdout or "M").strip()[:1] or "M"

    def file_size(self, remote: str) -> Optional[int]:
        p = self.shell(f"stat -c %s {shell_quote(remote)} 2>/dev/null", timeout=30)
        text = (p.stdout or "").strip().splitlines()
        if not text:
            return None
        try:
            return int(text[0])
        except ValueError:
            return None

    def canonical_path(self, remote: str) -> str:
        p = self.shell(f"readlink -f {shell_quote(remote)} 2>/dev/null || echo {shell_quote(remote)}")
        return (p.stdout or remote).strip().splitlines()[0] if (p.stdout or "").strip() else remote

    def supports_find_print0(self, remote: str) -> bool:
        cmd = f"find {shell_quote(remote)} -maxdepth 0 -print0 2>/dev/null"
        p = self.run(["exec-out", "sh", "-c", cmd], timeout=30, text=False, capture=True)
        data = p.stdout or b""
        return p.returncode == 0 and b"\x00" in data

    def _iter_nul_command(self, cmd: str, error_sink) -> Iterator[str]:
        """Stream NUL-delimited stdout and always terminate adb if the consumer cancels."""
        with tempfile.TemporaryFile() as err_fp:
            proc = subprocess.Popen(
                self.prefix + ["exec-out", "sh", "-c", cmd],
                stdout=subprocess.PIPE,
                stderr=err_fp,
            )
            assert proc.stdout is not None
            buf = b""
            rc: Optional[int] = None
            try:
                while True:
                    chunk = proc.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    buf += chunk
                    parts = buf.split(b"\x00")
                    buf = parts.pop()
                    for raw in parts:
                        if raw:
                            yield os.fsdecode(raw)
                if buf:
                    yield os.fsdecode(buf)
                rc = proc.wait()
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                if rc is None:
                    rc = proc.returncode
                err_fp.seek(0)
                err = err_fp.read().decode("utf-8", "replace").strip()
                if rc not in (None, 0) or err:
                    error_sink(int(rc or 0), err)

    def iter_find(self, remote: str, kind: str, error_sink) -> Iterator[str]:
        # Stream NUL-delimited paths so huge trees do not have to fit in memory.
        cmd = f"find {shell_quote(remote)} -type {kind} -print0"
        yield from self._iter_nul_command(cmd, error_sink)

    def iter_shallow(self, remote: str, error_sink) -> Iterator[str]:
        """Yield direct children of a directory using NUL delimiters."""
        cmd = f"find {shell_quote(remote)} -mindepth 1 -maxdepth 1 -print0"
        yield from self._iter_nul_command(cmd, error_sink)


class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS storage_files (
                root_label TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                local_rel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                updated_at TEXT,
                PRIMARY KEY (root_label, remote_path)
            );
            CREATE INDEX IF NOT EXISTS idx_storage_status ON storage_files(status);

            CREATE TABLE IF NOT EXISTS app_artifacts (
                kind TEXT NOT NULL,
                package_name TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                local_rel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                updated_at TEXT,
                PRIMARY KEY (kind, package_name, remote_path)
            );
            CREATE INDEX IF NOT EXISTS idx_app_status ON app_artifacts(status);
            """
        )
        self.conn.commit()

    def add_storage(self, root_label: str, remote: str, local_rel: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO storage_files(root_label,remote_path,local_rel,status,updated_at) VALUES(?,?,?,?,?)",
            (root_label, remote, local_rel, "pending", now_iso()),
        )

    def safe_local_rel(self, root_label: str, remote: str, proposed: str) -> str:
        """Keep a stable local path and avoid common case-insensitive macOS collisions."""
        existing = self.conn.execute(
            "SELECT local_rel FROM storage_files WHERE root_label=? AND remote_path=?",
            (root_label, remote),
        ).fetchone()
        if existing:
            return str(existing[0])

        candidate = proposed
        salt = hashlib.sha256(remote.encode("utf-8", "surrogateescape")).hexdigest()[:10]
        attempt = 0
        while True:
            collision = self.conn.execute(
                "SELECT remote_path FROM storage_files WHERE root_label=? AND local_rel=? COLLATE NOCASE LIMIT 1",
                (root_label, candidate),
            ).fetchone()
            if not collision or str(collision[0]) == remote:
                return candidate
            p = Path(proposed)
            suffix = p.suffix
            stem = p.name[: -len(suffix)] if suffix else p.name
            marker = f".__android_rescue_collision_{salt}" + (f"_{attempt}" if attempt else "")
            candidate = str(p.with_name(stem + marker + suffix))
            attempt += 1

    def set_storage(self, root_label: str, remote: str, status: str, error: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE storage_files SET status=?,error=?,updated_at=? WHERE root_label=? AND remote_path=?",
            (status, error, now_iso(), root_label, remote),
        )

    def storage_rows(self, root_label: str) -> List[Tuple[str, str, str]]:
        return list(
            self.conn.execute(
                "SELECT remote_path,local_rel,status FROM storage_files WHERE root_label=? ORDER BY local_rel",
                (root_label,),
            )
        )

    def storage_status(self, root_label: str, remote: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT status FROM storage_files WHERE root_label=? AND remote_path=?",
            (root_label, remote),
        ).fetchone()
        return str(row[0]) if row else None

    def storage_row(self, root_label: str, remote: str) -> Optional[Tuple[str, str, str]]:
        row = self.conn.execute(
            "SELECT remote_path,local_rel,status FROM storage_files WHERE root_label=? AND remote_path=?",
            (root_label, remote),
        ).fetchone()
        if not row:
            return None
        return str(row[0]), str(row[1]), str(row[2])

    def add_artifact(self, kind: str, package: str, remote: str, local_rel: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO app_artifacts(kind,package_name,remote_path,local_rel,status,updated_at) VALUES(?,?,?,?,?,?)",
            (kind, package, remote, local_rel, "pending", now_iso()),
        )

    def set_artifact(self, kind: str, package: str, remote: str, status: str, error: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE app_artifacts SET status=?,error=?,updated_at=? WHERE kind=? AND package_name=? AND remote_path=?",
            (status, error, now_iso(), kind, package, remote),
        )

    def commit(self) -> None:
        self.conn.commit()

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for status, n in self.conn.execute("SELECT status,COUNT(*) FROM storage_files GROUP BY status"):
            out[f"storage_{status}"] = n
        for status, n in self.conn.execute("SELECT status,COUNT(*) FROM app_artifacts GROUP BY status"):
            out[f"app_{status}"] = n
        return out

    def failures(self) -> List[Tuple[str, str, str, str]]:
        rows: List[Tuple[str, str, str, str]] = []
        for root, remote, local, error in self.conn.execute(
            "SELECT root_label,remote_path,local_rel,COALESCE(error,'') FROM storage_files WHERE status='error'"
        ):
            rows.append(("storage:" + root, remote, local, error))
        for kind, package, remote, local, error in self.conn.execute(
            "SELECT kind,package_name,remote_path,local_rel,COALESCE(error,'') FROM app_artifacts WHERE status='error'"
        ):
            rows.append((f"{kind}:{package}", remote, local, error))
        return rows

    def storage_path_map(self) -> List[Tuple[str, str, str, str]]:
        return list(
            self.conn.execute(
                "SELECT root_label,remote_path,local_rel,status FROM storage_files ORDER BY root_label,remote_path"
            )
        )

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def adb_global(adb_bin: str, args: Sequence[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([adb_bin, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)


def require_program(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required program not found: {name}")
    return path


def parse_devices(adb_bin: str) -> List[Tuple[str, str, str]]:
    p = adb_global(adb_bin, ["devices", "-l"])
    devices: List[Tuple[str, str, str]] = []
    for line in (p.stdout or "").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        details = " ".join(parts[2:])
        devices.append((serial, state, details))
    return devices


def choose_device(adb_bin: str, requested: Optional[str]) -> str:
    devices = parse_devices(adb_bin)
    if requested:
        for serial, state, _ in devices:
            if serial == requested:
                if state != "device":
                    raise RuntimeError(f"Device {serial} is in state '{state}', not ready. Check the phone's ADB authorization prompt.")
                return serial
        raise RuntimeError(f"Requested ADB device not found: {requested}")

    ready = [(s, d) for s, state, d in devices if state == "device"]
    blocked = [(s, state) for s, state, _ in devices if state != "device"]
    if not ready:
        detail = ", ".join(f"{s}({st})" for s, st in blocked) or "none"
        raise RuntimeError(f"No authorized ADB device is ready. Other states: {detail}")
    if len(ready) == 1:
        return ready[0][0]
    if not sys.stdin.isatty():
        raise RuntimeError("Multiple ADB devices are connected. Re-run with --serial DEVICE_SERIAL.")
    print("Multiple authorized devices are connected:")
    for i, (serial, details) in enumerate(ready, 1):
        print(f"  {i}. {serial}  {details}")
    while True:
        choice = input("Select device number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(ready):
            return ready[int(choice) - 1][0]


def require_authorization(noninteractive_ok: bool) -> None:
    if noninteractive_ok:
        return
    if not sys.stdin.isatty():
        raise RuntimeError("Non-interactive use requires --authorized to confirm you have permission to access this device.")
    print(
        textwrap.dedent(
            """
            AUTHORIZATION CHECK
            This tool can copy personal files and other sensitive data that Android permits ADB to read.
            Use it only on a device you own or are explicitly authorized to rescue/inspect.
            It does not bypass locks, vault passwords, app sandboxing, or secure profiles.
            """
        ).strip()
    )
    answer = input("Type I HAVE AUTHORIZATION to continue: ").strip()
    if answer != "I HAVE AUTHORIZATION":
        raise RuntimeError("Authorization confirmation not provided; exiting without acquisition.")


def capture_command(adb: Adb, out: Path, command: str, timeout: int = 120) -> Dict[str, object]:
    p = adb.shell(command, timeout=timeout)
    body = p.stdout or ""
    if p.stderr:
        body += "\n--- STDERR ---\n" + p.stderr
    write_text(out, body)
    return {"path": str(out), "returncode": p.returncode, "ok": p.returncode == 0}


def collect_metadata(adb: Adb, case: Path, logger: Logger) -> None:
    logger.log("Collecting device metadata and package inventories")
    meta = case / "metadata" / "device"
    commands = {
        "getprop.txt": "getprop",
        "uname.txt": "uname -a",
        "identity.txt": "id",
        "date.txt": "date",
        "android_version.txt": "getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.build.version.security_patch",
        "build_fingerprint.txt": "getprop ro.build.fingerprint",
        "users_cmd.txt": "cmd user list -v 2>/dev/null || cmd user list",
        "users_pm.txt": "pm list users",
        "storage_volumes.txt": "sm list-volumes all 2>/dev/null; echo '--- disks ---'; sm list-disks 2>/dev/null",
        "storage_listing.txt": "ls -la /storage 2>&1",
        "df.txt": "df -h 2>&1",
        "mounts.txt": "mount 2>&1",
        "battery.txt": "dumpsys battery 2>&1",
        "display.txt": "wm size 2>&1; wm density 2>&1",
        "selinux.txt": "getenforce 2>&1",
        "packages_all.txt": "pm list packages -f -i",
        "packages_third_party.txt": "pm list packages -3 -f -i",
        "packages_disabled.txt": "pm list packages -d -f",
        "features.txt": "pm list features",
        "libraries.txt": "pm list libraries",
        "settings_global.txt": "settings list global",
        "settings_system.txt": "settings list system",
        "settings_secure.txt": "settings list secure",
    }
    total = len(commands)
    for idx, (name, cmd) in enumerate(commands.items(), 1):
        progress_line("Metadata", idx - 1, total, name)
        try:
            capture_command(adb, meta / name, cmd)
        except Exception as exc:
            logger.log(f"Metadata command failed for {name}: {exc}", "WARN")
            write_text(meta / name, f"ERROR: {exc}\n")
        progress_line("Metadata", idx, total, name)
    end_progress(f"Metadata: COMPLETE ({total} inventories/queries)")


def parse_user_ids(text: str) -> List[int]:
    ids = {0}
    for match in re.finditer(r"UserInfo\{(\d+):", text):
        ids.add(int(match.group(1)))
    return sorted(ids)


def discover_storage_roots(adb: Adb, case: Path, logger: Logger) -> List[Dict[str, str]]:
    roots: List[Dict[str, str]] = []
    probes: List[Dict[str, object]] = []
    seen_canonical = set()

    users_text = adb.shell_text("cmd user list -v 2>/dev/null || cmd user list")
    user_ids = parse_user_ids(users_text)
    for uid in user_ids:
        candidates = [f"/storage/emulated/{uid}"]
        if uid == 0:
            candidates += ["/sdcard", "/storage/self/primary"]
        for candidate in candidates:
            readable = adb.readable_dir(candidate)
            probes.append({"kind": "emulated", "user_id": uid, "candidate": candidate, "readable": readable})
            if not readable:
                continue
            canonical = adb.canonical_path(candidate)
            if canonical in seen_canonical:
                continue
            seen_canonical.add(canonical)
            label = "user_0_primary" if uid == 0 else f"user_{uid}_emulated"
            roots.append({"label": label, "remote": canonical, "kind": "emulated", "user_id": str(uid)})
            break

    # Physical/removable volumes normally appear as /storage/XXXX-XXXX.
    p = adb.shell("ls -1A /storage 2>/dev/null")
    for name in (p.stdout or "").splitlines():
        name = name.strip()
        if not name or name in {"emulated", "self"}:
            continue
        remote = f"/storage/{name}"
        readable = adb.readable_dir(remote)
        probes.append({"kind": "external", "candidate": remote, "readable": readable})
        if not readable:
            continue
        canonical = adb.canonical_path(remote)
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)
        roots.append({"label": "external_" + safe_name(name), "remote": canonical, "kind": "external", "user_id": ""})

    atomic_json(case / "metadata" / "storage_roots.json", roots)
    atomic_json(case / "metadata" / "storage_root_probe.json", probes)
    if roots:
        for r in roots:
            logger.log(f"Readable storage root: {r['label']} -> {r['remote']}")
    else:
        logger.log("No readable shared/external storage roots discovered", "WARN")
    return roots


def relative_remote(root: str, path: str) -> str:
    root_clean = root.rstrip("/")
    if path == root_clean:
        return ""
    prefix = root_clean + "/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    # Defensive fallback; never allow an absolute path to escape the case dir.
    return path.lstrip("/")


def register_storage_file(db: StateDB, root: Dict[str, str], remote: str) -> Tuple[str, str, str]:
    rel = relative_remote(root["remote"], remote)
    proposed = str(Path("storage") / root["label"] / rel)
    local_rel = db.safe_local_rel(root["label"], remote, proposed)
    db.add_storage(root["label"], remote, local_rel)
    status = db.storage_status(root["label"], remote) or "pending"
    return remote, local_rel, status


def queue_subtree(
    adb: Adb,
    db: StateDB,
    case: Path,
    root: Dict[str, str],
    remote_path: str,
    logger: Logger,
    *,
    label: str,
) -> List[Tuple[str, str, str]]:
    """Register one file or all files below a directory while preserving original structure."""
    kind = adb.path_kind(remote_path)
    if kind == "M":
        logger.log(f"{label}: path no longer exists: {remote_path}", "WARN")
        return []
    if kind == "F":
        row = register_storage_file(db, root, remote_path)
        db.commit()
        return [row]
    if kind != "D":
        logger.log(f"{label}: unsupported/non-regular item skipped: {remote_path}", "WARN")
        return []

    local_base = case / "storage" / root["label"] / relative_remote(root["remote"], remote_path)
    local_base.mkdir(parents=True, exist_ok=True)
    rows: List[Tuple[str, str, str]] = []
    dir_count = 0
    file_count = 0

    def scan_err(rc: int, err: str) -> None:
        if rc != 0 or err:
            logger.log(f"{label}: subtree scan was partial (rc={rc}): {err[:500]}", "WARN")

    try:
        for d in adb.iter_find(remote_path, "d", scan_err):
            rel = relative_remote(root["remote"], d)
            (case / "storage" / root["label"] / rel).mkdir(parents=True, exist_ok=True)
            dir_count += 1
            if dir_count % 1000 == 0:
                progress_line(f"Scan {label}", dir_count, 0, f"directories; {file_count:,} files")
        for f in adb.iter_find(remote_path, "f", scan_err):
            rows.append(register_storage_file(db, root, f))
            file_count += 1
            if file_count % 500 == 0:
                db.commit()
                progress_line(f"Scan {label}", file_count, 0, Path(f).name)
        db.commit()
    except KeyboardInterrupt:
        db.commit()
        end_progress(f"Scan {label}: INTERRUPTED — {file_count:,} files registered so far")
        raise
    except Exception as exc:
        db.commit()
        logger.log(f"{label}: scan failed: {exc}", "WARN")

    if dir_count >= 1000 or file_count >= 500:
        end_progress(f"Scan {label}: COMPLETE — {file_count:,} files, {dir_count:,} directories")
    else:
        print(f"Scan {label}: COMPLETE — {file_count:,} files, {dir_count:,} directories")
    return rows


def scan_root_catalog(adb: Adb, root: Dict[str, str], logger: Logger, display_label: str) -> List[str]:
    """Enumerate all regular files in one storage root for selective extension filtering."""
    files: List[str] = []

    def scan_err(rc: int, err: str) -> None:
        if rc != 0 or err:
            logger.log(f"Catalog scan for {root['remote']} was partial (rc={rc}): {err[:500]}", "WARN")

    try:
        for path in adb.iter_find(root["remote"], "f", scan_err):
            files.append(path)
            if len(files) % 1000 == 0:
                progress_line(f"Scan {display_label}", len(files), 0, Path(path).name)
    except KeyboardInterrupt:
        end_progress(f"Scan {display_label}: INTERRUPTED — {len(files):,} paths seen")
        raise
    if len(files) >= 1000:
        end_progress(f"Scan {display_label}: COMPLETE — {len(files):,} files indexed for this session")
    else:
        print(f"Scan {display_label}: COMPLETE — {len(files):,} files indexed for this session")
    return files


def queue_extension_category(
    adb: Adb,
    db: StateDB,
    roots: Sequence[Dict[str, str]],
    extensions: set[str],
    logger: Logger,
    category_name: str,
    catalog_cache: Dict[str, List[str]],
) -> Dict[str, List[Tuple[str, str, str]]]:
    selected: Dict[str, List[Tuple[str, str, str]]] = {}
    for root in roots:
        label = root["label"]
        catalog = catalog_cache.get(label)
        if catalog is None:
            catalog = scan_root_catalog(adb, root, logger, f"{category_name}/{label}")
            catalog_cache[label] = catalog
        rows: List[Tuple[str, str, str]] = []
        for path in catalog:
            suffix = Path(path).suffix.lower()
            if suffix in extensions:
                rows.append(register_storage_file(db, root, path))
        db.commit()
        selected[label] = rows
        print(f"{category_name}: {label} — {len(rows):,} matching file(s)")
    return selected


def queue_named_subtrees(
    adb: Adb,
    db: StateDB,
    case: Path,
    roots: Sequence[Dict[str, str]],
    relative_candidates: Sequence[str],
    logger: Logger,
    category_name: str,
) -> Dict[str, List[Tuple[str, str, str]]]:
    selected: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    seen: set[Tuple[str, str]] = set()
    for root in roots:
        for rel in relative_candidates:
            remote = root["remote"].rstrip("/") + "/" + rel.strip("/")
            if not adb.readable_dir(remote):
                continue
            canonical = adb.canonical_path(remote)
            key = (root["label"], canonical)
            if key in seen:
                continue
            seen.add(key)
            rows = queue_subtree(adb, db, case, root, canonical, logger, label=f"{category_name}/{rel}")
            selected[root["label"]].extend(rows)
    return dict(selected)


def discover_storage_files(adb: Adb, db: StateDB, case: Path, root: Dict[str, str], logger: Logger) -> bool:
    label, remote_root = root["label"], root["remote"]
    local_root = case / "storage" / label
    local_root.mkdir(parents=True, exist_ok=True)

    if not adb.supports_find_print0(remote_root):
        logger.log(f"Device find(1) lacks usable -print0 for {remote_root}; will use whole-tree fallback pull", "WARN")
        return False

    logger.log(f"Indexing directories under {remote_root}")
    dir_count = 0

    def dir_err(rc: int, err: str) -> None:
        if rc != 0 or err:
            logger.log(f"Directory scan for {remote_root} was partial (rc={rc}): {err[:500]}", "WARN")

    try:
        for d in adb.iter_find(remote_root, "d", dir_err):
            rel = relative_remote(remote_root, d)
            (local_root / rel).mkdir(parents=True, exist_ok=True)
            dir_count += 1
            if dir_count % 1000 == 0:
                progress_line(f"Index dirs {label}", dir_count, 0, Path(d).name)
    except Exception as exc:
        logger.log(f"Directory discovery failed for {remote_root}: {exc}", "WARN")
    if dir_count >= 1000:
        end_progress(f"Index dirs {label}: {dir_count:,} found")

    logger.log(f"Indexing files under {remote_root}")
    file_count = 0

    def file_err(rc: int, err: str) -> None:
        if rc != 0 or err:
            logger.log(f"File scan for {remote_root} was partial (rc={rc}): {err[:500]}", "WARN")

    try:
        for f in adb.iter_find(remote_root, "f", file_err):
            register_storage_file(db, root, f)
            file_count += 1
            if file_count % 1000 == 0:
                db.commit()
                progress_line(f"Index files {label}", file_count, 0, Path(f).name)
        db.commit()
    except Exception as exc:
        logger.log(f"File discovery failed for {remote_root}: {exc}", "WARN")
        db.commit()

    if file_count >= 1000:
        end_progress(f"Index files {label}: {file_count:,} found")

    logger.log(f"Indexed {file_count:,} files and {dir_count:,} directories in {label}")
    return True


def fallback_pull_root(adb: Adb, case: Path, root: Dict[str, str], timeout: int, logger: Logger) -> None:
    label, remote = root["label"], root["remote"]
    dest = case / "storage" / label
    dest.mkdir(parents=True, exist_ok=True)
    logger.log(f"Fallback recursive pull: {remote} -> {dest}")
    # Pull contents by appending /. so the destination remains the root label.
    if sys.stdout.isatty():
        print(f"Fallback pull {label}: adb will display transfer progress where supported")
        ok, msg = adb.pull_one_live(remote.rstrip("/") + "/.", dest, timeout)
    else:
        ok, msg = adb.pull_one(remote.rstrip("/") + "/.", dest, timeout)
    if ok:
        logger.log(f"Fallback pull completed for {label}")
    else:
        logger.log(f"Fallback pull failed/partial for {label}: {msg[-1000:]}", "ERROR")


def acquire_storage_rows(
    adb: Adb,
    db: StateDB,
    case: Path,
    root: Dict[str, str],
    rows: Sequence[Tuple[str, str, str]],
    timeout: int,
    logger: Logger,
    *,
    operation_name: Optional[str] = None,
    live: bool = False,
) -> Dict[str, int]:
    """Acquire only the supplied storage rows and update resume state."""
    label = root["label"]
    operation = operation_name or label
    # Deduplicate while preserving deterministic local-path ordering.
    unique: Dict[str, Tuple[str, str, str]] = {remote: (remote, local_rel, status) for remote, local_rel, status in rows}
    ordered = sorted(unique.values(), key=lambda x: x[1])

    pending: List[Tuple[str, str]] = []
    skipped = 0
    for remote, local_rel, status in ordered:
        local = case / local_rel
        # The row may have been queued earlier in this session; consult SQLite again
        # so a completed copy is never recopied merely because the in-memory row is stale.
        current_status = db.storage_status(label, remote) or status
        if current_status == "done" and local.exists():
            skipped += 1
            continue
        pending.append((remote, local_rel))

    logger.log(f"{operation}: {len(pending):,} files pending, {skipped:,} already complete")
    if not pending:
        end_progress(f"{operation}: COMPLETE — nothing pending ({skipped:,} already present)")
        return {"copied": 0, "failed": 0, "skipped": skipped, "attempted": 0}

    groups: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    exact_name_items: List[Tuple[str, str]] = []
    for remote, local_rel in pending:
        # adb pull REMOTE... LOCAL_DIR always writes each source basename.  If our
        # collision-safe local filename differs from the Android basename, batch
        # pulling would create/overwrite the wrong name, so force an exact pull.
        if Path(local_rel).name != PurePosixPath(remote).name:
            exact_name_items.append((remote, local_rel))
        else:
            groups[str(Path(local_rel).parent)].append((remote, local_rel))

    completed = 0
    errors = 0
    attempted = 0
    total = len(pending)
    batch_number = 0
    total_batches = sum((len(items) + BATCH_SIZE - 1) // BATCH_SIZE for items in groups.values()) + len(exact_name_items)

    for parent_rel in sorted(groups):
        items = groups[parent_rel]
        for raw_batch in batched(items, BATCH_SIZE):
            batch_number += 1
            batch = list(raw_batch)
            local_parent = case / parent_rel
            remotes = [r for r, _ in batch]
            extra = f"batch {batch_number}/{total_batches}: {Path(parent_rel).name or parent_rel}"
            progress_line(operation, attempted, total, extra)

            if live and sys.stdout.isatty():
                end_progress(f"{operation}: copying batch {batch_number}/{total_batches} ({len(batch)} file(s))")
                ok, msg = adb.pull_many_live(remotes, local_parent, timeout)
            else:
                ok, msg = adb.pull_many(remotes, local_parent, timeout)

            retry: List[Tuple[str, str]] = []
            if ok:
                for remote, local_rel in batch:
                    local = case / local_rel
                    if local.exists():
                        db.set_storage(label, remote, "done", None)
                        completed += 1
                        attempted += 1
                    else:
                        retry.append((remote, local_rel))
                db.commit()
                if retry:
                    logger.log(
                        f"{operation}: batch reported success but {len(retry)} expected files were absent; retrying individually",
                        "WARN",
                    )
            else:
                retry = batch
                logger.log(f"{operation}: batch pull failed; retrying individually: {msg[-500:]}", "WARN")

            for remote, local_rel in retry:
                local = case / local_rel
                progress_line(operation, attempted, total, f"retry: {Path(local_rel).name}")
                if live and sys.stdout.isatty():
                    end_progress(f"{operation}: copying {remote}")
                    ok1, msg1 = adb.pull_one_live(remote, local, timeout)
                else:
                    ok1, msg1 = adb.pull_one(remote, local, timeout)
                if ok1:
                    db.set_storage(label, remote, "done", None)
                    completed += 1
                else:
                    err = msg1[-2000:] if msg1 else "adb pull failed"
                    db.set_storage(label, remote, "error", err)
                    errors += 1
                    logger.log(f"Could not acquire {remote}: {err[-300:]}", "WARN")
                attempted += 1
                db.commit()

            progress_line(operation, attempted, total, f"copied={completed:,} failed={errors:,}")

    # Collision-safe/renamed destinations must be copied to their exact local path.
    for remote, local_rel in exact_name_items:
        batch_number += 1
        local = case / local_rel
        progress_line(operation, attempted, total, f"exact {batch_number}/{total_batches}: {Path(local_rel).name}")
        if live and sys.stdout.isatty():
            end_progress(f"{operation}: copying {remote} -> {Path(local_rel).name}")
            ok1, msg1 = adb.pull_one_live(remote, local, timeout)
        else:
            ok1, msg1 = adb.pull_one(remote, local, timeout)
        if ok1:
            db.set_storage(label, remote, "done", None)
            completed += 1
        else:
            err = msg1[-2000:] if msg1 else "adb pull failed"
            db.set_storage(label, remote, "error", err)
            errors += 1
            logger.log(f"Could not acquire {remote}: {err[-300:]}", "WARN")
        attempted += 1
        db.commit()
        progress_line(operation, attempted, total, f"copied={completed:,} failed={errors:,}")

    end_progress(
        f"{operation}: COMPLETE — copied={completed:,}, failed={errors:,}, skipped={skipped:,}, considered={len(ordered):,}"
    )
    logger.log(f"{operation}: acquisition pass complete; copied={completed:,}, errors={errors:,}, skipped={skipped:,}")
    return {"copied": completed, "failed": errors, "skipped": skipped, "attempted": attempted}


def acquire_storage_root(adb: Adb, db: StateDB, case: Path, root: Dict[str, str], timeout: int, logger: Logger) -> None:
    rows = db.storage_rows(root["label"])
    acquire_storage_rows(
        adb,
        db,
        case,
        root,
        rows,
        timeout,
        logger,
        operation_name=f"Shared storage {root['label']}",
        live=sys.stdout.isatty(),
    )

def parse_package_names(text: str) -> List[str]:
    packages = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            # pm list packages may emit package:/path/base.apk=com.pkg when -f is used.
            payload = line[len("package:") :]
            if "=" in payload:
                pkg = payload.rsplit("=", 1)[1].split()[0]
            else:
                pkg = payload.split()[0]
            if re.fullmatch(r"[A-Za-z0-9_.]+", pkg):
                packages.append(pkg)
    return sorted(set(packages))


def get_packages(adb: Adb, third_party_only: bool) -> List[str]:
    cmd = "pm list packages -3" if third_party_only else "pm list packages"
    return parse_package_names(adb.shell_text(cmd, timeout=180))


def acquire_apks(adb: Adb, db: StateDB, case: Path, mode: str, timeout: int, logger: Logger) -> None:
    if mode == "none":
        logger.log("APK acquisition disabled")
        return
    third_party = mode == "third-party"
    packages = get_packages(adb, third_party)
    logger.log(f"Discovering APK paths for {len(packages):,} {'third-party' if third_party else 'all'} packages")

    for i, pkg in enumerate(packages, 1):
        progress_line("APK inventory", i - 1, len(packages), pkg)
        p = adb.shell(f"pm path {shell_quote(pkg)}", timeout=60)
        if p.returncode != 0:
            continue
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            remote = line[len("package:") :]
            base = Path(remote).name or "base.apk"
            local_rel = str(Path("apps") / "apks" / pkg / base)
            db.add_artifact("apk", pkg, remote, local_rel)
        if i % 100 == 0:
            db.commit()
        progress_line("APK inventory", i, len(packages), pkg)
    db.commit()
    end_progress(f"APK inventory: COMPLETE — {len(packages):,} package(s) inspected")

    rows = list(
        db.conn.execute(
            "SELECT package_name,remote_path,local_rel,status FROM app_artifacts WHERE kind='apk' ORDER BY package_name,remote_path"
        )
    )
    done = errors = skipped = 0
    total_rows = len(rows)
    processed = 0
    for pkg, remote, local_rel, status in rows:
        local = case / local_rel
        if status == "done" and local.exists():
            skipped += 1
            processed += 1
            progress_line("APK copy", processed, total_rows, f"skipped {pkg}")
            continue
        progress_line("APK copy", processed, total_rows, pkg)
        if sys.stdout.isatty():
            end_progress(f"APK copy: {pkg} — {Path(remote).name}")
            ok, msg = adb.pull_one_live(remote, local, timeout)
        else:
            ok, msg = adb.pull_one(remote, local, timeout)
        if ok:
            db.set_artifact("apk", pkg, remote, "done", None)
            done += 1
        else:
            db.set_artifact("apk", pkg, remote, "error", msg[-2000:] if msg else "adb pull failed")
            errors += 1
        db.commit()
        processed += 1
        progress_line("APK copy", processed, total_rows, f"copied={done:,} failed={errors:,} skipped={skipped:,}")
    end_progress(f"APK copy: COMPLETE — copied={done:,}, failed={errors:,}, skipped={skipped:,}")
    logger.log(f"APK acquisition complete: {done:,} copied, {errors:,} failed, {skipped:,} previously complete")


def archive_debuggable_private_data(adb: Adb, case: Path, timeout: int, logger: Logger) -> None:
    packages = get_packages(adb, third_party_only=True)
    out_dir = case / "apps" / "debuggable_private"
    out_dir.mkdir(parents=True, exist_ok=True)
    status: Dict[str, Dict[str, object]] = {}
    logger.log(f"Testing {len(packages):,} third-party packages for Android-authorized run-as access")

    for idx, pkg in enumerate(packages, 1):
        progress_line("App data probe", idx - 1, len(packages), pkg)
        probe = adb.run(["shell", "run-as", pkg, "pwd"], timeout=20, text=True, capture=True)
        if probe.returncode != 0:
            status[pkg] = {"accessible": False, "reason": (probe.stderr or probe.stdout or "run-as denied").strip()[:500]}
            progress_line("App data probe", idx, len(packages), pkg)
            continue

        tar_path = out_dir / f"{safe_name(pkg)}.tar"
        if tar_path.exists() and tar_path.stat().st_size > 0:
            status[pkg] = {"accessible": True, "archived": True, "path": str(tar_path.relative_to(case)), "resumed": True}
            progress_line("App data probe", idx, len(packages), f"{pkg} (already archived)")
            continue

        logger.log(f"run-as permitted for {pkg}; archiving its private app directory")
        try:
            rc, err = run_stream_to_file_with_progress(
                adb.prefix + ["exec-out", "run-as", pkg, "tar", "-cf", "-", "."],
                tar_path,
                label=f"App data archive {pkg}",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            rc, err = 124, f"archive timed out after {timeout}s"
        valid = False
        if rc == 0 and tar_path.exists() and tar_path.stat().st_size > 0:
            try:
                with tarfile.open(tar_path, "r:*") as tf:
                    # Validate that the archive has at least a readable TAR header without
                    # loading a potentially huge member index into memory.
                    tf.next()
                valid = True
            except Exception as exc:
                err = f"Archive validation failed: {exc}; {err}".strip()
        if valid:
            status[pkg] = {"accessible": True, "archived": True, "path": str(tar_path.relative_to(case))}
        else:
            try:
                tar_path.unlink(missing_ok=True)
            except Exception:
                pass
            status[pkg] = {"accessible": True, "archived": False, "reason": err or f"tar returned {rc}"}

        if idx % 50 == 0:
            atomic_json(out_dir / "status.json", status)
        progress_line("App data probe", idx, len(packages), pkg)

    atomic_json(out_dir / "status.json", status)
    successful = sum(1 for v in status.values() if v.get("archived"))
    end_progress(f"App data probe: COMPLETE — {successful} package(s) archived through Android run-as")
    logger.log(f"Private app-data pass complete: {successful} package(s) archived through run-as; other release apps remain sandboxed")


def export_content_providers(adb: Adb, case: Path, logger: Logger) -> Dict[str, Dict[str, object]]:
    out_dir = case / "providers"
    out_dir.mkdir(parents=True, exist_ok=True)
    queries = {
        "call_log": "content query --uri content://call_log/calls",
        "sms": "content query --uri content://sms/",
        "mms": "content query --uri content://mms/",
        "contacts_phones": "content query --uri content://contacts/phones/",
        "calendar_events": "content query --uri content://com.android.calendar/events",
    }
    status: Dict[str, Dict[str, object]] = {}
    logger.log("Attempting read-only content-provider exports allowed to the ADB shell")
    total = len(queries)
    for idx, (name, command) in enumerate(queries.items(), 1):
        progress_line("Providers", idx - 1, total, name)
        try:
            p = adb.shell(command, timeout=180)
            text = p.stdout or ""
            if p.stderr:
                text += "\n--- STDERR ---\n" + p.stderr
            write_text(out_dir / f"{name}.txt", text)
            denied_hint = any(s in text.lower() for s in ["permission denial", "securityexception", "not allowed", "error while accessing provider", "unknown uri"])
            ok = p.returncode == 0 and not denied_hint
            status[name] = {
                "ok": ok,
                "returncode": p.returncode,
                "bytes": len(text.encode("utf-8", "replace")),
                "note": "Provider may legitimately contain zero rows" if ok and not text.strip() else "",
            }
            if not ok:
                logger.log(f"Provider {name} was not readable on this device (expected on many Android builds)", "WARN")
        except Exception as exc:
            status[name] = {"ok": False, "error": str(exc)}
            write_text(out_dir / f"{name}.txt", f"ERROR: {exc}\n")
            logger.log(f"Provider export failed for {name}: {exc}", "WARN")
        progress_line("Providers", idx, total, name)
    atomic_json(out_dir / "status.json", status)
    readable = sum(1 for info in status.values() if info.get("ok"))
    end_progress(f"Providers: COMPLETE — {readable}/{total} readable/exported")
    return status


def run_bugreport(adb: Adb, case: Path, timeout: int, logger: Logger) -> None:
    dest = case / "bugreport"
    dest.mkdir(parents=True, exist_ok=True)
    logger.log("Generating optional Android bugreport (can be large and contain sensitive diagnostic data)")
    print("Bugreport: STARTED — adb will display its own progress where supported")
    try:
        rc = run_cancellable_passthrough(adb.prefix + ["bugreport", str(dest)], timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.log(f"Bugreport timed out after {timeout}s", "WARN")
        print("Bugreport: FAILED — timed out")
        return
    if rc == 0:
        logger.log("Bugreport completed")
        print("Bugreport: COMPLETE")
    else:
        logger.log(f"Bugreport failed with adb return code {rc}", "WARN")
        print(f"Bugreport: FAILED — adb return code {rc}")


def launch_scrcpy(serial: str, case: Path, logger: Logger) -> Optional[subprocess.Popen]:
    scrcpy = shutil.which("scrcpy")
    if not scrcpy:
        logger.log("--scrcpy requested but scrcpy is not installed/found in PATH", "WARN")
        return None
    log_path = case / "logs" / "scrcpy.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fp = log_path.open("ab", buffering=0)
    logger.log("Launching scrcpy for interactive, owner-authorized access; acquisition continues independently")
    try:
        proc = subprocess.Popen([scrcpy, "-s", serial], stdout=fp, stderr=subprocess.STDOUT, start_new_session=True)
        return proc
    except Exception as exc:
        fp.close()
        logger.log(f"Could not launch scrcpy: {exc}", "WARN")
        return None


def generate_hash_manifest(case: Path, logger: Logger) -> Tuple[int, int]:
    manifest_dir = case / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    csv_path = manifest_dir / "files.csv"
    sums_path = manifest_dir / "SHA256SUMS.txt"

    roots_to_hash = ["storage", "apps", "providers", "metadata", "bugreport"]
    files: List[Path] = []
    for root_name in roots_to_hash:
        root = case / root_name
        if root.exists():
            files.extend(p for p in root.rglob("*") if p.is_file())
    files = sorted(set(files), key=lambda p: str(p.relative_to(case)))

    logger.log(f"Hashing {len(files):,} acquired files with SHA-256")
    total_bytes = 0
    with csv_path.open("w", newline="", encoding="utf-8") as csv_fp, sums_path.open("w", encoding="utf-8") as sum_fp:
        writer = csv.writer(csv_fp)
        writer.writerow(["relative_path", "size_bytes", "mtime_local", "sha256"])
        for i, path in enumerate(files, 1):
            progress_line("SHA-256", i - 1, len(files), str(path.relative_to(case)))
            try:
                stat = path.stat()
                digest = sha256_file(path)
                rel = str(path.relative_to(case))
                writer.writerow([rel, stat.st_size, int(stat.st_mtime), digest])
                sum_fp.write(f"{digest}  {rel}\n")
                total_bytes += stat.st_size
            except Exception as exc:
                logger.log(f"Could not hash {path}: {exc}", "WARN")
            progress_line("SHA-256", i, len(files), str(path.relative_to(case)))
    end_progress(f"SHA-256: COMPLETE — {len(files):,} files, {human_bytes(total_bytes)}")
    logger.log(f"Hash manifest complete: {len(files):,} files, {total_bytes / (1024**3):.2f} GiB")
    return len(files), total_bytes


def write_failures_report(db: StateDB, case: Path) -> int:
    failures = db.failures()
    path = case / "reports" / "failures.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(["category", "remote_path", "local_path", "error"])
        writer.writerows(failures)
    return len(failures)


def write_storage_path_map(db: StateDB, case: Path) -> int:
    """Record source Android paths and their exact local destinations/status."""
    rows = db.storage_path_map()
    path = case / "manifests" / "remote_to_local.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(["storage_root", "remote_android_path", "local_relative_path", "status"])
        writer.writerows(rows)
    return len(rows)


def case_identity(adb: Adb) -> Dict[str, str]:
    return {
        "adb_serial": adb.serial,
        "manufacturer": adb.getprop("ro.product.manufacturer"),
        "model": adb.getprop("ro.product.model"),
        "device": adb.getprop("ro.product.device"),
        "android_version": adb.getprop("ro.build.version.release"),
        "sdk": adb.getprop("ro.build.version.sdk"),
        "build_fingerprint": adb.getprop("ro.build.fingerprint"),
        "ro_serialno": adb.getprop("ro.serialno"),
    }


def make_case_dir(base: Path, identity: Dict[str, str], case_name: Optional[str]) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if case_name:
        name = safe_name(case_name)
    else:
        mfr = safe_name(identity.get("manufacturer", "android"), "android")
        model = safe_name(identity.get("model", "device"), "device")
        serial = safe_name(identity.get("ro_serialno") or identity.get("adb_serial", "unknown"), "unknown")
        name = f"{mfr}_{model}_{serial}_{stamp}"
    return base.expanduser().resolve() / name


def compare_resume_identity(existing: Dict[str, object], current: Dict[str, str]) -> List[str]:
    mismatches = []
    old = existing.get("device_identity") if isinstance(existing, dict) else None
    if not isinstance(old, dict):
        return mismatches
    for key in ["build_fingerprint", "ro_serialno", "model"]:
        ov = str(old.get(key, ""))
        nv = str(current.get(key, ""))
        if ov and nv and ov != nv:
            mismatches.append(f"{key}: existing={ov!r}, current={nv!r}")
    return mismatches


def write_summary(
    case: Path,
    identity: Dict[str, str],
    db: StateDB,
    provider_status: Dict[str, Dict[str, object]],
    hash_count: Optional[int],
    hash_bytes: Optional[int],
    failures: int,
    started: str,
    completed: str,
) -> None:
    counts = db.counts()
    provider_lines = []
    for name, info in provider_status.items():
        provider_lines.append(f"  - {name}: {'readable/exported' if info.get('ok') else 'not available/denied'}")
    if not provider_lines:
        provider_lines = ["  - provider export was disabled"]

    text = f"""ANDROID RESCUE ACQUISITION SUMMARY
==================================
Tool version: {VERSION}
Started:      {started}
Completed:    {completed}

DEVICE
------
Manufacturer: {identity.get('manufacturer','')}
Model:        {identity.get('model','')}
Android:      {identity.get('android_version','')} (SDK {identity.get('sdk','')})
ADB serial:   {identity.get('adb_serial','')}
Build:        {identity.get('build_fingerprint','')}

RESULTS
-------
Storage files complete: {counts.get('storage_done', 0):,}
Storage files failed:   {counts.get('storage_error', 0):,}
Storage files pending:  {counts.get('storage_pending', 0):,}
App artifacts complete: {counts.get('app_done', 0):,}
App artifacts failed:   {counts.get('app_error', 0):,}
Recorded failures:      {failures:,}
Hashed files:           {(f'{hash_count:,}' if hash_count is not None else 'hashing disabled')}
Hashed bytes:           {(f'{hash_bytes / (1024**3):.2f} GiB' if hash_bytes is not None else 'hashing disabled')}

CONTENT PROVIDERS (best effort; varies by Android/OEM policy)
----------------------------------------------------------
{os.linesep.join(provider_lines)}

IMPORTANT LIMITATIONS
---------------------
- This is an ADB-readable rescue/acquisition, not a bit-for-bit physical image.
- Normal release-app private data under /data/user/* or /data/data/* is sandboxed.
- apps/debuggable_private contains data only where Android itself permitted `run-as`.
- Private Space, Secure Folder, encrypted vaults, screen locks, and vault passwords are not bypassed.
- Hardware-backed Android Keystore secrets generally cannot be cloned into an emulator.
- A file copied from an encrypted/private app does not guarantee that the app can decrypt it on another device.
- Keep the source phone until critical rescued data has been opened and verified elsewhere.
- The acquisition is live: files may change while the phone is in use; this is not a frozen forensic snapshot.

LAYOUT
------
storage/                    Readable shared/emulated/removable storage
apps/apks/                  APK and split APK files selected for preservation
apps/debuggable_private/    Private app archives only when Android run-as allowed access
providers/                  Best-effort call/SMS/MMS/contacts/calendar provider exports
metadata/                   Device, package, user/profile, settings and storage metadata
manifests/                  SHA-256 integrity manifests
reports/failures.csv        Items that could not be acquired
logs/acquisition.log        Acquisition log
.state/rescue.sqlite3       Resume state (local bookkeeping only)

For owner-authorized protected content, use scrcpy to open the relevant app/profile,
use its legitimate export/move/share function, then rerun this tool with --resume so
newly exported files are discovered and acquired.
"""
    write_text(case / "reports" / "SUMMARY.txt", text)


def choose_mode(requested: Optional[str]) -> str:
    if requested:
        return requested
    if not sys.stdin.isatty():
        return "auto"
    print(
        textwrap.dedent(
            """
            ACQUISITION MODE
              1. Auto Fetch Everything
                 Comprehensive pass: shared/removable storage, APKs, Android-authorized
                 debuggable app data, provider exports, metadata, bugreport and hashes.

              2. Priority / Selective Fetch
                 Choose what to rescue first. Return to the menu after each operation.
                 Ideal when battery, device stability, or available time is limited.
            """
        ).strip()
    )
    while True:
        choice = input("Select mode [1/2]: ").strip().lower()
        if choice in {"1", "auto", "a"}:
            return "auto"
        if choice in {"2", "selective", "priority", "s"}:
            return "selective"


def acquire_selected_map(
    adb: Adb,
    db: StateDB,
    case: Path,
    roots: Sequence[Dict[str, str]],
    selected: Dict[str, List[Tuple[str, str, str]]],
    timeout: int,
    logger: Logger,
    operation_name: str,
) -> Dict[str, int]:
    root_by_label = {r["label"]: r for r in roots}
    totals = {"copied": 0, "failed": 0, "skipped": 0, "attempted": 0}
    total_candidates = sum(len(rows) for rows in selected.values())
    if total_candidates == 0:
        print(f"{operation_name}: nothing matching was found in ADB-readable storage.")
        return totals
    print(f"{operation_name}: {total_candidates:,} file(s) selected across {len(selected)} storage root(s).")
    for label, rows in selected.items():
        root = root_by_label.get(label)
        if not root or not rows:
            continue
        result = acquire_storage_rows(
            adb,
            db,
            case,
            root,
            rows,
            timeout,
            logger,
            operation_name=f"{operation_name}/{label}",
            live=sys.stdout.isatty(),
        )
        for key in totals:
            totals[key] += result.get(key, 0)
    print(
        f"{operation_name}: DONE — copied={totals['copied']:,}, failed={totals['failed']:,}, "
        f"already complete={totals['skipped']:,}."
    )
    return totals


def manual_browser(
    adb: Adb,
    db: StateDB,
    case: Path,
    roots: Sequence[Dict[str, str]],
    timeout: int,
    logger: Logger,
) -> None:
    if not roots:
        print("No ADB-readable storage roots are available.")
        return

    if len(roots) == 1:
        root = roots[0]
    else:
        print("\nStorage roots:")
        for idx, item in enumerate(roots, 1):
            print(f"  {idx}. {item['label']}: {item['remote']}")
        while True:
            raw = input("Choose storage root (or q): ").strip().lower()
            if raw == "q":
                return
            if raw.isdigit() and 1 <= int(raw) <= len(roots):
                root = roots[int(raw) - 1]
                break

    root_path = root["remote"].rstrip("/") or "/"
    current = root_path
    page = 0
    page_size = 30

    def list_current() -> List[str]:
        errors: List[str] = []
        sys.stdout.write(f"\rReading {current} ...\033[K")
        sys.stdout.flush()

        def sink(rc: int, err: str) -> None:
            if rc != 0 or err:
                errors.append(err or f"find returned {rc}")

        entries = list(adb.iter_shallow(current, sink))
        entries.sort(key=lambda x: Path(x).name.casefold())
        sys.stdout.write(f"\rRead {len(entries):,} item(s) from {current}\033[K\n")
        sys.stdout.flush()
        if errors:
            logger.log(f"Browser listing for {current} was partial: {errors[-1][:500]}", "WARN")
        return entries

    while True:
        try:
            entries = list_current()
            max_page = max(0, (len(entries) - 1) // page_size)
            page = min(page, max_page)
            start = page * page_size
            visible = entries[start : start + page_size]
            print("\n" + "=" * 78)
            print(f"PHONE STORAGE BROWSER  [{root['label']}]")
            print(f"Path: {current}")
            print(f"Entries: {len(entries):,}   Page: {page + 1}/{max_page + 1}")
            print("-" * 78)
            for offset, remote in enumerate(visible, start + 1):
                kind = adb.path_kind(remote)
                marker = "/" if kind == "D" else ""
                size_text = ""
                if kind == "F":
                    size = adb.file_size(remote)
                    if size is not None:
                        size_text = f"  [{human_bytes(size)}]"
                print(f"{offset:>4}. {Path(remote).name}{marker}{size_text}")
            print("-" * 78)
            print("Enter N = open directory / copy file")
            print("c N     = copy that file or whole directory")
            print("c .     = copy current directory")
            print("..      = parent directory   n/p = next/previous page   r = refresh")
            print("q       = leave browser")
            raw = input("browser> ").strip()
            if not raw:
                continue
            low = raw.lower()
            if low == "q":
                return
            if low == "n":
                if page < max_page:
                    page += 1
                continue
            if low == "p":
                if page > 0:
                    page -= 1
                continue
            if low == "r":
                continue
            if low == "..":
                if current != root_path:
                    parent = str(PurePosixPath(current).parent)
                    if parent == "." or not parent.startswith(root_path):
                        parent = root_path
                    current = parent
                    page = 0
                continue

            copy_target: Optional[str] = None
            if low == "c .":
                copy_target = current
            elif low.startswith("c "):
                token = low[2:].strip()
                if token.isdigit() and 1 <= int(token) <= len(entries):
                    copy_target = entries[int(token) - 1]
                else:
                    print("Invalid item number.")
                    continue
            elif low.isdigit() and 1 <= int(low) <= len(entries):
                chosen = entries[int(low) - 1]
                kind = adb.path_kind(chosen)
                if kind == "D":
                    current = chosen.rstrip("/")
                    page = 0
                    continue
                if kind == "F":
                    copy_target = chosen
                else:
                    print("That item is not a regular file or readable directory.")
                    continue
            else:
                print("Unknown command.")
                continue

            if copy_target:
                print(f"\nSelected for copy: {copy_target}")
                rows = queue_subtree(
                    adb,
                    db,
                    case,
                    root,
                    copy_target,
                    logger,
                    label=f"manual:{Path(copy_target).name or root['label']}",
                )
                if not rows:
                    print("Nothing copyable was found at that path.")
                    continue
                acquire_storage_rows(
                    adb,
                    db,
                    case,
                    root,
                    rows,
                    timeout,
                    logger,
                    operation_name=f"Manual copy {Path(copy_target).name or root['label']}",
                    live=True,
                )
                print(f"Manual copy completed. Saved under: {case / 'storage' / root['label']}")
        except KeyboardInterrupt:
            db.commit()
            print("\nCurrent browser/copy operation interrupted safely. Partial completed files remain resumable.")
            continue
        except EOFError:
            return


def selective_menu(
    adb: Adb,
    db: StateDB,
    case: Path,
    roots: Sequence[Dict[str, str]],
    timeout: int,
    logger: Logger,
    provider_status: Dict[str, Dict[str, object]],
    scrcpy_proc: Optional[subprocess.Popen],
) -> Tuple[Dict[str, Dict[str, object]], Optional[subprocess.Popen], Optional[int], Optional[int]]:
    catalog_cache: Dict[str, List[str]] = {}
    last_hash_count: Optional[int] = None
    last_hash_bytes: Optional[int] = None

    app_media_candidates = [
        "Android/media",
        "WhatsApp",
        "Telegram",
        "Signal",
        "Viber",
        "LINE",
        "Kik",
        "WeChat",
    ]
    download_candidates = ["Download", "Downloads"]

    while True:
        counts = db.counts()
        print(
            textwrap.dedent(
                f"""
                \nPRIORITY / SELECTIVE FETCH
                Case: {case}
                Completed storage files: {counts.get('storage_done', 0):,}   Failed: {counts.get('storage_error', 0):,}

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
                 R. Refresh selective file catalog (use after exporting new files)
                 F. Finish selective session

                Ctrl-C interrupts the current operation and returns here where possible.
                """
            ).strip()
        )
        try:
            choice = input("Select what to fetch: ").strip().lower()
        except KeyboardInterrupt:
            print("\nUse F to finish cleanly, or select another item.")
            continue
        except EOFError:
            choice = "f"

        try:
            if choice == "1":
                selected = queue_extension_category(adb, db, roots, IMAGE_EXTENSIONS, logger, "Photos", catalog_cache)
                acquire_selected_map(adb, db, case, roots, selected, timeout, logger, "Photos")
            elif choice == "2":
                selected = queue_extension_category(adb, db, roots, VIDEO_EXTENSIONS, logger, "Videos", catalog_cache)
                acquire_selected_map(adb, db, case, roots, selected, timeout, logger, "Videos")
            elif choice == "3":
                selected = queue_extension_category(adb, db, roots, AUDIO_EXTENSIONS, logger, "Audio", catalog_cache)
                acquire_selected_map(adb, db, case, roots, selected, timeout, logger, "Audio")
            elif choice == "4":
                selected = queue_extension_category(adb, db, roots, DOCUMENT_EXTENSIONS, logger, "Documents", catalog_cache)
                acquire_selected_map(adb, db, case, roots, selected, timeout, logger, "Documents")
            elif choice == "5":
                selected = queue_named_subtrees(adb, db, case, roots, download_candidates, logger, "Downloads")
                acquire_selected_map(adb, db, case, roots, selected, timeout, logger, "Downloads")
            elif choice == "6":
                selected = queue_named_subtrees(adb, db, case, roots, app_media_candidates, logger, "App media")
                acquire_selected_map(adb, db, case, roots, selected, timeout, logger, "App media")
            elif choice == "7":
                selected = queue_extension_category(
                    adb, db, roots, BACKUP_ARCHIVE_EXTENSIONS, logger, "Backups/archives", catalog_cache
                )
                acquire_selected_map(adb, db, case, roots, selected, timeout, logger, "Backups/archives")
            elif choice == "8":
                acquire_apks(adb, db, case, "all", timeout, logger)
            elif choice == "9":
                archive_debuggable_private_data(adb, case, timeout, logger)
            elif choice == "10":
                provider_status.update(export_content_providers(adb, case, logger))
            elif choice == "11":
                collect_metadata(adb, case, logger)
            elif choice == "12":
                run_bugreport(adb, case, timeout, logger)
            elif choice == "13":
                for root in roots:
                    indexed = discover_storage_files(adb, db, case, root, logger)
                    if indexed:
                        acquire_storage_root(adb, db, case, root, timeout, logger)
                    else:
                        fallback_pull_root(adb, case, root, timeout, logger)
            elif choice == "14":
                manual_browser(adb, db, case, roots, timeout, logger)
            elif choice == "s":
                if scrcpy_proc is not None and scrcpy_proc.poll() is None:
                    print("scrcpy is already running.")
                else:
                    scrcpy_proc = launch_scrcpy(adb.serial, case, logger)
            elif choice == "h":
                last_hash_count, last_hash_bytes = generate_hash_manifest(case, logger)
            elif choice == "r":
                catalog_cache.clear()
                print("Selective file catalog cleared. The next Photos/Videos/Audio/Documents/Backups choice will rescan the phone.")
            elif choice == "f":
                return provider_status, scrcpy_proc, last_hash_count, last_hash_bytes
            else:
                print("Invalid selection.")
        except KeyboardInterrupt:
            db.commit()
            print("\nOperation interrupted safely. Completed files remain recorded; returning to the selective menu.")
        except Exception as exc:
            logger.log(f"Selective operation failed: {exc}", "ERROR")
            print(f"Operation failed: {exc}\nReturning to the menu; other rescue options remain available.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Read-only Android rescue/acquisition over ADB for macOS.",
        epilog=textwrap.dedent(
            """
            Examples:
              python3 android_rescue.py --scrcpy
              python3 android_rescue.py --mode auto --serial 192.168.1.20:40211 --scrcpy
              python3 android_rescue.py --mode selective --serial 192.168.1.20:40211 --scrcpy
              python3 android_rescue.py --pair 192.168.1.20:37123 --connect 192.168.1.20:40211
              python3 android_rescue.py --resume ~/AndroidRescue/MyPhone_20260913_101500 --mode selective

            Wireless pairing ports and connection ports shown by Android may differ.
            This tool does not require internet access; ADB and the Mac only need network reachability.
            """
        ),
    )
    p.add_argument("--serial", help="ADB device serial, e.g. 192.168.1.20:40211")
    p.add_argument("--mode", choices=["auto", "selective"], help="Acquisition mode; if omitted an interactive menu is shown")
    p.add_argument("--pair", metavar="HOST:PORT", help="Run `adb pair` first; pairing code is requested interactively by adb")
    p.add_argument("--connect", metavar="HOST:PORT", help="Run `adb connect HOST:PORT` before selecting the device")
    p.add_argument("--output", default="~/AndroidRescue", help="Base directory for new cases (default: ~/AndroidRescue)")
    p.add_argument("--case-name", help="Optional name for a new acquisition directory")
    p.add_argument("--resume", type=Path, help="Resume/re-scan an existing acquisition directory")
    p.add_argument("--force-resume", action="store_true", help="Resume even if stored device identity does not match")
    p.add_argument("--authorized", action="store_true", help="Confirm non-interactively that you own/have explicit authorization for the device")
    p.add_argument("--scrcpy", action="store_true", help="Launch scrcpy while the acquisition runs")
    p.add_argument("--apk-mode", choices=["none", "third-party", "all"], default="all", help="APK preservation mode (default: all)")
    p.add_argument("--no-debuggable-data", action="store_true", help="Do not attempt run-as archives for debuggable third-party apps")
    p.add_argument("--no-providers", action="store_true", help="Do not attempt call/SMS/MMS/contacts/calendar content-provider exports")
    p.add_argument(
        "--bugreport",
        action="store_true",
        help="Force a bugreport at the end of Selective mode; Auto mode already includes one unless --no-bugreport",
    )
    p.add_argument("--no-bugreport", action="store_true", help="Skip bugreport in Auto Fetch Everything mode")
    p.add_argument("--no-hash", action="store_true", help="Skip SHA-256 manifests")
    p.add_argument("--pull-timeout", type=int, default=3600, help="Seconds allowed for each ADB pull/archive operation (default: 3600)")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    started = now_iso()
    if args.bugreport and args.no_bugreport:
        raise RuntimeError("Use either --bugreport or --no-bugreport, not both.")
    require_authorization(args.authorized)

    adb_bin = require_program("adb")
    adb_global(adb_bin, ["start-server"], timeout=30)

    if args.pair:
        if not sys.stdin.isatty():
            raise RuntimeError("--pair requires an interactive terminal so adb can request the pairing code.")
        print(f"Pairing with {args.pair} ...")
        rc = subprocess.call([adb_bin, "pair", args.pair])
        if rc != 0:
            raise RuntimeError("adb pair failed")

    if args.connect:
        p = adb_global(adb_bin, ["connect", args.connect], timeout=30)
        print((p.stdout or p.stderr or "").strip())
        if p.returncode != 0 or "failed" in (p.stdout or "").lower():
            raise RuntimeError(f"adb connect failed for {args.connect}")

    requested_serial = args.serial or args.connect
    serial = choose_device(adb_bin, requested_serial)

    temp_dir = Path(tempfile.mkdtemp(prefix="android_rescue_"))
    temp_logger = Logger(temp_dir / "acquisition.log")
    adb_temp = Adb(adb_bin, serial, temp_logger)
    identity = case_identity(adb_temp)

    if args.resume:
        case = args.resume.expanduser().resolve()
        if not case.exists() or not case.is_dir():
            raise RuntimeError(f"Resume directory does not exist: {case}")
    else:
        case = make_case_dir(Path(args.output), identity, args.case_name)
        case.mkdir(parents=True, exist_ok=False)

    temp_logger.close()
    shutil.rmtree(temp_dir, ignore_errors=True)
    logger = Logger(case / "logs" / "acquisition.log")
    adb = Adb(adb_bin, serial, logger)
    db = StateDB(case / ".state" / "rescue.sqlite3")

    acquisition_path = case / "metadata" / "acquisition.json"
    existing: Dict[str, object] = {}
    if acquisition_path.exists():
        try:
            existing = json.loads(acquisition_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    if args.resume:
        mismatches = compare_resume_identity(existing, identity)
        if mismatches and not args.force_resume:
            raise RuntimeError(
                "Resume safety check found a different device/build:\n  "
                + "\n  ".join(mismatches)
                + "\nUse --force-resume only if this is intentional."
            )

    mode = choose_mode(args.mode)
    acquisition_meta = {
        "tool": "android_rescue.py",
        "tool_version": VERSION,
        "started": existing.get("started", started),
        "last_run_started": started,
        "status": "running",
        "mode": mode,
        "device_identity": identity,
        "options": {
            "apk_mode": args.apk_mode,
            "debuggable_data": not args.no_debuggable_data,
            "providers": not args.no_providers,
            "bugreport_forced": args.bugreport,
            "bugreport_skipped_in_auto": args.no_bugreport,
            "hash": not args.no_hash,
            "scrcpy": args.scrcpy,
        },
        "safety": "Read/query/pull only toward the source, except interactive actions the authorized operator performs manually through scrcpy.",
    }
    atomic_json(acquisition_path, acquisition_meta)

    logger.log(f"Android Rescue v{VERSION}")
    logger.log(f"Mode: {mode}")
    logger.log(f"Device: {identity.get('manufacturer')} {identity.get('model')} / {serial}")
    logger.log(f"Case directory: {case}")
    logger.log("Source policy: read/query/pull only; no rooting, deletion, app clearing, remounting, or lock/vault bypass")

    scrcpy_proc: Optional[subprocess.Popen] = None
    provider_status: Dict[str, Dict[str, object]] = {}
    provider_status_path = case / "providers" / "status.json"
    if provider_status_path.exists():
        try:
            raw_status = json.loads(provider_status_path.read_text(encoding="utf-8"))
            if isinstance(raw_status, dict):
                provider_status = raw_status
        except Exception:
            pass
    hash_count: Optional[int] = None
    hash_bytes: Optional[int] = None

    def handle_signal(signum, _frame):
        logger.log(f"Received signal {signum}; committing local resume state", "WARN")
        try:
            db.commit()
        except Exception:
            pass
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        if args.scrcpy:
            scrcpy_proc = launch_scrcpy(serial, case, logger)

        if mode == "auto":
            print("\nAUTO FETCH EVERYTHING — starting comprehensive acquisition. Ctrl-C stops safely and leaves a resumable case.\n")
            collect_metadata(adb, case, logger)
            roots = discover_storage_roots(adb, case, logger)

            for root in roots:
                indexed = discover_storage_files(adb, db, case, root, logger)
                if indexed:
                    acquire_storage_root(adb, db, case, root, args.pull_timeout, logger)
                else:
                    fallback_pull_root(adb, case, root, args.pull_timeout, logger)

            acquire_apks(adb, db, case, args.apk_mode, args.pull_timeout, logger)

            if not args.no_debuggable_data:
                archive_debuggable_private_data(adb, case, args.pull_timeout, logger)
            else:
                logger.log("Android-authorized run-as app-data archive pass disabled by --no-debuggable-data")

            if not args.no_providers:
                provider_status = export_content_providers(adb, case, logger)
            else:
                logger.log("Content-provider export pass disabled by --no-providers")

            if not args.no_bugreport:
                run_bugreport(adb, case, args.pull_timeout, logger)
            else:
                logger.log("Bugreport skipped by --no-bugreport")

            if not args.no_hash:
                hash_count, hash_bytes = generate_hash_manifest(case, logger)

        else:
            print("\nPRIORITY MODE — discovering readable storage roots only; no bulk copy has started yet.\n")
            roots = discover_storage_roots(adb, case, logger)
            provider_status, scrcpy_proc, menu_hash_count, menu_hash_bytes = selective_menu(
                adb,
                db,
                case,
                roots,
                args.pull_timeout,
                logger,
                provider_status,
                scrcpy_proc,
            )
            if menu_hash_count is not None:
                hash_count, hash_bytes = menu_hash_count, menu_hash_bytes

            if args.bugreport:
                run_bugreport(adb, case, args.pull_timeout, logger)

            if not args.no_hash:
                do_hash = True
                if sys.stdin.isatty():
                    try:
                        answer = input("Generate/refresh final SHA-256 manifests before finishing? [Y/n]: ").strip().lower()
                        do_hash = answer not in {"n", "no"}
                    except KeyboardInterrupt:
                        print("\nFinal hashing skipped.")
                        do_hash = False
                if do_hash:
                    hash_count, hash_bytes = generate_hash_manifest(case, logger)
                else:
                    logger.log("Final hashing skipped at operator request")

        write_storage_path_map(db, case)
        failures = write_failures_report(db, case)
        completed = now_iso()
        write_summary(
            case,
            identity,
            db,
            provider_status,
            hash_count,
            hash_bytes,
            failures,
            str(acquisition_meta["started"]),
            completed,
        )
        counts = db.counts()
        pending = counts.get("storage_pending", 0) + counts.get("app_pending", 0)
        if mode == "auto":
            final_status = "complete_with_errors" if failures or pending else "complete"
        else:
            final_status = "selective_complete_with_open_items" if failures or pending else "selective_complete"
        acquisition_meta["status"] = final_status
        acquisition_meta["completed"] = completed
        acquisition_meta["counts"] = counts
        acquisition_meta["failure_count"] = failures
        acquisition_meta["pending_count"] = pending
        atomic_json(acquisition_path, acquisition_meta)

        print("\n" + "=" * 78)
        print("ANDROID RESCUE SESSION COMPLETE")
        print(f"Case:    {case}")
        print(f"Summary: {case / 'reports' / 'SUMMARY.txt'}")
        print(f"Copied storage files recorded complete: {counts.get('storage_done', 0):,}")
        print(f"Failed items: {failures:,}   Pending/resumable items: {pending:,}")
        print("=" * 78)
        logger.log(f"Finished. Summary: {case / 'reports' / 'SUMMARY.txt'}")
        if failures or pending:
            logger.log(
                "Open items remain; inspect reports/failures.csv and rerun with --resume after restoring connectivity/access",
                "WARN",
            )
        return 0 if not failures else 2

    except KeyboardInterrupt:
        completed = now_iso()
        write_storage_path_map(db, case)
        failures = write_failures_report(db, case)
        write_summary(
            case,
            identity,
            db,
            provider_status,
            hash_count,
            hash_bytes,
            failures,
            str(acquisition_meta["started"]),
            completed,
        )
        acquisition_meta["status"] = "interrupted"
        acquisition_meta["completed"] = completed
        acquisition_meta["counts"] = db.counts()
        atomic_json(acquisition_path, acquisition_meta)
        logger.log(
            f"Interrupted safely. Resume with: python3 {Path(__file__).name} --resume {shlex.quote(str(case))} --serial {shlex.quote(serial)}",
            "WARN",
        )
        print(f"\nInterrupted safely. Resume case: {case}")
        return 130
    except Exception as exc:
        completed = now_iso()
        logger.log(f"Fatal error: {exc}", "ERROR")
        try:
            write_storage_path_map(db, case)
            failures = write_failures_report(db, case)
            write_summary(
                case,
                identity,
                db,
                provider_status,
                hash_count,
                hash_bytes,
                failures,
                str(acquisition_meta["started"]),
                completed,
            )
            acquisition_meta["status"] = "failed"
            acquisition_meta["completed"] = completed
            acquisition_meta["fatal_error"] = str(exc)
            acquisition_meta["counts"] = db.counts()
            atomic_json(acquisition_path, acquisition_meta)
        except Exception:
            pass
        return 1
    finally:
        db.close()
        logger.close()
        _ = scrcpy_proc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
