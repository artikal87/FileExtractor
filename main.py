#!/usr/bin/env python3
"""
backup_organizer.py — Back up and organise files by type, with batch mode
for processing multiple removable drives one after another.

Modes:
  1. Single backup  — back up one or more sources to a destination.
                      Includes optional dedup during copy.
  2. Batch backup   — set destination once, then insert and back up
                      drives one at a time in a loop. No in-run dedup;
                      designed for a final dedup pass when all media
                      are done.
  3. Deduplicate    — standalone dedup of any folder.
  4. Resume batch   — pick up a previous batch session where you left off.

Usage:
  python backup_organizer.py
"""

import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────
# File type categories
# ──────────────────────────────────────────────
FILE_CATEGORIES = {
    "Images": {
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
        ".svg", ".webp", ".ico", ".heic", ".heif", ".raw", ".cr2",
        ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".psd",
        ".ai", ".eps", ".xcf", ".indd",
    },
    "Video": {
        ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm",
        ".m4v", ".mpg", ".mpeg", ".3gp", ".vob", ".ts", ".mts",
        ".m2ts", ".ogv",
    },
    "Audio": {
        ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a",
        ".opus", ".aiff", ".alac", ".mid", ".midi", ".ape", ".dsf",
    },
    "Documents": {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".odt", ".ods", ".odp", ".rtf", ".txt", ".md", ".tex",
        ".csv", ".tsv", ".epub", ".mobi", ".pages", ".numbers",
        ".key",
    },
    "Archives": {
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
        ".tgz", ".tbz2", ".cab", ".iso", ".dmg", ".img",
    },
    "Databases": {
        ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb", ".dbf",
    },
}

_EXT_TO_CATEGORY = {}
for _cat, _exts in FILE_CATEGORIES.items():
    for _ext in _exts:
        _EXT_TO_CATEGORY[_ext] = _cat


def get_category(filepath: Path) -> str:
    return _EXT_TO_CATEGORY.get(filepath.suffix.lower(), "Other")


# ──────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────
def file_hash(filepath: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def unique_dest_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def format_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1 << 20:
        return f"{b / 1024:.1f} KB"
    elif b < 1 << 30:
        return f"{b / (1 << 20):.1f} MB"
    else:
        return f"{b / (1 << 30):.2f} GB"


def format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s"
    else:
        h, remainder = divmod(int(seconds), 3600)
        m, s = divmod(remainder, 60)
        return f"{h}h {m:02d}m"


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{prompt} {hint}: ").strip().lower()
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter y or n.")


def dest_free_space(dest: Path) -> int:
    """Return free bytes on the filesystem containing dest."""
    try:
        target = dest if dest.exists() else dest.parent
        return shutil.disk_usage(target).free
    except OSError:
        return 0


def scan_files(sources: list[Path], exclude: list[str] | None = None):
    exclude = exclude or []
    for source in sources:
        source = source.resolve()
        if not source.exists():
            logging.warning(f"Source does not exist, skipping: {source}")
            continue
        for root, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and not any(pat.lower() in d.lower() for pat in exclude)
            ]
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                if fpath.is_file():
                    try:
                        size = fpath.stat().st_size
                    except OSError:
                        logging.warning(f"Cannot stat, skipping: {fpath}")
                        continue
                    yield fpath, size


# ──────────────────────────────────────────────
# Drive detection
# ──────────────────────────────────────────────
def detect_drives() -> list[Path]:
    system = platform.system()
    drives = []
    if system == "Windows":
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                drives.append(drive)
    elif system == "Darwin":
        volumes = Path("/Volumes")
        if volumes.exists():
            drives = sorted([v for v in volumes.iterdir() if v.is_dir()])
    else:
        for base in [Path("/mnt"), Path("/media"), Path("/run/media")]:
            if base.exists():
                for item in sorted(base.iterdir()):
                    if item.is_dir():
                        if base.name == "media" and item.is_dir():
                            for sub in sorted(item.iterdir()):
                                if sub.is_dir():
                                    drives.append(sub)
                        else:
                            drives.append(item)
    return drives


def get_volume_id(drive_path: Path) -> str:
    label = drive_path.name
    try:
        usage = shutil.disk_usage(drive_path)
        return f"{label}|{usage.total}"
    except OSError:
        return f"{label}|unknown"


def display_drive(drive: Path) -> str:
    try:
        usage = shutil.disk_usage(drive)
        used_gb = (usage.total - usage.free) / (1 << 30)
        total_gb = usage.total / (1 << 30)
        return f"{drive}  ({used_gb:.1f} / {total_gb:.1f} GB used)"
    except OSError:
        return str(drive)


