#!/usr/bin/env python3
"""
backup_organiser.py — Back up and organise files by type, with batch mode
for processing multiple removable drives one after another.

Modes:
  1. Single backup  — back up one or more sources to a destination.
                      Includes optional dedup during copy.
  2. Batch backup   — set destination once, then insert and back up
                      drives one at a time in a loop. No in-run dedup;
                      designed for a final dedup pass when all media
                      are done.
  3. Deduplicate    — standalone dedup of any folder.
  4. Organise photos — sort images by date/album, optionally split
                       real photos from icons/screenshots.
  5. Resume batch   — pick up a previous batch session where you left off.

Usage:
  python backup_organiser.py
"""

import hashlib
import json
import logging
import os
import platform
import shutil
import struct
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Optional: Pillow for EXIF and image dimensions
try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _install_pillow():
    """Attempt to pip-install Pillow and reload it."""
    global HAS_PIL, PILImage
    import subprocess
    print("  Installing Pillow...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "Pillow", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        from PIL import Image as PILImage  # noqa: F811
        HAS_PIL = True
        print("  Pillow installed successfully.")
    except Exception as e:
        print(f"  Could not install Pillow: {e}")
        print("  Continuing without it. You can install it manually:")
        print(f"    {sys.executable} -m pip install Pillow")

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

IMAGE_EXTENSIONS = FILE_CATEGORIES["Images"]

# Photo detection constants
RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raw",
}
LIKELY_PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif",
}
SCREENSHOT_PATTERNS = [
    "screenshot", "screen shot", "screen recording",
    "snip", "capture", "grab", "screenclip",
]
SCREEN_RESOLUTIONS = {
    (1920, 1080), (2560, 1440), (3840, 2160),  # 16:9
    (1440, 900), (2880, 1800), (1680, 1050),    # Mac
    (1366, 768), (1536, 864),                    # common laptop
    (2560, 1600), (3024, 1964), (3456, 2234),    # Retina Mac
    (1170, 2532), (1284, 2778), (1290, 2796),    # iPhone
    (1179, 2556), (1242, 2688),                  # iPhone
    (1080, 1920), (1440, 2560), (1080, 2400),    # Android
}
MIN_PHOTO_DIMENSION = 600

