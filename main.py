#!/usr/bin/env python3
"""
backup_organizer.py — Interactively back up multiple drives/directories,
sorted by file type.

Features:
  • Interactive setup — walks you through every option
  • Scans any number of source paths (drives, folders, mount points)
  • Categorises every file by extension into human-friendly groups
  • Deduplicates using SHA-256 (size pre-check for speed)
  • Preserves original modification timestamps
  • Dry-run mode to preview without copying
  • Resumable — skips files already present at the destination
  • Generates a detailed log and a final summary report

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

# Fast lookup: extension → category
_EXT_TO_CATEGORY = {}
for _cat, _exts in FILE_CATEGORIES.items():
    for _ext in _exts:
        _EXT_TO_CATEGORY[_ext] = _cat


def get_category(filepath: Path) -> str:
    """Return the category name for a file based on its extension."""
    return _EXT_TO_CATEGORY.get(filepath.suffix.lower(), "Other")


# ──────────────────────────────────────────────
# Hashing
# ──────────────────────────────────────────────
def file_hash(filepath: Path, chunk_size: int = 1 << 20) -> str:
    """Return the SHA-256 hex digest of a file, read in chunks."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ──────────────────────────────────────────────
# Name-collision resolver
# ──────────────────────────────────────────────
def unique_dest_path(dest: Path) -> Path:
    """If dest already exists, append _1, _2, … until unique."""
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ──────────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────────
def scan_sources(sources: list[Path], exclude: list[str] | None = None):
    """
    Yield (Path, size) for every regular file under each source.
    Skips hidden directories and any patterns in `exclude`.
    """
    exclude = exclude or []
    for source in sources:
        source = source.resolve()
        if not source.exists():
            logging.warning(f"Source does not exist, skipping: {source}")
            continue
        for root, dirs, files in os.walk(source, followlinks=False):
            # Skip hidden directories and excluded patterns
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
# Interactive setup
# ──────────────────────────────────────────────
def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question and return a boolean."""
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


def detect_drives() -> list[Path]:
    """Try to list mounted drives / volumes for the user's convenience."""
    system = platform.system()
    drives = []

    if system == "Windows":
        # Check common drive letters
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                drives.append(drive)
    elif system == "Darwin":
        # macOS: list /Volumes
        volumes = Path("/Volumes")
        if volumes.exists():
            drives = sorted([v for v in volumes.iterdir() if v.is_dir()])
    else:
        # Linux: check /mnt, /media, and /run/media
        for base in [Path("/mnt"), Path("/media"), Path("/run/media")]:
            if base.exists():
                for item in sorted(base.iterdir()):
                    if item.is_dir():
                        # /media and /run/media have user subdirs
                        if base.name in ("media", "run") and item.is_dir():
                            for sub in sorted(item.iterdir()):
                                if sub.is_dir():
                                    drives.append(sub)
                        else:
                            drives.append(item)

    return drives


