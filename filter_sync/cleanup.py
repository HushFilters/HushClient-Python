"""Discover superseded date ranges and persist their retention policy."""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import re
import stat
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger("hushclient.sync.cleanup")


class _FileScan:
    """One metadata snapshot. Directory checks and file stats are shared per scan.

    Never reuse this snapshot for deletion: unlink still uses fresh path checks.
    """
    def __init__(self, root: Path):
        self.root = root
        self.directories = {"": True}
        self.listings = {}
        self.files = {}

    def directory(self, name: str) -> bool:
        if name not in self.directories:
            parent = name.rpartition("/")[0]
            safe = False
            if self.directory(parent):
                try:
                    info = (self.root / name).lstat()
                    safe = stat.S_ISDIR(info.st_mode) and not (
                        getattr(info, "st_file_attributes", 0) & 0x400)  # Windows reparse point
                except FileNotFoundError:
                    pass
            self.directories[name] = safe
        return self.directories[name]

    def entries(self, directory: str):
        if directory not in self.listings:
            entries = {}
            if self.directory(directory):
                try:
                    with os.scandir(self.root / directory) as listing:
                        entries = {os.path.normcase(entry.name): entry for entry in listing}
                except FileNotFoundError:
                    pass
            self.listings[directory] = entries
        return self.listings[directory]

    def has_file(self, name: str) -> bool:
        directory, _, filename = name.rpartition("/")
        entry = self.entries(directory).get(filename)
        return entry is not None and entry.is_file(follow_symlinks=False)

    def identity(self, name: str):
        if name not in self.files:
            directory, _, filename = name.rpartition("/")
            entry = self.entries(directory).get(filename)
            identity = None
            if entry is not None:
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and not (
                            getattr(info, "st_file_attributes", 0) & 0x400):
                        identity = [info.st_size, info.st_mtime_ns]
                except FileNotFoundError:
                    pass
            self.files[name] = identity
        return self.files[name]


class CleanupSettings(BaseModel):
    enabled: bool = True
    retention_days: int = Field(default=31, ge=0, le=31, strict=True)