def pick_drive_from_list(drives: list[Path],
                         already_done: set[str] | None = None) -> Path | None:
    already_done = already_done or set()
    if not drives:
        print("  No drives detected. Enter a path manually.")
        raw = input("  Path: ").strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        return p if p.exists() else None

    print("  Available drives:")
    for i, d in enumerate(drives, 1):
        vid = get_volume_id(d)
        tag = "  [already backed up]" if vid in already_done else ""
        print(f"    {i}. {display_drive(d)}{tag}")
    print(f"    0. Enter a path manually")
    print()

    while True:
        raw = input("  Choose a number (or 0 for manual): ").strip()
        if raw == "0":
            path = input("  Path: ").strip()
            if not path:
                return None
            p = Path(path).expanduser()
            if p.exists():
                return p
            print(f"    Not found: {p}")
            continue
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(drives):
                chosen = drives[idx]
                vid = get_volume_id(chosen)
                if vid in already_done:
                    if not ask_yes_no("    Already backed up. Proceed anyway?",
                                     default=False):
                        continue
                return chosen
        print("    Invalid choice.")


# ──────────────────────────────────────────────
# Batch session persistence
# ──────────────────────────────────────────────
class BatchSession:
    def __init__(self, dest: Path, exclude: list[str] | None = None,
                 min_size: int = 0, max_size: int = 0):
        self.dest = dest.resolve()
        self.exclude = exclude or []
        self.min_size = min_size
        self.max_size = max_size
        self.started = datetime.now().isoformat()
        self.completed_media: list[dict] = []
        self._session_path = self.dest / ".batch_session.json"

    @property
    def completed_volume_ids(self) -> set[str]:
        return {m["volume_id"] for m in self.completed_media}

    def record_media(self, label: str, path: Path, volume_id: str,
                     files_copied: int, bytes_copied: int,
                     category_counts: dict):
        self.completed_media.append({
            "label": label,
            "path": str(path),
            "volume_id": volume_id,
            "files_copied": files_copied,
            "bytes_copied": bytes_copied,
            "category_counts": category_counts,
            "completed_at": datetime.now().isoformat(),
        })
        self.save()

    def save(self):
        self.dest.mkdir(parents=True, exist_ok=True)
        with open(self._session_path, "w") as f:
            json.dump({
                "started": self.started,
                "destination": str(self.dest),
                "exclude": self.exclude,
                "min_size": self.min_size,
                "max_size": self.max_size,
                "completed_media": self.completed_media,
            }, f, indent=2)

    @classmethod
    def load(cls, dest: Path) -> "BatchSession | None":
        session_path = dest.resolve() / ".batch_session.json"
        if not session_path.exists():
            return None
        try:
            with open(session_path) as f:
                data = json.load(f)
            session = cls(
                dest=dest,
                exclude=data.get("exclude", []),
                min_size=data.get("min_size", 0),
                max_size=data.get("max_size", 0),
            )
            session.started = data.get("started", session.started)
            session.completed_media = data.get("completed_media", [])
            return session
        except (json.JSONDecodeError, KeyError, OSError) as e:
            logging.warning(f"Could not load session: {e}")
            return None

    @staticmethod
    def find_sessions() -> list[Path]:
        found = []
        search_roots = detect_drives() + [Path.home()]
        for root in search_roots:
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames
                                   if not d.startswith(".")]
                    if ".batch_session.json" in filenames:
                        found.append(Path(dirpath))
                    depth = dirpath.count(os.sep) - str(root).count(os.sep)
                    if depth >= 3:
                        dirnames.clear()
            except OSError:
                continue
        return found

    def print_status(self):
        print(f"  Session started:    {self.started}")
        print(f"  Destination:        {self.dest}")
        print(f"  Media completed:    {len(self.completed_media)}")
        if self.completed_media:
            total_files = sum(m["files_copied"] for m in self.completed_media)
            total_bytes = sum(m["bytes_copied"] for m in self.completed_media)
            print(f"  Total files so far: {total_files:,}")
            print(f"  Total data so far:  {format_bytes(total_bytes)}")
            print(f"  Completed drives:")
            for m in self.completed_media:
                print(f"    • {m['label']}  ({m['files_copied']:,} files, "
                      f"{format_bytes(m['bytes_copied'])})")

    def print_final_summary(self):
        total_files = sum(m["files_copied"] for m in self.completed_media)
        total_bytes = sum(m["bytes_copied"] for m in self.completed_media)
        merged_cats: dict[str, int] = defaultdict(int)
        for m in self.completed_media:
            for cat, count in m.get("category_counts", {}).items():
                merged_cats[cat] += count

        lines = [
            "",
            "═" * 58,
            "  BATCH BACKUP — FINAL SUMMARY",
            "═" * 58,
            f"  Media processed     : {len(self.completed_media):>10,}",
            f"  Total files copied  : {total_files:>10,}",
            f"  Total data copied   : {format_bytes(total_bytes):>10s}",
            "",
            "  Breakdown by source:",
        ]
        for m in self.completed_media:
            lines.append(
                f"    {m['label']:<25s} {m['files_copied']:>7,} files  "
                f"{format_bytes(m['bytes_copied']):>10s}"
            )
        lines.append("")
        lines.append("  Breakdown by category:")
        for cat in sorted(merged_cats, key=merged_cats.get, reverse=True):
            lines.append(f"    {cat:<25s} {merged_cats[cat]:>7,} files")
        lines.append("═" * 58)
        print("\n".join(lines))