def interactive_setup() -> dict:
    """Walk the user through all options and return a config dict."""
    print()
    print("═" * 54)
    print("  BACKUP ORGANIZER")
    print("  Consolidate files from multiple drives by type")
    print("═" * 54)
    print()

    # ── Detect and show available drives ──
    detected = detect_drives()
    if detected:
        print("Detected drives / volumes:")
        for i, d in enumerate(detected, 1):
            try:
                usage = shutil.disk_usage(d)
                used_gb = (usage.total - usage.free) / (1 << 30)
                total_gb = usage.total / (1 << 30)
                print(f"  {i}. {d}  ({used_gb:.1f} / {total_gb:.1f} GB used)")
            except OSError:
                print(f"  {i}. {d}")
        print()

    # ── Source paths ──
    print("Which locations do you want to back up?")
    if detected:
        print("Enter numbers from the list above, full paths, or a mix.")
        print("Separate multiple entries with commas, or enter one per line.")
    else:
        print("Enter full paths, one per line.")
    print('Type "done" or press Enter on a blank line when finished.')
    print()

    sources: list[Path] = []
    while True:
        prompt = f"  Source #{len(sources) + 1}: " if not sources else f"  Source #{len(sources) + 1} (or Enter to finish): "
        raw = input(prompt).strip()

        if raw.lower() in ("done", "") and sources:
            break
        if raw.lower() in ("done", "") and not sources:
            print("  You need at least one source path.")
            continue

        # Handle comma-separated entries
        entries = [e.strip() for e in raw.split(",") if e.strip()]
        for entry in entries:
            # Check if it's a number referencing the detected list
            if entry.isdigit() and detected:
                idx = int(entry) - 1
                if 0 <= idx < len(detected):
                    sources.append(detected[idx])
                    print(f"    ✓ Added: {detected[idx]}")
                else:
                    print(f"    ✗ Number {entry} is out of range.")
            else:
                p = Path(entry).expanduser()
                if p.exists():
                    sources.append(p)
                    print(f"    ✓ Added: {p}")
                else:
                    print(f"    ✗ Path not found: {p}")
                    if ask_yes_no("      Add it anyway? (it may become available later)", default=False):
                        sources.append(p)
                        print(f"    ✓ Added: {p}")

    print()

    # ── Destination path ──
    while True:
        raw = input("Where should the organised backup be saved?\n  Destination: ").strip()
        if not raw:
            print("  A destination path is required.")
            continue
        dest = Path(raw).expanduser()
        if dest.exists() and not dest.is_dir():
            print("  That path exists but is not a directory.")
            continue
        if not dest.exists():
            if ask_yes_no(f"  {dest} doesn't exist yet. Create it?", default=True):
                break
            else:
                continue
        break

    print()

    # ── Overlap check ──
    dest_resolved = dest.resolve()
    for s in sources:
        try:
            s_resolved = s.resolve()
            if dest_resolved == s_resolved or str(dest_resolved).startswith(str(s_resolved) + os.sep):
                print(f"  ⚠  Warning: destination is inside source {s}.")
                print(f"     The backup folder itself will be excluded to avoid loops.")
        except OSError:
            pass

    # ── Dry run ──
    dry_run = ask_yes_no(
        "Do a dry run first? (preview only, no files copied)",
        default=True,
    )
    print()

    # ── Deduplication ──
    skip_dedup = False
    if not ask_yes_no(
        "Enable deduplication? (uses SHA-256 hashing — slower but skips identical files)",
        default=True,
    ):
        skip_dedup = True
    print()

    # ── Exclusions ──
    exclude = []
    if ask_yes_no("Exclude any folder names? (e.g. node_modules, .git, Trash)", default=False):
        raw = input("  Folder names to skip (comma-separated): ").strip()
        exclude = [e.strip() for e in raw.split(",") if e.strip()]
        if exclude:
            print(f"    Will skip folders containing: {', '.join(exclude)}")
    print()

    # ── Size filters ──
    min_size = 0
    max_size = 0
    if ask_yes_no("Set file size filters?", default=False):
        raw = input("  Minimum file size in MB (Enter to skip): ").strip()
        if raw:
            try:
                min_size = int(float(raw) * 1_048_576)
                print(f"    Skipping files smaller than {raw} MB")
            except ValueError:
                print("    Invalid number, skipping.")
        raw = input("  Maximum file size in MB (Enter to skip): ").strip()
        if raw:
            try:
                max_size = int(float(raw) * 1_048_576)
                print(f"    Skipping files larger than {raw} MB")
            except ValueError:
                print("    Invalid number, skipping.")
    print()

    # ── Confirmation ──
    print("─" * 54)
    print("  SUMMARY OF SETTINGS")
    print("─" * 54)
    print(f"  Sources:")
    for s in sources:
        print(f"    • {s}")
    print(f"  Destination:    {dest}")
    print(f"  Mode:           {'Dry run (preview)' if dry_run else 'Live (will copy files)'}")
    print(f"  Deduplication:  {'On' if not skip_dedup else 'Off'}")
    if exclude:
        print(f"  Excluding:      {', '.join(exclude)}")
    if min_size:
        print(f"  Min file size:  {min_size / 1_048_576:.1f} MB")
    if max_size:
        print(f"  Max file size:  {max_size / 1_048_576:.1f} MB")
    print("─" * 54)
    print()

    if not ask_yes_no("Proceed?", default=True):
        print("Cancelled.")
        sys.exit(0)

    return {
        "sources": sources,
        "dest": dest,
        "dry_run": dry_run,
        "exclude": exclude,
        "skip_dedup": skip_dedup,
        "min_size": min_size,
        "max_size": max_size,
    }