# Album detection thresholds
ALBUM_MIN_IMAGES = 5
ALBUM_DENSITY_THRESHOLD = 0.6  # 60% of files must be images


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
        for root, dirs, files in os.walk(source, followlinks=False,
                                         onerror=lambda e: logging.warning(
                                             f"Cannot access, skipping: {e}")):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and not any(pat.lower() in d.lower() for pat in exclude)
            ]
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                try:
                    if not fpath.is_file():
                        continue
                    size = fpath.stat().st_size
                except OSError:
                    logging.warning(f"Cannot access, skipping: {fpath}")
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
                 exclude_ext: list[str] | None = None,
                 include_categories: list[str] | None = None,
                 min_size: int = 0, max_size: int = 0):
        self.dest = dest.resolve()
        self.exclude = exclude or []
        self.exclude_ext = exclude_ext or []
        self.include_categories = include_categories or list(_ALL_CATEGORIES)
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
                "exclude_ext": self.exclude_ext,
                "include_categories": self.include_categories,
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
                exclude_ext=data.get("exclude_ext", []),
                include_categories=data.get("include_categories"),
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
                        logging.info(
                            f"    … hashed {hashed:,} / {candidates:,}")
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
                        logging.error(
                            f"  Delete failed: {dupe_path} ({e})")
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
# Photo organiser
# ──────────────────────────────────────────────
class PhotoOrganiser:
    """
    Organise image files by date and/or album. Optionally split
    real photos from icons, screenshots, and other non-photo images.
    """

    def __init__(self, target: Path, strategy: str = "date",
                 filter_photos: bool = False, dry_run: bool = False):
        """
        target:         folder containing images (e.g. backup/Images)
        strategy:       "date", "album", or "album_date"
        filter_photos:  if True, split into photo/ and _other/
        dry_run:        preview moves without doing them
        """
        self.target = target.resolve()
        self.strategy = strategy
        self.filter_photos = filter_photos
        self.dry_run = dry_run

        # Stats
        self.total_images = 0
        self.moved_photo = 0
        self.moved_other = 0
        self.skipped = 0
        self.errors = 0
        self.albums_detected: list[dict] = []

        # Source path mapping from manifests (dest filename → source path)
        self._source_map: dict[str, str] = {}

        # PIL notice printed once
        self._pil_warned = False

    def run(self):
        start_time = time.time()
        print()
        logging.info(
            f"{'DRY RUN — ' if self.dry_run else ''}"
            f"Organising photos in: {self.target}"
        )

        if not HAS_PIL:
            print()
            print("  Pillow is not installed. Without it, photo")
            print("  detection uses filename patterns and file")
            print("  extensions only. With it, you get EXIF dates,")
            print("  camera detection, and dimension checks.")
            print()
            if ask_yes_no("  Install Pillow now?", default=True):
                _install_pillow()
            print()

        # Load any manifests for album detection
        self._load_manifests()

        # Collect all image files
        images: list[Path] = []
        for root, dirs, files in os.walk(self.target, followlinks=False,
                                         onerror=lambda e: None):
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("photo", "_other")]
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix.lower() in IMAGE_EXTENSIONS:
                    images.append(fpath)

        self.total_images = len(images)
        print(f"  Found {self.total_images:,} image files.")

        if self.total_images == 0:
            print("  Nothing to organise.")
            return

        # Album detection (if strategy uses albums)
        album_map: dict[str, str] = {}  # filename → album name
        if self.strategy in ("album", "album_date"):
            album_map = self._detect_albums(images)
            if self.albums_detected:
                self._preview_albums()

        # Process each image
        for fpath in images:
            self._process_image(fpath, album_map)

        elapsed = time.time() - start_time
        self._print_summary(elapsed)

        # Offer to go live after dry run
        if self.dry_run and (self.moved_photo + self.moved_other) > 0:
            print()
            if ask_yes_no("Apply these changes?", default=True):
                self.dry_run = False
                self.moved_photo = 0
                self.moved_other = 0
                self.skipped = 0
                self.errors = 0
                for fpath in images:
                    # Re-check — file may not exist if already moved
                    if fpath.exists():
                        self._process_image(fpath, album_map)
                self._print_summary(0)

    def _load_manifests(self):
        """Load manifest files to map destination paths to source paths.
        Searches both the target directory and its parent (since manifests
        are written to the backup root, not the Images subfolder)."""
        manifest_dirs = [self.target]
        if self.target.parent != self.target:
            manifest_dirs.append(self.target.parent)

        loaded = 0
        for search_dir in manifest_dirs:
            if not search_dir.is_dir():
                continue
            for item in search_dir.iterdir():
                if (item.name.startswith("manifest_")
                        and item.suffix == ".json"):
                    try:
                        with open(item) as f:
                            data = json.load(f)
                        for entry in data.get("files", []):
                            dest_str = entry.get("destination", "")
                            source_str = entry.get("source", "")
                            if not dest_str or not source_str:
                                continue
                            dest_path = Path(dest_str)
                            # Skip files that no longer exist (removed by dedup)
                            if not dest_path.exists():
                                continue
                            # Key by full resolved path to avoid collisions
                            self._source_map[str(dest_path.resolve())] = source_str
                            loaded += 1
                    except (json.JSONDecodeError, OSError):
                        continue
        if self._source_map:
            print(f"  Loaded source paths for {len(self._source_map):,} "
                  f"files from manifests ({loaded:,} entries).")

    def _detect_albums(self, images: list[Path]) -> dict[str, str]:
        """
        Detect album folders using manifest source paths (preferred)
        or current folder structure. Returns resolved_dest_path → album name.
        """
        album_map: dict[str, str] = {}

        # Set of resolved paths that actually exist on disk
        live_images = {str(p.resolve()) for p in images}

        if self._source_map:
            # Group by source parent directory
            # dir_files: source_dir → list of resolved dest paths
            dir_files: dict[str, list[str]] = defaultdict(list)
            dir_total: dict[str, int] = defaultdict(int)
            dir_images: dict[str, int] = defaultdict(int)

            for dest_resolved, source_path in self._source_map.items():
                parent = str(Path(source_path).parent)
                dir_total[parent] += 1
                if Path(source_path).suffix.lower() in IMAGE_EXTENSIONS:
                    # Only count files still on disk
                    if dest_resolved in live_images:
                        dir_images[parent] += 1
                        dir_files[parent].append(dest_resolved)

            for parent, img_count in dir_images.items():
                total = dir_total[parent]
                density = img_count / total if total > 0 else 0
                if (img_count >= ALBUM_MIN_IMAGES
                        and density >= ALBUM_DENSITY_THRESHOLD):
                    album_name = Path(parent).name
                    album_name = album_name.strip().replace(" ", "_")
                    if album_name:
                        self.albums_detected.append({
                            "name": album_name,
                            "image_count": img_count,
                            "density": density,
                            "source_dir": parent,
                        })
                        for dest_resolved in dir_files[parent]:
                            album_map[dest_resolved] = album_name

        else:
            # No manifests — use current folder structure
            for root, dirs, files in os.walk(
                    self.target, followlinks=False):
                dirs[:] = [d for d in dirs if not d.startswith(".")
                           and d not in ("photo", "_other")]
                if root == str(self.target):
                    continue  # skip root level
                all_files = files
                img_files = [f for f in files
                             if Path(f).suffix.lower() in IMAGE_EXTENSIONS]
                total = len(all_files)
                img_count = len(img_files)
                density = img_count / total if total > 0 else 0

                if (img_count >= ALBUM_MIN_IMAGES
                        and density >= ALBUM_DENSITY_THRESHOLD):
                    album_name = Path(root).name.strip().replace(" ", "_")
                    if album_name:
                        self.albums_detected.append({
                            "name": album_name,
                            "image_count": img_count,
                            "density": density,
                            "source_dir": root,
                        })
                        for fname in img_files:
                            fpath = Path(root) / fname
                            resolved = str(fpath.resolve())
                            album_map[resolved] = album_name

        return album_map

    def _preview_albums(self):
        """Show detected albums for user review."""
        print()
        print(f"  Detected {len(self.albums_detected)} album(s):")
        for a in sorted(self.albums_detected,
                        key=lambda x: x["image_count"], reverse=True):
            print(f"    {a['name']:<30s} {a['image_count']:>5} images  "
                  f"({a['density']:.0%} density)")
        print()

    def _is_photo(self, fpath: Path) -> bool:
        """Determine if an image is a real photo vs icon/screenshot."""
        ext = fpath.suffix.lower()

        # RAW formats are always photos
        if ext in RAW_EXTENSIONS:
            return True

        fname_lower = fpath.stem.lower()

        # Screenshot filename patterns
        if any(pat in fname_lower for pat in SCREENSHOT_PATTERNS):
            return False

        if HAS_PIL:
            try:
                with PILImage.open(fpath) as img:
                    w, h = img.size

                    # Exact screen resolution match → screenshot
                    if (w, h) in SCREEN_RESOLUTIONS or \
                       (h, w) in SCREEN_RESOLUTIONS:
                        return False

                    # Too small on both axes → icon/thumbnail
                    if (w < MIN_PHOTO_DIMENSION
                            and h < MIN_PHOTO_DIMENSION):
                        return False

                    # EXIF camera make/model → definitely a photo
                    exif = None
                    try:
                        exif = img._getexif()
                    except Exception:
                        pass
                    if exif:
                        if (271 in exif or 272 in exif):  # Make or Model
                            return True

                    # Large enough, no screenshot markers → probably photo
                    return True
            except Exception:
                pass

        # Without PIL: trust extension for likely photo formats
        if ext in LIKELY_PHOTO_EXTENSIONS:
            return True

        # PNG, GIF, BMP, SVG, ICO etc. without PIL → assume not a photo
        return False

    def _get_photo_date(self, fpath: Path) -> tuple[str, str] | None:
        """Get (year, month) from EXIF or file mtime."""
        if HAS_PIL:
            try:
                with PILImage.open(fpath) as img:
                    exif = None
                    try:
                        exif = img._getexif()
                    except Exception:
                        pass
                    if exif and 36867 in exif:  # DateTimeOriginal
                        date_str = str(exif[36867])
                        parts = date_str.split(":")
                        if len(parts) >= 2:
                            year = parts[0].strip()
                            month = parts[1].strip()
                            if year.isdigit() and month.isdigit():
                                return (year, f"{int(month):02d}")
            except Exception:
                pass

        # Fallback: file modification time
        try:
            mtime = fpath.stat().st_mtime
            dt = datetime.fromtimestamp(mtime)
            return (str(dt.year), f"{dt.month:02d}")
        except OSError:
            return None

    def _process_image(self, fpath: Path, album_map: dict[str, str]):
        """Move a single image to its new location."""
        is_photo = True
        if self.filter_photos:
            is_photo = self._is_photo(fpath)

        # Determine destination subfolder
        if self.filter_photos:
            base = self.target / ("photo" if is_photo else "_other")
        else:
            base = self.target

        # Choose subfolder based on strategy (only for photos or
        # unfiltered mode)
        if is_photo or not self.filter_photos:
            album_name = album_map.get(str(fpath.resolve()))

            if self.strategy == "album" and album_name:
                dest_dir = base / album_name
            elif self.strategy == "album_date":
                if album_name:
                    dest_dir = base / album_name
                else:
                    date = self._get_photo_date(fpath)
                    if date:
                        dest_dir = base / date[0] / f"{date[0]}-{date[1]}"
                    else:
                        dest_dir = base / "_undated"
            elif self.strategy == "date":
                date = self._get_photo_date(fpath)
                if date:
                    dest_dir = base / date[0] / f"{date[0]}-{date[1]}"
                else:
                    dest_dir = base / "_undated"
            else:
                dest_dir = base
        else:
            # Non-photos go flat into _other
            dest_dir = base

        dest_path = dest_dir / fpath.name

        # Skip if already in the right place
        if dest_path == fpath:
            self.skipped += 1
            return

        dest_path = unique_dest_path(dest_path)

        if self.dry_run:
            label = "photo" if is_photo else "other"
            logging.info(f"  [{label}] {fpath.name}  →  {dest_path}")
        else:
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(fpath), str(dest_path))
            except OSError as e:
                logging.error(f"  Move failed: {fpath} ({e})")
                self.errors += 1
                return

        if is_photo:
            self.moved_photo += 1
        else:
            self.moved_other += 1

    def _print_summary(self, elapsed: float):
        lines = [
            "",
            "═" * 54,
            f"  PHOTO ORGANISER — "
            f"{'DRY RUN ' if self.dry_run else ''}SUMMARY",
            "═" * 54,
            f"  Total images found  : {self.total_images:>10,}",
            f"  Photos moved        : {self.moved_photo:>10,}",
        ]
        if self.filter_photos:
            lines.append(
                f"  Non-photos moved    : {self.moved_other:>10,}")
        lines += [
            f"  Already in place    : {self.skipped:>10,}",
            f"  Albums detected     : {len(self.albums_detected):>10,}",
            f"  Errors              : {self.errors:>10,}",
        ]
        if elapsed > 0:
            lines.append(f"  Elapsed time        : {elapsed:>10.1f} s")
        lines.append("═" * 54)
        print("\n".join(lines))