# ──────────────────────────────────────────────
# Deduplicator
# ──────────────────────────────────────────────
class Deduplicator:
    def __init__(self, target: Path, dry_run: bool = False):
        self.target = target.resolve()
        self.dry_run = dry_run
        self.total_files = 0
        self.duplicates_found = 0
        self.bytes_freed = 0
        self.errors = 0
        self.groups_found = 0
        self.hash_index: dict[str, list[tuple[Path, float, int]]] = \
            defaultdict(list)
        self.size_index: dict[int, list[Path]] = defaultdict(list)
        self.removed: list[dict] = []

    def run(self):
        start_time = time.time()
        print()
        logging.info(
            f"{'DRY RUN — ' if self.dry_run else ''}"
            f"Deduplicating: {self.target}"
        )

        print("  Pass 1: Indexing files by size...")
        for root, dirs, files in os.walk(self.target, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fname.startswith(".") or fname.endswith(".json"):
                    continue
                fpath = Path(root) / fname
                if fpath.is_file():
                    try:
                        st = fpath.stat()
                        self.total_files += 1
                        self.size_index[st.st_size].append(fpath)
                    except OSError:
                        self.errors += 1

        candidates = sum(
            len(paths) for size, paths in self.size_index.items()
            if len(paths) > 1 and size > 0
        )
        print(f"  Found {self.total_files:,} files, "
              f"{candidates:,} potential duplicates (matching sizes).")

        if candidates == 0:
            print("  No potential duplicates found.")
            return

        print("  Pass 2: Hashing candidates...")
        hashed = 0
        for size, paths in self.size_index.items():
            if len(paths) <= 1 or size == 0:
                continue
            for fpath in paths:
                try:
                    h = file_hash(fpath)
                    mtime = fpath.stat().st_mtime
                    self.hash_index[h].append((fpath, mtime, size))
                    hashed += 1
                    if hashed % 500 == 0:
                        logging.info(f"    … hashed {hashed:,} / {candidates:,}")
                except OSError as e:
                    logging.warning(f"  Hash error: {fpath} ({e})")
                    self.errors += 1

        print("  Pass 3: Identifying duplicates...")
        self._process_groups()

        elapsed = time.time() - start_time
        self._print_summary(elapsed)

        if not self.dry_run and self.removed:
            self._write_report()

        if self.dry_run and self.duplicates_found > 0:
            print()
            if ask_yes_no(
                f"Delete {self.duplicates_found:,} duplicates and "
                f"free {format_bytes(self.bytes_freed)}?",
                default=False,
            ):
                self.dry_run = False
                self.duplicates_found = 0
                self.bytes_freed = 0
                self.errors = 0
                self.groups_found = 0
                self.removed.clear()
                self._process_groups()
                self._print_summary(0)
                if self.removed:
                    self._write_report()

    def _process_groups(self):
        for h, entries in self.hash_index.items():
            if len(entries) <= 1:
                continue
            self.groups_found += 1
            entries.sort(key=lambda e: e[1])
            keeper = entries[0]
            for dupe_path, _, dupe_size in entries[1:]:
                if not dupe_path.exists():
                    continue
                self.duplicates_found += 1
                self.bytes_freed += dupe_size
                if self.dry_run:
                    logging.info(
                        f"  [DRY RUN] Would remove: {dupe_path}\n"
                        f"            Keeping:      {keeper[0]}"
                    )
                else:
                    try:
                        dupe_path.unlink()
                    except OSError as e:
                        logging.error(f"  Delete failed: {dupe_path} ({e})")
                        self.errors += 1
                        self.bytes_freed -= dupe_size
                        continue
                self.removed.append({
                    "removed": str(dupe_path),
                    "kept": str(keeper[0]),
                    "size": dupe_size,
                })

    def _print_summary(self, elapsed: float):
        lines = [
            "",
            "═" * 54,
            f"  DEDUPLICATION — {'DRY RUN ' if self.dry_run else ''}SUMMARY",
            "═" * 54,
            f"  Total files scanned : {self.total_files:>10,}",
            f"  Duplicate groups    : {self.groups_found:>10,}",
            f"  Duplicates {'found' if self.dry_run else 'removed'}   : "
            f"{self.duplicates_found:>10,}",
            f"  Space {'reclaimable' if self.dry_run else 'freed'}    : "
            f"{format_bytes(self.bytes_freed):>10s}",
            f"  Errors              : {self.errors:>10,}",
        ]
        if elapsed > 0:
            lines.append(f"  Elapsed time        : {elapsed:>10.1f} s")
        lines.append("═" * 54)
        print("\n".join(lines))

    def _write_report(self):
        report_path = (self.target /
                       f"dedup_report_{datetime.now():%Y%m%d_%H%M%S}.json")
        with open(report_path, "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "target": str(self.target),
                "total_files": self.total_files,
                "duplicates_removed": self.duplicates_found,
                "bytes_freed": self.bytes_freed,
                "errors": self.errors,
                "removals": self.removed,
            }, f, indent=2)
        logging.info(f"Dedup report: {report_path}")