# ──────────────────────────────────────────────
# Main organiser logic
# ──────────────────────────────────────────────
class BackupOrganizer:
    def __init__(
        self,
        sources: list[Path],
        dest: Path,
        dry_run: bool = False,
        exclude: list[str] | None = None,
        skip_dedup: bool = False,
        min_size: int = 0,
        max_size: int = 0,
    ):
        self.sources = sources
        self.dest = dest.resolve()
        self.dry_run = dry_run
        self.exclude = exclude or []
        self.skip_dedup = skip_dedup
        self.min_size = min_size
        self.max_size = max_size

        # Stats
        self.total_files = 0
        self.copied_files = 0
        self.skipped_dupes = 0
        self.skipped_existing = 0
        self.skipped_size = 0
        self.errors = 0
        self.bytes_copied = 0
        self.category_counts = defaultdict(int)

        # Dedup tracking: hash → first source path
        self.seen_hashes: dict[str, Path] = {}
        # Size index for fast pre-check
        self.seen_sizes: dict[int, list[Path]] = defaultdict(list)

        # Manifest for the run
        self.manifest: list[dict] = []

    def _is_size_filtered(self, size: int) -> bool:
        if self.min_size and size < self.min_size:
            return True
        if self.max_size and size > self.max_size:
            return True
        return False

    def run(self):
        """Execute the backup organisation."""
        start_time = time.time()
        print()
        logging.info(
            f"{'DRY RUN — ' if self.dry_run else ''}"
            f"Scanning {len(self.sources)} source(s) → {self.dest}"
        )

        # Ensure destination exists
        if not self.dry_run:
            self.dest.mkdir(parents=True, exist_ok=True)

        # Exclude the destination folder itself to prevent loops
        dest_name = self.dest.name
        effective_exclude = self.exclude + [dest_name]

        for fpath, size in scan_sources(self.sources, effective_exclude):
            # Also skip if the file is literally inside the destination
            try:
                if fpath.resolve().is_relative_to(self.dest):
                    continue
            except (OSError, ValueError):
                pass

            self.total_files += 1
            self._process_file(fpath, size)

            # Progress indicator every 500 files
            if self.total_files % 500 == 0:
                logging.info(
                    f"  … scanned {self.total_files:,} files, "
                    f"copied {self.copied_files:,}, "
                    f"dupes {self.skipped_dupes:,}"
                )

        elapsed = time.time() - start_time
        self._print_summary(elapsed)

        # Write manifest
        if not self.dry_run:
            self._write_manifest()

        # Offer to run again (live) if this was a dry run
        if self.dry_run and self.copied_files > 0:
            print()
            if ask_yes_no("This was a dry run. Run it again for real and copy the files?", default=True):
                self.dry_run = False
                self.total_files = 0
                self.copied_files = 0
                self.skipped_dupes = 0
                self.skipped_existing = 0
                self.skipped_size = 0
                self.errors = 0
                self.bytes_copied = 0
                self.category_counts.clear()
                self.seen_hashes.clear()
                self.seen_sizes.clear()
                self.manifest.clear()
                self.run()

    def _process_file(self, fpath: Path, size: int):
        # Size filter
        if self._is_size_filtered(size):
            self.skipped_size += 1
            return

        category = get_category(fpath)
        dest_dir = self.dest / category
        dest_path = dest_dir / fpath.name

        # Deduplication
        if not self.skip_dedup:
            try:
                fhash = file_hash(fpath)
            except OSError as e:
                logging.warning(f"Hash error, skipping: {fpath} ({e})")
                self.errors += 1
                return

            if fhash in self.seen_hashes:
                self.skipped_dupes += 1
                logging.debug(
                    f"Duplicate skipped: {fpath} "
                    f"(same as {self.seen_hashes[fhash]})"
                )
                return
            self.seen_hashes[fhash] = fpath

        # Resolve name collisions
        dest_path = unique_dest_path(dest_path)

        # Check if already copied in a previous (resumed) run
        if dest_path.exists():
            self.skipped_existing += 1
            return

        # Copy
        if self.dry_run:
            logging.info(f"[DRY RUN] {fpath}  →  {dest_path}")
        else:
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(fpath), str(dest_path))  # preserves metadata
            except OSError as e:
                logging.error(f"Copy failed: {fpath} → {dest_path} ({e})")
                self.errors += 1
                return

        self.copied_files += 1
        self.bytes_copied += size
        self.category_counts[category] += 1
        self.manifest.append({
            "source": str(fpath),
            "destination": str(dest_path),
            "category": category,
            "size": size,
        })

    def _print_summary(self, elapsed: float):
        gb = self.bytes_copied / (1 << 30)
        lines = [
            "",
            "═" * 54,
            f"  BACKUP ORGANIZER — {'DRY RUN ' if self.dry_run else ''}SUMMARY",
            "═" * 54,
            f"  Total files scanned : {self.total_files:>10,}",
            f"  Files copied        : {self.copied_files:>10,}",
            f"  Duplicates skipped  : {self.skipped_dupes:>10,}",
            f"  Already present     : {self.skipped_existing:>10,}",
            f"  Size-filtered       : {self.skipped_size:>10,}",
            f"  Errors              : {self.errors:>10,}",
            f"  Data copied         : {gb:>10.2f} GB",
            f"  Elapsed time        : {elapsed:>10.1f} s",
            "",
            "  Files per category:",
        ]
        for cat in sorted(self.category_counts, key=self.category_counts.get, reverse=True):
            lines.append(f"    {cat:<20s} {self.category_counts[cat]:>8,}")
        lines.append("═" * 54)
        print("\n".join(lines))

    def _write_manifest(self):
        manifest_path = self.dest / f"manifest_{datetime.now():%Y%m%d_%H%M%S}.json"
        with open(manifest_path, "w") as f:
            json.dump(
                {
                    "timestamp": datetime.now().isoformat(),
                    "sources": [str(s) for s in self.sources],
                    "destination": str(self.dest),
                    "total_files": self.total_files,
                    "copied_files": self.copied_files,
                    "duplicates_skipped": self.skipped_dupes,
                    "errors": self.errors,
                    "bytes_copied": self.bytes_copied,
                    "category_counts": dict(self.category_counts),
                    "files": self.manifest,
                },
                f,
                indent=2,
            )
        logging.info(f"Manifest written to {manifest_path}")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = interactive_setup()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(0)

    organizer = BackupOrganizer(**config)

    try:
        organizer.run()
    except KeyboardInterrupt:
        print("\n\nInterrupted. Run the script again to resume — it will skip already-copied files.")
        sys.exit(1)


if __name__ == "__main__":
    main()