# ──────────────────────────────────────────────
# Progress display
# ──────────────────────────────────────────────
class ProgressTracker:
    """
    Maintains running totals and prints a single updating line
    during file copy operations.
    """

    LARGE_FILE_THRESHOLD = 500 * (1 << 20)  # 500 MB

    def __init__(self, total_files: int, total_bytes: int):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.start_time = time.time()
        self._last_line_len = 0

    def update(self, filename: str, filesize: int,
               before_copy: bool = False):
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
        padded = line.ljust(self._last_line_len)
        print(f"\r{padded}", end="", flush=True)
        self._last_line_len = len(line)

    def finish(self):
        print("\r" + " " * self._last_line_len + "\r", end="",
              flush=True)


# ──────────────────────────────────────────────
# Copier (used by both single and batch modes)
# ──────────────────────────────────────────────
CONSECUTIVE_SKIP_ABORT = 20
MIN_FREE_SPACE = 100 * (1 << 20)  # 100 MB


class FileCopier:
    def __init__(self, sources: list[Path], dest: Path,
                 exclude: list[str] | None = None,
                 exclude_ext: list[str] | None = None,
                 include_categories: list[str] | None = None,
                 dedup: bool = True,
                 min_size: int = 0, max_size: int = 0,
                 dry_run: bool = False,
                 label: str = ""):
        self.sources = sources
        self.dest = dest.resolve()
        self.exclude = exclude or []
        self.exclude_ext = set(
            (e if e.startswith(".") else f".{e}").lower()
            for e in (exclude_ext or [])
        )
        self.include_categories = set(
            include_categories) if include_categories else None
        self.dedup = dedup
        self.min_size = min_size
        self.max_size = max_size
        self.dry_run = dry_run
        self.label = label

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

        self._consecutive_space_skips = 0
        self._space_skip_mode = "ask"
        self._aborted = False

        self._scan_total_files = 0
        self._scan_total_bytes = 0

    def run(self) -> dict:
        start_time = time.time()

        # ── Pre-scan ──
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
            if self.min_size and size < self.min_size:
                continue
            if self.max_size and size > self.max_size:
                continue
            cat = get_category(fpath)
            if self.include_categories and cat not in self.include_categories:
                continue
            if cat == "Other":
                continue
            if self.exclude_ext and fpath.suffix.lower() in self.exclude_ext:
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
                print(f"     Source total:  "
                      f"{format_bytes(self._scan_total_bytes)}")
                print(f"     Free space:    {format_bytes(free)}")
                print(f"     Shortfall:     {format_bytes(shortfall)}")
                print()
                print("  Options:")
                print("    1. Proceed anyway (files that don't fit "
                      "will be skipped)")
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
                progress.done_files += 1

        progress.finish()

        elapsed = time.time() - start_time
        self._print_summary(elapsed)

        if not self.dry_run:
            self._write_manifest()

        return self._stats(elapsed)

    def _process(self, fpath: Path, size: int,
                 progress: ProgressTracker) -> bool:
        category = get_category(fpath)
        dest_dir = self.dest / category
        dest_path = dest_dir / fpath.name

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

        # Space check
        if not self.dry_run:
            free = dest_free_space(self.dest)
            if free < MIN_FREE_SPACE:
                print(f"\n  ✗ Critically low on space "
                      f"({format_bytes(free)} free). Aborting.")
                self._aborted = True
                return False
            if size > free:
                return self._handle_space_skip(fpath, size, free)

        if self.dry_run:
            logging.info(f"[DRY RUN] {fpath}  →  {dest_path}")
        else:
            progress.update(fpath.name, size, before_copy=True)
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(fpath), str(dest_path))
            except OSError as e:
                logging.error(
                    f"Copy failed: {fpath} → {dest_path} ({e})")
                self.errors += 1
                return False

        self._consecutive_space_skips = 0
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
        self._consecutive_space_skips += 1
        self.skipped_space += 1

        if self._consecutive_space_skips >= CONSECUTIVE_SKIP_ABORT:
            print(f"\n  ✗ {CONSECUTIVE_SKIP_ABORT} consecutive files "
                  f"skipped for space. Aborting.")
            self._aborted = True
            return False

        if self._space_skip_mode == "ask":
            print()
            print(f"\n  ⚠  Not enough space for: {fpath.name}")
            print(f"     File size:  {format_bytes(size)}")
            print(f"     Free space: {format_bytes(free)}")
            print()
            print("  Options:")
            print("    1. Skip this file and continue")
            print("    2. Abort the copy")
            print("    3. I've freed up space — retry")
            print()
            while True:
                choice = input("  Choose [1/2/3]: ").strip()
                if choice == "1":
                    self._space_skip_mode = "skip"
                    return False
                if choice == "2":
                    self._aborted = True
                    return False
                if choice == "3":
                    new_free = dest_free_space(self.dest)
                    if size <= new_free:
                        self._consecutive_space_skips = 0
                        print(f"  Space available ({format_bytes(new_free)}). "
                              f"Retrying.")
                        return False
                    else:
                        print(f"  Still not enough "
                              f"({format_bytes(new_free)} free).")
                        continue
                print("  Please enter 1, 2, or 3.")

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
                f"  Average speed       : "
                f"{format_bytes(int(speed))}/s")
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
            data = {
                "timestamp": datetime.now().isoformat(),
                "sources": [str(s) for s in self.sources],
                "destination": str(self.dest),
                "files": self.manifest,
            }
            if self.label:
                data["label"] = self.label
            json.dump(data, f, indent=2)
        logging.info(f"Manifest: {manifest_path}")