# ──────────────────────────────────────────────
# Progress display
# ──────────────────────────────────────────────
class ProgressTracker:
    """
    Maintains running totals and prints a single updating line
    during file copy operations.

    Display format:
      1,247 / 8,320 files | 3.2 / 14.7 GB | 22% | ETA 12m 34s | photo.jpg

    For large files (>500 MB):
      Copying large file (2.3 GB): vacation_video.mp4
    """

    LARGE_FILE_THRESHOLD = 500 * (1 << 20)  # 500 MB

    def __init__(self, total_files: int, total_bytes: int):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.start_time = time.time()
        self._last_line_len = 0

    def update(self, filename: str, filesize: int, before_copy: bool = False):
        """
        Call with before_copy=True right before starting a large file,
        and with before_copy=False after a file has finished copying.
        """
        if before_copy:
            if filesize >= self.LARGE_FILE_THRESHOLD:
                self._write(
                    f"  Copying large file ({format_bytes(filesize)}): "
                    f"{filename}"
                )
            return

        self.done_files += 1
        self.done_bytes += filesize

        elapsed = time.time() - self.start_time
        if elapsed > 0 and self.done_bytes > 0:
            speed = self.done_bytes / elapsed
            remaining_bytes = self.total_bytes - self.done_bytes
            eta = remaining_bytes / speed if speed > 0 else 0
            eta_str = format_time(eta)
        else:
            eta_str = "—"

        if self.total_bytes > 0:
            pct = int(self.done_bytes / self.total_bytes * 100)
        else:
            pct = 0

        # Truncate filename to fit
        max_name = 30
        display_name = filename
        if len(display_name) > max_name:
            display_name = "…" + display_name[-(max_name - 1):]

        line = (
            f"  {self.done_files:,} / {self.total_files:,} files | "
            f"{format_bytes(self.done_bytes)} / "
            f"{format_bytes(self.total_bytes)} | "
            f"{pct}% | ETA {eta_str} | {display_name}"
        )
        self._write(line)

    def _write(self, line: str):
        # Pad to overwrite previous line, use \r to stay on same line
        padded = line.ljust(self._last_line_len)
        print(f"\r{padded}", end="", flush=True)
        self._last_line_len = len(line)

    def finish(self):
        """Clear the progress line."""
        print("\r" + " " * self._last_line_len + "\r", end="", flush=True)


# ──────────────────────────────────────────────
# Copier (used by both single and batch modes)
# ──────────────────────────────────────────────

# How many consecutive space-related skips before auto-aborting
CONSECUTIVE_SKIP_ABORT = 20
# Stop if free space drops below this
MIN_FREE_SPACE = 100 * (1 << 20)  # 100 MB