class FilterCleanup:
    """Caller serializes access with the same lock used for sync and reload."""

    def __init__(self, filters_dir: Path, state_path: Path):
        self.root = filters_dir.resolve()
        self.state_path = state_path
        self.settings = CleanupSettings()
        self.active: set[str] = set()
        self.retired: dict[str, dict] = {}
        # Invalid state must never silently restore automatic deletion settings.
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.settings = CleanupSettings.model_validate(data["settings"])
            self.active = set(data["active"])
            self.retired = data["retired"]
            for name in self.active | self.retired.keys():
                self._lexical_path(name)
            for entry in self.retired.values():
                if not math.isfinite(entry["superseded_at"]) or entry["superseded_at"] < 0:
                    raise ValueError("Invalid filter retirement time")
                if len(entry["identity"]) != 2 or not all(type(v) is int for v in entry["identity"]):
                    raise ValueError("Invalid filter identity")

    def _lexical_path(self, name: str) -> Path:
        path = self.root / name
        if (Path(name).is_absolute() or ".." in Path(name).parts
                or path.suffix.lower() != ".hf" or not path.is_relative_to(self.root)):
            raise ValueError("Unsafe cleanup filter path")
        return path

    def _path(self, name: str) -> Path:
        path = self._lexical_path(name)
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Cleanup filter path escapes filters directory")
        # Do not delete through symlinks or Windows junctions, even within root.
        for part in (path, *path.parents):
            if part == self.root:
                break
            if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
                raise ValueError("Cleanup does not follow linked paths")
        return path

    def _names(self, paths) -> set[str]:
        names = set()
        for raw in set(paths):
            path = Path(raw).absolute()
            if path.is_relative_to(self.root):
                name = path.relative_to(self.root).as_posix()
                self._lexical_path(name)
                names.add(os.path.normcase(name).replace(os.sep, "/"))
        return names

    @staticmethod
    def _identity(path: Path) -> list[int]:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def _save(self, settings, active, retired) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "settings": settings.model_dump(), "active": sorted(active), "retired": retired,
        }, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)
        self.settings, self.active, self.retired = settings, active, retired

    def configure(self, settings: CleanupSettings) -> None:
        self._save(settings, self.active, self.retired)

    @staticmethod
    @lru_cache(maxsize=512)
    def _build_range(directory: str):
        match = re.fullmatch(r"(?:\.generations/[0-9a-f]{32}/)?(\d{6})/(\d{8})_(\d{8})", directory)
        if not match:
            return None
        month, start, end = match.groups()
        try:
            start_date = datetime.strptime(start, "%Y%m%d")
            end_date = datetime.strptime(end, "%Y%m%d")
        except ValueError:
            return None
        if start_date >= end_date or month != end[:6]:
            return None
        return start_date, end_date

    @classmethod
    def _range(cls, name: str):
        directory, _, filename = name.rpartition("/")
        dates = cls._build_range(directory)
        if (dates is None or len(filename[:2]) != 2
                or any(c not in "0123456789abcdef" for c in filename[:2])
                or filename[2:] != "_" + directory.rsplit("/", 1)[1] + ".hf"):
            return None
        return filename[:2], *dates

    def scan(self):
        return _FileScan(self.root)

    @classmethod
    def build_directories(cls, scan):
        roots = [""]
        roots.extend(f".generations/{generation}" for generation in scan.entries(".generations")
                     if re.fullmatch(r"[0-9a-f]{32}", generation))
        for prefix in roots:
            for month in scan.entries(prefix):
                if not re.fullmatch(r"\d{6}", month):
                    continue
                month_dir = f"{prefix}/{month}" if prefix else month
                for build in scan.entries(month_dir):
                    directory = f"{month_dir}/{build}"
                    if cls._build_range(directory) is not None and scan.directory(directory):
                        yield directory

    @staticmethod
    def _covers(ranges, start, end):
        covered_until = start
        for range_start, range_end in ranges:
            if range_start > covered_until:
                break
            covered_until = max(covered_until, range_end)
            if covered_until >= end:
                return True
        return False

    def superseded_paths(self, active: set[str], *, scan=None) -> set[str]:
        """Require full date coverage by loaded files of the same hash shard.

        Build directories are YYYYMM/start_end and shards are hh_start_end.hf.
        A newer directory alone is not evidence that an older range is covered.
        Adjacent loaded ranges may jointly cover an older range; gaps may not.
        """
        scan = scan or self.scan()
        coverage = {}
        for name in active:
            parsed = self._range(name)
            if parsed and scan.has_file(name):
                shard, start, end = parsed
                coverage.setdefault(shard, []).append((start, end))
        for ranges in coverage.values():
            ranges.sort()
        if not coverage:
            return set()
        superseded = set()
        for directory in self.build_directories(scan):
            covered_shards = {shard for shard, ranges in coverage.items()
                              if self._covers(ranges, *self._build_range(directory))}
            if not covered_shards:
                continue
            for filename in scan.entries(directory):
                name = f"{directory}/{filename}"
                if (name not in active and filename[:2] in covered_shards
                        and self._range(name) is not None and scan.has_file(name)):
                    superseded.add(name)
        return superseded

    def loaded(self, paths, *, now: float | None = None, scan=None) -> None:
        """Called only after a nonempty, complete load and release of old mappings."""
        active = self._names(paths)
        if not active:
            return
        now = time.time() if now is None else now
        scan = scan or self.scan()
        superseded = self.superseded_paths(active, scan=scan)
        # Custom, undated manifests retain explicit previous-load tracking. Dated
        # builds always require coverage, including entries from older state files.
        superseded.update(name for name in self.active - active if self._range(name) is None)
        retired = {name: entry for name, entry in self.retired.items()
                   if name not in active and (self._range(name) is None or name in superseded)}
        for name in superseded:
            if name in retired:
                continue
            identity = scan.identity(name)
            if identity is not None:
                retired[name] = {"superseded_at": now, "identity": identity}
        if active != self.active or retired != self.retired:
            self._save(self.settings, active, retired)

    def inventory(self, protected_paths, *, scan=None) -> dict:
        """Describe existing, unchanged superseded files safe for manual deletion."""
        protected = self.active | self._names(protected_paths)
        scan = scan or self.scan()
        files = []
        for name, entry in sorted(self.retired.items()):
            if name in protected:
                continue
            identity = scan.identity(name)
            if identity is not None and identity == entry["identity"]:
                files.append([name, identity, entry["superseded_at"]])
        return {
            "filter_count": len(files),
            "total_bytes": sum(identity[0] for _, identity, _ in files),
            "confirmation_token": hashlib.sha256(json.dumps(files).encode()).hexdigest(),
        }

    def _remove_empty_build_directories(self, protected: set[str]) -> None:
        protected_directories = {name.rpartition("/")[0] for name in protected}
        for directory in self.build_directories(self.scan()):
            if directory in protected_directories:
                continue
            path = self.root / directory
            try:
                # Recheck containment and linked ancestors immediately before removal.
                if not path.resolve().is_relative_to(self.root) or not self.scan().directory(directory):
                    continue
                # Never recursively delete: files or subdirectories keep a build intact.
                path.rmdir()
                logger.info("Removed empty filter build directory %s", path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    logger.warning("Could not remove empty filter build directory %s; will retry", path,
                                   exc_info=True)

    def cleanup(self, protected_paths, *, now: float | None = None, manual: bool = False) -> list[str]:
        if not manual and not self.settings.enabled:
            return []
        protected = self.active | self._names(protected_paths)
        now = time.time() if now is None else now
        retired = dict(self.retired)
        deleted = []
        for name, entry in self.retired.items():
            if name in protected or (not manual and now < entry["superseded_at"] + self.settings.retention_days * 86400):
                continue
            try:
                path = self._path(name)
                if not path.exists():
                    retired.pop(name)
                    continue
                if self._identity(path) != entry["identity"]:
                    # A new download or manual replacement owns this path now.
                    retired.pop(name)
                    continue
                path.unlink()
                retired.pop(name)
                deleted.append(name)
                logger.info("Deleted superseded filter %s", path)
            except (OSError, ValueError):
                logger.exception("Could not delete superseded filter %s; will retry", name)
        if retired != self.retired:
            self._save(self.settings, self.active, retired)
        self._remove_empty_build_directories(protected)
        return deleted