# ──────────────────────────────────────────────
# Photo organisation setup prompts
# ──────────────────────────────────────────────
def _ask_photo_org_settings() -> dict:
    """Ask the user how they want photos organised. Returns config dict."""
    print("  How should photos be organised?")
    print()
    print("    1. By date (EXIF date taken, file date fallback)")
    print("    2. By album (auto-detected from source folders)")
    print("    3. By album with date fallback (album if detected,")
    print("       otherwise by date)")
    print()
    while True:
        choice = input("  Choose [1/2/3]: ").strip()
        if choice == "1":
            strategy = "date"
            break
        if choice == "2":
            strategy = "album"
            break
        if choice == "3":
            strategy = "album_date"
            break
        print("  Please enter 1, 2, or 3.")

    print()
    filter_photos = ask_yes_no(
        "  Filter real photos from icons/screenshots?\n"
        "  (Photos go to photo/, everything else to _other/)",
        default=True,
    )

    return {"strategy": strategy, "filter_photos": filter_photos}


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
    categories = _ask_categories()
    print()
    dry_run = ask_yes_no("Dry run first? (preview only)", default=True)
    print()
    dedup = ask_yes_no(
        "Deduplicate during copy? (hash each file, skip identical ones)",
        default=True)
    print()
    exclude = _ask_exclusions()
    exclude_ext = _ask_ext_exclusions()
    min_size, max_size = _ask_size_filters()

    # Photo organisation
    print()
    organise_photos = ask_yes_no(
        "Organise photos after copy? (sort by date/album)", default=False)
    photo_settings = None
    if organise_photos:
        print()
        photo_settings = _ask_photo_org_settings()

    print()
    print("─" * 54)
    print(f"  Sources:       {len(sources)} location(s)")
    for s in sources:
        print(f"                   {s}")
    print(f"  Destination:   {dest}")
    all_cats = len(categories) == len(_ALL_CATEGORIES)
    print(f"  Categories:    {'All' if all_cats else ', '.join(categories)}")
    print(f"  Mode:          {'Dry run' if dry_run else 'Live'}")
    print(f"  Dedup on copy: {'Yes' if dedup else 'No'}")
    if exclude:
        print(f"  Excl. folders: {', '.join(exclude)}")
    if exclude_ext:
        print(f"  Excl. types:   {', '.join(exclude_ext)}")
    if min_size:
        print(f"  Min file size: {format_bytes(min_size)}")
    if max_size:
        print(f"  Max file size: {format_bytes(max_size)}")
    if organise_photos and photo_settings:
        strat_labels = {"date": "By date", "album": "By album",
                        "album_date": "Album + date fallback"}
        print(f"  Photo org:     "
              f"{strat_labels[photo_settings['strategy']]}")
        print(f"  Photo filter:  "
              f"{'Yes (photo/ and _other/)' if photo_settings['filter_photos'] else 'No'}")
    print("─" * 54)
    print()
    if not ask_yes_no("Proceed?", default=True):
        return

    copier = FileCopier(
        sources=sources, dest=dest, exclude=exclude,
        exclude_ext=exclude_ext, include_categories=categories,
        dedup=dedup, min_size=min_size, max_size=max_size,
        dry_run=dry_run,
    )
    stats = copier.run()

    if dry_run and stats["copied_files"] > 0:
        print()
        if ask_yes_no("Run it for real?", default=True):
            copier = FileCopier(
                sources=sources, dest=dest, exclude=exclude,
                exclude_ext=exclude_ext, include_categories=categories,
                dedup=dedup, min_size=min_size, max_size=max_size,
                dry_run=False,
            )
            stats = copier.run()

    # Post-copy photo organisation
    if organise_photos and photo_settings and not dry_run:
        images_dir = dest / "Images"
        if images_dir.is_dir():
            print()
            print("─" * 54)
            print("  Starting photo organisation...")
            print("─" * 54)
            org = PhotoOrganiser(
                target=images_dir,
                strategy=photo_settings["strategy"],
                filter_photos=photo_settings["filter_photos"],
                dry_run=True,  # always preview first
            )
            org.run()


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
        categories = _ask_categories()
        print()
        exclude = _ask_exclusions()
        exclude_ext = _ask_ext_exclusions()
        min_size, max_size = _ask_size_filters()

        print()
        batch_dry_run = ask_yes_no(
            "Dry run the first drive? (preview before committing)",
            default=True)

        session = BatchSession(
            dest=dest, exclude=exclude, exclude_ext=exclude_ext,
            include_categories=categories,
            min_size=min_size, max_size=max_size,
        )
        session.save()
        print()
        print(f"  Session saved. If interrupted, choose")
        print(f"  'Resume batch session' to continue.")

    # ── Main loop ──
    drive_number = len(session.completed_media) + 1
    baseline_drives = set(str(d) for d in detect_drives())
    try:
        batch_dry_run
    except NameError:
        batch_dry_run = False

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
        new_drives = [d for d in current_drives
                      if str(d) in new_drive_paths]

        source = None

        if len(new_drives) == 1:
            candidate = new_drives[0]
            print(f"\n  Detected new drive: {display_drive(candidate)}")
            vid = get_volume_id(candidate)
            if vid in session.completed_volume_ids:
                print("  This drive was already backed up in this "
                      "session.")
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
        raw = input(
            f"  Label for this media [{default_label}]: ").strip()
        label = raw if raw else default_label

        volume_id = get_volume_id(source)

        print()
        print(f"  Source:      {display_drive(source)}")
        print(f"  Label:       {label}")
        print(f"  Destination: {session.dest}")
        all_cats = len(session.include_categories) == len(_ALL_CATEGORIES)
        print(f"  Categories:  {'All' if all_cats else ', '.join(session.include_categories)}")
        if session.exclude:
            print(f"  Excl. folders: {', '.join(session.exclude)}")
        if session.exclude_ext:
            print(f"  Excl. types: {', '.join(session.exclude_ext)}")
        if session.min_size:
            print(f"  Min size:    {format_bytes(session.min_size)}")
        if session.max_size:
            print(f"  Max size:    {format_bytes(session.max_size)}")
        print()
        if not ask_yes_no("  Start copying?", default=True):
            continue

        copier = FileCopier(
            sources=[source],
            dest=session.dest,
            exclude=session.exclude,
            exclude_ext=session.exclude_ext,
            include_categories=session.include_categories,
            dedup=False,
            min_size=session.min_size,
            max_size=session.max_size,
            dry_run=batch_dry_run,
            label=label,
        )
        stats = copier.run()

        # If this was a dry run, offer to go live
        if batch_dry_run:
            print()
            if ask_yes_no("  Go live and copy these files?",
                          default=True):
                copier = FileCopier(
                    sources=[source],
                    dest=session.dest,
                    exclude=session.exclude,
                    exclude_ext=session.exclude_ext,
                    include_categories=session.include_categories,
                    dedup=False,
                    min_size=session.min_size,
                    max_size=session.max_size,
                    dry_run=False,
                    label=label,
                )
                stats = copier.run()
            else:
                print("  Skipped. Moving to next drive.")
                batch_dry_run = False
                continue
            batch_dry_run = False

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

    # Dedup first, then photo org
    print()
    if ask_yes_no("Run deduplication on the backup folder now?",
                  default=True):
        print()
        dry_run = ask_yes_no("  Dry run first?", default=True)
        deduper = Deduplicator(session.dest, dry_run=dry_run)
        deduper.run()

    images_dir = session.dest / "Images"
    if images_dir.is_dir():
        print()
        if ask_yes_no("Organise photos? (sort by date/album)",
                      default=False):
            print()
            photo_settings = _ask_photo_org_settings()
            org = PhotoOrganiser(
                target=images_dir,
                strategy=photo_settings["strategy"],
                filter_photos=photo_settings["filter_photos"],
                dry_run=True,
            )
            org.run()

    session_path = session.dest / ".batch_session.json"
    if session_path.exists():
        if ask_yes_no(
                "\nRemove the session file? (can't resume after this)",
                default=False):
            session_path.unlink()
            print("  Session file removed.")