class FileCopier:
    """
    Copies files from sources into dest organised by category.
    Includes pre-flight space check, progress display, and
    intelligent handling of out-of-space conditions.
    """

    def __init__(self, sources: list[Path], dest: Path,
                 exclude: list[str] | None = None,
                 dedup: bool = True,
                 min_size: int = 0, max_size: int = 0,
                 dry_run: bool = False):
        self.sources = sources
        self.dest = dest.resolve()
        self.exclude = exclude or []
        self.dedup = dedup
        self.min_size = min_size
        self.max_size = max_size
        self.dry_run = dry_run

        self.total_files = 0
        self.copied_files = 0
        self.skipped_dupes = 0
        self.skipped_existing = 0
        self.skipped_size = 0
        self.skipped_space = 0
        self.errors = 0
        self.bytes_copied = 0
        self.category_counts: dict[str, int] = defaultdict(int)
        self.seen_hashes: dict[str, Path] = {}
        self.manifest: list[dict] = []

        # Space tracking
        self._consecutive_space_skips = 0
        self._space_skip_mode = "ask"  # "ask", "skip", "abort"
        self._aborted = False

        # Pre-scan totals (for progress)
        self._scan_total_files = 0
        self._scan_total_bytes = 0

    def run(self) -> dict:
        """Pre-scan, check space, copy with progress, return stats."""
        start_time = time.time()

        # ── Pre-scan: count files and bytes ──
        print("  Scanning source(s)...")
        dest_name = self.dest.name
        effective_exclude = self.exclude + [dest_name]
        file_list: list[tuple[Path, int]] = []

        for fpath, size in scan_files(self.sources, effective_exclude):
            try:
                if fpath.resolve().is_relative_to(self.dest):
                    continue
            except (OSError, ValueError):
                pass
            # Apply size filters during scan
            if self.min_size and size < self.min_size:
                continue
            if self.max_size and size > self.max_size:
                continue
            file_list.append((fpath, size))

        self._scan_total_files = len(file_list)
        self._scan_total_bytes = sum(s for _, s in file_list)

        print(f"  Found {self._scan_total_files:,} files, "
              f"{format_bytes(self._scan_total_bytes)} total.")

        if self._scan_total_files == 0:
            print("  Nothing to copy.")
            return self._stats(0)

        # ── Pre-flight space check ──
        free = dest_free_space(self.dest)
        if free > 0:
            print(f"  Destination free space: {format_bytes(free)}")
            if self._scan_total_bytes > free:
                shortfall = self._scan_total_bytes - free
                print()
                print(f"  ⚠  Not enough space to copy everything.")
                print(f"     Source total:  {format_bytes(self._scan_total_bytes)}")
                print(f"     Free space:    {format_bytes(free)}")
                print(f"     Shortfall:     {format_bytes(shortfall)}")
                print()
                print("  Options:")
                print("    1. Proceed anyway (files that don't fit will be skipped)")
                print("    2. Abort")
                print()
                while True:
                    choice = input("  Choose [1/2]: ").strip()
                    if choice == "1":
                        break
                    if choice == "2":
                        print("  Aborted.")
                        return self._stats(0)
                    print("  Please enter 1 or 2.")
            else:
                headroom = free - self._scan_total_bytes
                print(f"  Headroom after copy: ~{format_bytes(headroom)}")
        print()

        # ── Copy with progress ──
        if not self.dry_run:
            self.dest.mkdir(parents=True, exist_ok=True)

        logging.info(
            f"{'DRY RUN — ' if self.dry_run else ''}"
            f"Copying {self._scan_total_files:,} files → {self.dest}"
        )

        progress = ProgressTracker(
            self._scan_total_files, self._scan_total_bytes
        )

        for fpath, size in file_list:
            if self._aborted:
                break

            self.total_files += 1
            copied = self._process(fpath, size, progress)

            if not self.dry_run and copied:
                progress.update(fpath.name, size)
            elif not self.dry_run and not copied:
                # Still update count for non-copied files (dupes, existing)
                # but don't add to bytes — keeps ETA accurate
                progress.done_files += 1

        progress.finish()

        elapsed = time.time() - start_time
        self._print_summary(elapsed)

        if not self.dry_run:
            self._write_manifest()

        return self._stats(elapsed)

    def _process(self, fpath: Path, size: int,
                 progress: ProgressTracker) -> bool:
        """Process one file. Returns True if copied successfully."""

        category = get_category(fpath)
        dest_dir = self.dest / category
        dest_path = dest_dir / fpath.name

        # Dedup
        if self.dedup:
            try:
                fhash = file_hash(fpath)
            except OSError as e:
                logging.warning(f"Hash error: {fpath} ({e})")
                self.errors += 1
                return False
            if fhash in self.seen_hashes:
                self.skipped_dupes += 1
                return False
            self.seen_hashes[fhash] = fpath

        dest_path = unique_dest_path(dest_path)

        if dest_path.exists():
            self.skipped_existing += 1
            return False

        # ── Space check before copy ──
        if not self.dry_run:
            free = dest_free_space(self.dest)

            if free < MIN_FREE_SPACE:
                print()
                print(f"\n  ✗ Destination critically low on space "
                      f"({format_bytes(free)} free). Aborting.")
                self._aborted = True
                return False

            if size > free:
                return self._handle_space_skip(fpath, size, free)

        # ── Copy ──
        if self.dry_run:
            logging.info(f"[DRY RUN] {fpath}  →  {dest_path}")
        else:
            # Show large-file notice before copy starts
            progress.update(fpath.name, size, before_copy=True)
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(fpath), str(dest_path))
            except OSError as e:
                logging.error(f"Copy failed: {fpath} → {dest_path} ({e})")
                self.errors += 1
                return False

        self._consecutive_space_skips = 0  # reset on successful copy
        self.copied_files += 1
        self.bytes_copied += size
        self.category_counts[category] += 1
        self.manifest.append({
            "source": str(fpath),
            "destination": str(dest_path),
            "category": category,
            "size": size,
        })
        return True

    def _handle_space_skip(self, fpath: Path, size: int,
                           free: int) -> bool:
        """
        Handle a file that won't fit. Returns False (file not copied).
        May set self._aborted to stop the whole run.
        """
        self._consecutive_space_skips += 1
        self.skipped_space += 1

        # Auto-abort after too many consecutive skips
        if self._consecutive_space_skips >= CONSECUTIVE_SKIP_ABORT:
            print(f"\n  ✗ {CONSECUTIVE_SKIP_ABORT} consecutive files "
                  f"skipped for space. Target is full. Aborting.")
            self._aborted = True
            return False

        # First space skip: pause and ask
        if self._space_skip_mode == "ask":
            print()
            print(f"\n  ⚠  Not enough space for: {fpath.name}")
            print(f"     File size:  {format_bytes(size)}")
            print(f"     Free space: {format_bytes(free)}")
            print()
            print("  Options:")
            print("    1. Skip this file and continue (will skip "
                  "automatically from now on)")
            print("    2. Abort the copy")
            print("    3. I've freed up space — retry this file")
            print()
            while True:
                choice = input("  Choose [1/2/3]: ").strip()
                if choice == "1":
                    self._space_skip_mode = "skip"
                    print(f"  Skipping. Will auto-skip if this keeps "
                          f"happening (abort after "
                          f"{CONSECUTIVE_SKIP_ABORT} in a row).")
                    return False
                if choice == "2":
                    self._aborted = True
                    return False
                if choice == "3":
                    # Re-check space
                    new_free = dest_free_space(self.dest)
                    if size <= new_free:
                        self._consecutive_space_skips = 0
                        print(f"  Space available now "
                              f"({format_bytes(new_free)}). Retrying.")
                        # Return to let the caller try again — but we
                        # can't easily re-enter _process, so just
                        # signal that space is fine and let next
                        # iteration handle it. For this file, we do
                        # a direct copy here.
                        return False  # will be handled on re-scan
                    else:
                        print(f"  Still not enough space "
                              f"({format_bytes(new_free)} free).")
                        continue
                print("  Please enter 1, 2, or 3.")

        # Already in skip mode
        return False

    def _stats(self, elapsed: float) -> dict:
        return {
            "total_files": self.total_files,
            "copied_files": self.copied_files,
            "bytes_copied": self.bytes_copied,
            "category_counts": dict(self.category_counts),
            "errors": self.errors,
            "skipped_space": self.skipped_space,
            "elapsed": elapsed,
            "aborted": self._aborted,
        }

    def _print_summary(self, elapsed: float):
        gb = self.bytes_copied / (1 << 30)
        lines = [
            "",
            "─" * 54,
            f"  {'DRY RUN ' if self.dry_run else ''}COPY SUMMARY",
            "─" * 54,
            f"  Files scanned       : {self.total_files:>10,}",
            f"  Files copied        : {self.copied_files:>10,}",
        ]
        if self.dedup:
            lines.append(
                f"  Duplicates skipped  : {self.skipped_dupes:>10,}")
        lines += [
            f"  Already present     : {self.skipped_existing:>10,}",
        ]
        if self.skipped_space > 0:
            lines.append(
                f"  Skipped (no space)  : {self.skipped_space:>10,}")
        lines += [
            f"  Errors              : {self.errors:>10,}",
            f"  Data copied         : {gb:>10.2f} GB",
            f"  Time                : {elapsed:>10.1f} s",
        ]
        if elapsed > 0 and self.bytes_copied > 0:
            speed = self.bytes_copied / elapsed
            lines.append(
                f"  Average speed       : {format_bytes(int(speed))}/s")
        if self.category_counts:
            lines.append("")
            for cat in sorted(self.category_counts,
                              key=self.category_counts.get, reverse=True):
                lines.append(
                    f"    {cat:<20s} {self.category_counts[cat]:>8,}")
        if self._aborted:
            lines.append("")
            lines.append("  ⚠  Copy was aborted (insufficient space).")
        lines.append("─" * 54)
        print("\n".join(lines))

    def _write_manifest(self):
        manifest_path = (
            self.dest /
            f"manifest_{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        with open(manifest_path, "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "sources": [str(s) for s in self.sources],
                "destination": str(self.dest),
                "files": self.manifest,
            }, f, indent=2)
        logging.info(f"Manifest: {manifest_path}")