# ──────────────────────────────────────────────
# Mode 4: Organise photos (standalone)
# ──────────────────────────────────────────────
def run_photo_organise():
    print("─" * 54)
    print("  ORGANISE PHOTOS")
    print("  Sort images by date or album. Optionally split")
    print("  real photos from icons and screenshots.")
    print("─" * 54)
    print()

    while True:
        raw = input(
            "Which folder contains the images?\n  Path: ").strip()
        if not raw:
            print("  A path is required.")
            continue
        target = Path(raw).expanduser()
        if target.is_dir():
            break
        print(f"  Not a valid directory: {target}")

    print()
    photo_settings = _ask_photo_org_settings()

    print()
    print("─" * 54)
    print(f"  Target:        {target}")
    strat_labels = {"date": "By date", "album": "By album",
                    "album_date": "Album + date fallback"}
    print(f"  Strategy:      {strat_labels[photo_settings['strategy']]}")
    print(f"  Photo filter:  "
          f"{'Yes (photo/ and _other/)' if photo_settings['filter_photos'] else 'No'}")
    print("─" * 54)
    print()

    org = PhotoOrganiser(
        target=target,
        strategy=photo_settings["strategy"],
        filter_photos=photo_settings["filter_photos"],
        dry_run=True,  # always preview first
    )
    org.run()


# ──────────────────────────────────────────────
# Mode 5: Resume
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
        raw = input(
            "Destination folder for the backup:\n  Path: ").strip()
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
    if ask_yes_no(
            "Exclude any folder names? (e.g. node_modules, Trash)",
            default=False):
        raw = input(
            "  Folder names to skip (comma-separated): ").strip()
        exclude = [e.strip() for e in raw.split(",") if e.strip()]
        if exclude:
            print(f"    Excluding: {', '.join(exclude)}")
    return exclude


def _ask_size_filters() -> tuple[int, int]:
    min_size = 0
    max_size = 0
    if ask_yes_no("Set file size filters?", default=False):
        raw = input(
            "  Minimum file size in MB (Enter to skip): ").strip()
        if raw:
            try:
                min_size = int(float(raw) * 1_048_576)
            except ValueError:
                pass
        raw = input(
            "  Maximum file size in MB (Enter to skip): ").strip()
        if raw:
            try:
                max_size = int(float(raw) * 1_048_576)
            except ValueError:
                pass
    return min_size, max_size


def _ask_ext_exclusions() -> list[str]:
    exclude_ext: list[str] = []
    if ask_yes_no("Exclude any file types? (e.g. .gz, .mkv)",
                  default=False):
        raw = input(
            "  Extensions to skip (comma-separated): ").strip()
        exclude_ext = [e.strip() for e in raw.split(",") if e.strip()]
        if exclude_ext:
            # Normalise: ensure leading dot
            exclude_ext = [
                e if e.startswith(".") else f".{e}"
                for e in exclude_ext
            ]
            print(f"    Excluding: {', '.join(exclude_ext)}")
    return exclude_ext