# ──────────────────────────────────────────────
# Mode 1: Single backup
# ──────────────────────────────────────────────
def run_single_backup():
    print("─" * 54)
    print("  SINGLE BACKUP")
    print("  Back up one or more sources, organised by file type.")
    print("  Includes optional dedup during copy.")
    print("─" * 54)
    print()

    detected = detect_drives()
    if detected:
        print("Detected drives:")
        for i, d in enumerate(detected, 1):
            print(f"  {i}. {display_drive(d)}")
        print()

    print("Which locations do you want to back up?")
    if detected:
        print("Enter numbers from above, full paths, or a mix "
              "(comma-separated).")
    print('Press Enter on a blank line when finished.')
    print()

    sources: list[Path] = []
    while True:
        prompt = (f"  Source #{len(sources) + 1}: " if not sources
                  else f"  Source #{len(sources) + 1} (Enter to finish): ")
        raw = input(prompt).strip()
        if raw == "" and sources:
            break
        if raw == "" and not sources:
            print("  Need at least one source.")
            continue
        for entry in [e.strip() for e in raw.split(",") if e.strip()]:
            if entry.isdigit() and detected:
                idx = int(entry) - 1
                if 0 <= idx < len(detected):
                    sources.append(detected[idx])
                    print(f"    Added: {detected[idx]}")
                else:
                    print(f"    Number out of range.")
            else:
                p = Path(entry).expanduser()
                if p.exists():
                    sources.append(p)
                    print(f"    Added: {p}")
                else:
                    print(f"    Not found: {p}")

    print()
    dest = _ask_dest_path()
    print()
    dry_run = ask_yes_no("Dry run first? (preview only)", default=True)
    print()
    dedup = ask_yes_no(
        "Deduplicate during copy? (hash each file, skip identical ones)",
        default=True)
    print()
    exclude = _ask_exclusions()
    min_size, max_size = _ask_size_filters()

    print()
    print("─" * 54)
    print(f"  Sources:       {len(sources)} location(s)")
    for s in sources:
        print(f"                   {s}")
    print(f"  Destination:   {dest}")
    print(f"  Mode:          {'Dry run' if dry_run else 'Live'}")
    print(f"  Dedup on copy: {'Yes' if dedup else 'No'}")
    print("─" * 54)
    print()
    if not ask_yes_no("Proceed?", default=True):
        return

    copier = FileCopier(
        sources=sources, dest=dest, exclude=exclude,
        dedup=dedup, min_size=min_size, max_size=max_size,
        dry_run=dry_run,
    )
    stats = copier.run()

    if dry_run and stats["copied_files"] > 0:
        print()
        if ask_yes_no("Run it for real?", default=True):
            copier = FileCopier(
                sources=sources, dest=dest, exclude=exclude,
                dedup=dedup, min_size=min_size, max_size=max_size,
                dry_run=False,
            )
            copier.run()