# All selectable category names in display order
_ALL_CATEGORIES = list(FILE_CATEGORIES.keys())


def _ask_categories() -> list[str]:
    """Let the user choose which file type categories to extract."""
    print("  Which file types do you want to extract?")
    print("  Enter numbers separated by commas, or Enter for all.")
    print()
    for i, cat in enumerate(_ALL_CATEGORIES, 1):
        print(f"    {i}. {cat}")
    print()
    raw = input("  Categories [all]: ").strip()
    if not raw:
        print(f"    Selected: all")
        return list(_ALL_CATEGORIES)
    chosen = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(_ALL_CATEGORIES):
                chosen.append(_ALL_CATEGORIES[idx])
    if not chosen:
        print("    No valid selection. Using all categories.")
        return list(_ALL_CATEGORIES)
    print(f"    Selected: {', '.join(chosen)}")
    return chosen


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
    print("  BACKUP ORGANISER v1.1")
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
    print("  4. Organise photos")
    print("     Sort images by date or album. Optionally split")
    print("     real photos from icons and screenshots.")
    print()
    print("  5. Resume a batch session")
    print("     Pick up a previous batch backup where you")
    print("     left off.")
    print()

    while True:
        choice = input("Choose [1/2/3/4/5]: ").strip()
        if choice in ("1", "2", "3", "4", "5"):
            break
        print("  Please enter 1, 2, 3, 4, or 5.")

    print()

    if choice == "1":
        run_single_backup()
    elif choice == "2":
        run_batch_backup()
    elif choice == "3":
        while True:
            raw = input(
                "Which folder do you want to deduplicate?\n"
                "  Path: "
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
        run_photo_organise()
    elif choice == "5":
        run_resume()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(0)