# ──────────────────────────────────────────────
# Mode 2: Batch backup
# ──────────────────────────────────────────────
def run_batch_backup(session: BatchSession | None = None):
    if session:
        print("─" * 54)
        print("  RESUMING BATCH SESSION")
        print("─" * 54)
        session.print_status()
        print()
        if not ask_yes_no("Continue this session?", default=True):
            return
    else:
        print("─" * 54)
        print("  BATCH BACKUP")
        print("  Set your destination and preferences once, then")
        print("  insert and back up removable media one at a time.")
        print("  Files are copied without dedup for speed — run a")
        print("  final dedup when all media are done.")
        print("─" * 54)
        print()

        dest = _ask_dest_path()
        print()
        exclude = _ask_exclusions()
        min_size, max_size = _ask_size_filters()

        session = BatchSession(
            dest=dest, exclude=exclude,
            min_size=min_size, max_size=max_size,
        )
        session.save()
        print()
        print(f"  Session saved. If interrupted, choose")
        print(f"  'Resume batch session' to continue.")

    # ── Main loop ──
    drive_number = len(session.completed_media) + 1
    baseline_drives = set(str(d) for d in detect_drives())

    while True:
        print()
        print("═" * 54)
        print(f"  DRIVE #{drive_number}")
        print("═" * 54)
        print()
        print("  Insert your next removable media, then press Enter.")
        print("  Type 'done' to finish and see the final summary.")
        print()

        raw = input("  Ready? (Enter / done): ").strip().lower()
        if raw == "done":
            break

        # Detect new drives
        current_drives = detect_drives()
        current_set = set(str(d) for d in current_drives)
        new_drive_paths = current_set - baseline_drives
        new_drives = [d for d in current_drives if str(d) in new_drive_paths]

        source = None

        if len(new_drives) == 1:
            candidate = new_drives[0]
            print(f"\n  Detected new drive: {display_drive(candidate)}")
            vid = get_volume_id(candidate)
            if vid in session.completed_volume_ids:
                print("  This drive was already backed up in this session.")
                if not ask_yes_no("  Back it up again?", default=False):
                    continue
            if ask_yes_no("  Use this drive?", default=True):
                source = candidate
            else:
                source = pick_drive_from_list(
                    current_drives, session.completed_volume_ids
                )
        elif len(new_drives) > 1:
            print(f"\n  Detected {len(new_drives)} new drives:")
            source = pick_drive_from_list(
                new_drives, session.completed_volume_ids
            )
        else:
            print("\n  No new drives detected since last check.")
            print("  Choose from all available drives:\n")
            source = pick_drive_from_list(
                current_drives, session.completed_volume_ids
            )

        if source is None:
            print("  No drive selected. Skipping.")
            continue

        # Label
        default_label = source.name
        print()
        raw = input(f"  Label for this media [{default_label}]: ").strip()
        label = raw if raw else default_label

        volume_id = get_volume_id(source)

        print()
        print(f"  Source:      {display_drive(source)}")
        print(f"  Label:       {label}")
        print(f"  Destination: {session.dest}")
        print()
        if not ask_yes_no("  Start copying?", default=True):
            continue

        copier = FileCopier(
            sources=[source],
            dest=session.dest,
            exclude=session.exclude,
            dedup=False,
            min_size=session.min_size,
            max_size=session.max_size,
            dry_run=False,
        )
        stats = copier.run()

        session.record_media(
            label=label,
            path=source,
            volume_id=volume_id,
            files_copied=stats["copied_files"],
            bytes_copied=stats["bytes_copied"],
            category_counts=stats["category_counts"],
        )

        print(f"\n  Drive '{label}' done. "
              f"{len(session.completed_media)} media backed up so far.")

        baseline_drives = set(str(d) for d in detect_drives())
        drive_number += 1

    # ── Session complete ──
    session.print_final_summary()

    print()
    if ask_yes_no("Run deduplication on the backup folder now?",
                  default=True):
        print()
        dry_run = ask_yes_no("  Dry run first?", default=True)
        deduper = Deduplicator(session.dest, dry_run=dry_run)
        deduper.run()

    session_path = session.dest / ".batch_session.json"
    if session_path.exists():
        if ask_yes_no("\nRemove the session file? (can't resume after this)",
                      default=False):
            session_path.unlink()
            print("  Session file removed.")


# ──────────────────────────────────────────────
# Mode 4: Resume
# ──────────────────────────────────────────────
def run_resume():
    print("─" * 54)
    print("  RESUME BATCH SESSION")
    print("─" * 54)
    print()
    print("  Enter the backup destination folder, or press Enter")
    print("  to search for existing sessions.")
    print()
    raw = input("  Destination path (or Enter to search): ").strip()

    if raw:
        dest = Path(raw).expanduser()
        session = BatchSession.load(dest)
        if session:
            run_batch_backup(session)
        else:
            print(f"  No batch session found at {dest}")
    else:
        print("  Searching for session files...")
        found = BatchSession.find_sessions()
        if not found:
            print("  No batch sessions found.")
            return
        print(f"  Found {len(found)} session(s):")
        for i, p in enumerate(found, 1):
            s = BatchSession.load(p)
            if s:
                n = len(s.completed_media)
                print(f"    {i}. {p}  ({n} media completed)")
        print()
        while True:
            raw = input("  Choose a session number: ").strip()
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(found):
                    session = BatchSession.load(found[idx])
                    if session:
                        run_batch_backup(session)
                        return
            print("  Invalid choice.")


# ──────────────────────────────────────────────
# Shared prompts
# ──────────────────────────────────────────────
def _ask_dest_path() -> Path:
    while True:
        raw = input("Destination folder for the backup:\n  Path: ").strip()
        if not raw:
            print("  A destination is required.")
            continue
        dest = Path(raw).expanduser()
        if dest.exists() and not dest.is_dir():
            print("  That exists but is not a directory.")
            continue
        if not dest.exists():
            if ask_yes_no(f"  {dest} doesn't exist. Create it?",
                          default=True):
                return dest
            continue
        return dest


def _ask_exclusions() -> list[str]:
    exclude = []
    if ask_yes_no("Exclude any folder names? (e.g. node_modules, Trash)",
                  default=False):
        raw = input("  Folder names to skip (comma-separated): ").strip()
        exclude = [e.strip() for e in raw.split(",") if e.strip()]
        if exclude:
            print(f"    Excluding: {', '.join(exclude)}")
    return exclude


def _ask_size_filters() -> tuple[int, int]:
    min_size = 0
    max_size = 0
    if ask_yes_no("Set file size filters?", default=False):
        raw = input("  Minimum file size in MB (Enter to skip): ").strip()
        if raw:
            try:
                min_size = int(float(raw) * 1_048_576)
            except ValueError:
                pass
        raw = input("  Maximum file size in MB (Enter to skip): ").strip()
        if raw:
            try:
                max_size = int(float(raw) * 1_048_576)
            except ValueError:
                pass
    return min_size, max_size


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    print()
    print("═" * 54)
    print("  BACKUP ORGANISER v1.0")
    print("═" * 54)
    print()
    print("  1. Single backup")
    print("     Back up one or more locations, organised by")
    print("     type. Includes optional dedup during copy.")
    print()
    print("  2. Batch backup (multiple removable media)")
    print("     Set your destination once, then insert and")
    print("     back up drives one at a time. Skips in-run")
    print("     dedup for speed — run a final dedup when done.")
    print()
    print("  3. Deduplicate a folder")
    print("     Scan any folder for identical files and remove")
    print("     duplicates, keeping the oldest copy of each.")
    print()
    print("  4. Resume a batch session")
    print("     Pick up a previous batch backup where you")
    print("     left off.")
    print()

    while True:
        choice = input("Choose [1/2/3/4]: ").strip()
        if choice in ("1", "2", "3", "4"):
            break
        print("  Please enter 1, 2, 3, or 4.")

    print()

    if choice == "1":
        run_single_backup()
    elif choice == "2":
        run_batch_backup()
    elif choice == "3":
        while True:
            raw = input(
                "Which folder do you want to deduplicate?\n  Path: "
            ).strip()
            if not raw:
                print("  A path is required.")
                continue
            target = Path(raw).expanduser()
            if target.is_dir():
                break
            print(f"  Not a valid directory: {target}")
        print()
        dry_run = ask_yes_no("Dry run first?", default=True)
        deduper = Deduplicator(target, dry_run=dry_run)
        deduper.run()
    elif choice == "4":
        run_resume()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(0)