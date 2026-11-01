"""Prepare and validate immutable filter generations before publishing activation."""
from __future__ import annotations

import json
import os
import re
import uuid
import logging
from datetime import datetime
from pathlib import Path

from core.hash import credential_digest
from core.main import HushFilter
from .sync import SyncError, calculate_md5, file_metadata

CANARIES = tuple((f"testusername{i}@nwebbed.com", f"testpassword{i}") for i in range(1, 5))


def _sync_directory(path):
    if os.name != "posix":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        logging.getLogger("hushclient.sync").warning("Directory fsync unavailable for %s", path)


def atomic_json(path: Path, payload: dict) -> None:
    """Publish within a directory volume, rather than replace a file bind mount."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def pending_path() -> Path:
    return Path(os.getenv("HUSHCLIENT_PENDING_MANIFEST_PATH", "filters/.pending_manifest.json"))


def active_path() -> Path:
    return Path(os.getenv("HUSHCLIENT_ACTIVE_MANIFEST_PATH", "filters/.active_manifest.json"))


def load_candidate(current_path):
    pending = pending_path().exists()
    source = pending_path() if pending else Path(current_path)
    return source, json.loads(source.read_text(encoding="utf-8")), pending


def stage_sync(sync, loaded_paths):
    """A failed download can only write to its newly allocated generation."""
    reuse = {}
    filters_root = Path("filters").resolve()
    for raw in loaded_paths:
        path = Path(raw)
        location = path.parent.parent.name + "/" + path.parent.name
        if location not in reuse and len(path.parts) >= 3 and path.resolve().is_relative_to(filters_root):
            reuse[location] = path.parent
    generation = Path("filters/.generations") / uuid.uuid4().hex
    if not generation.resolve().is_relative_to(filters_root):
        raise SyncError("Generation storage must stay within the filters directory")
    generation.mkdir(parents=True)
    # A failed new attempt must not leave a previous candidate selected for reload.
    pending_path().unlink(missing_ok=True)
    result = sync(staging_dir=generation, reuse_locations=reuse)
    if not result.verified_filters:
        raise SyncError("Sync produced no verified candidate filters")
    def manifest_name(path):
        absolute = path.absolute()
        return absolute.relative_to(Path.cwd()).as_posix() if absolute.is_relative_to(Path.cwd()) else str(absolute)
    files = [manifest_name(entry.path) for entry in result.verified_filters]
    if len(set(files)) != len(files):
        raise SyncError("Duplicate candidate filter paths")
    for directory, _, _ in os.walk(generation, topdown=False):
        _sync_directory(directory)
    _sync_directory(generation.parent)
    payload = {
        "version": "1.0", "filters": sorted(files),
        "source_manifest": json.loads(result.manifest_path.read_text(encoding="utf-8")),
        "verified_files": {manifest_name(entry.path): entry.record() for entry in result.verified_filters},
    }
    atomic_json(pending_path(), payload)
    return result


def check_identities(payload):
    records = payload.get("verified_files", {})
    if set(records) != set(payload["filters"]):
        raise SyncError("Candidate verification records do not match the filter set")
    for raw, expected in records.items():
        info = Path(raw).stat()
        if (info.st_size, info.st_mtime_ns) != (expected["size"], expected["mtime_ns"]):
            raise SyncError(f"Candidate filter changed after verification: {raw}")
        for key, actual in (("device", info.st_dev), ("inode", info.st_ino), ("ctime_ns", info.st_ctime_ns)):
            if expected.get(key) is not None and expected[key] != actual:
                raise SyncError(f"Candidate filter identity changed after verification: {raw}")


def verify_manual_manifest(payload, progress):
    """Standalone reloads still verify every file before opening candidate mappings."""
    records = {}
    previous = payload.get("verified_files", {})
    source = payload.get("source_manifest")
    if source is None and Path("filters/manifest_current.json").exists():
        source = json.loads(Path("filters/manifest_current.json").read_text(encoding="utf-8"))
    expected_md5 = {"/".join(Path(entry["path"]).parts[-2:]): entry["md5"]
                    for entry in (source or {}).get("current_filter_files", [])}
    paths = payload.get("filters", [])
    for index, raw in enumerate(paths):
        progress("validate", index, len(paths), f"Verifying filter {index + 1}/{len(paths)}")
        path = Path(raw)
        before = path.stat()
        digest = calculate_md5(path)
        after = path.stat()
        if file_metadata(before) != file_metadata(after):
            raise SyncError(f"Filter changed during verification: {raw}")
        expected = previous.get(raw, {}).get("md5") or expected_md5.get(path.parent.name + "/" + path.name)
        if expected and digest != expected:
            raise SyncError(f"Filter MD5 mismatch for {raw}")
        records[raw] = {"md5": digest, "size": after.st_size, "mtime_ns": after.st_mtime_ns,
                        "device": after.st_dev, "inode": after.st_ino, "ctime_ns": after.st_ctime_ns}
    result = {**payload, "verified_files": records}
    if source is not None:
        result["source_manifest"] = source
    return result


def inspect_retained_groups(manager, root, log):
    """Report old retained groups without letting an obsolete failure block repair."""
    from .cleanup import FilterCleanup, _FileScan
    scan = _FileScan(root.resolve())
    active = {os.path.normcase(str(Path(raw).parent.absolute())) for raw, _ in manager.filters}
    for directory in FilterCleanup.build_directories(scan):
        path = root / directory
        if os.path.normcase(str(path.absolute())) in active:
            continue
        try:
            names = scan.entries(directory)
            missing = {f"{i:02x}_{path.name}.hf" for i in range(256)} - names.keys()
            if missing:
                raise SyncError(f"{len(missing)} shards missing")
            for username, password in CANARIES:
                shard = credential_digest(username, password).hex()[:2]
                name = f"{directory}/{shard}_{path.name}.hf"
                if scan.identity(name) is None:
                    raise SyncError(f"Unsafe or missing canary shard {shard}")
                bloom = HushFilter(str(root / name))
                try:
                    if not bloom.check(username, password):
                        raise SyncError(f"{username} failed in shard {shard}")
                finally:
                    bloom.close()
            log(f"INFO retained group {directory}: all four test credentials passed (diagnostic)")
        except Exception as exc:
            log(f"WARNING retained group {directory} failed diagnostic: {exc}; candidate activation is independent")


def validate_groups(manager, payload, progress, log, *, test_mode=False):
    """Check every group independently; no global first-positive short circuit."""
    if not manager.filters or len(manager.filters) != len(payload["filters"]):
        raise SyncError("The candidate filter set did not load completely")
    groups = {}
    for raw, bloom in manager.filters:
        path = Path(raw)
        match = re.fullmatch(r"([0-9a-f]{2})_(\d{8})_(\d{8})\.hf", path.name)
        if match:
            shard, start, end = match.groups()
            if datetime.strptime(start, "%Y%m%d") >= datetime.strptime(end, "%Y%m%d"):
                raise SyncError(f"Invalid build date range in {raw}")
            if path.parent.name != f"{start}_{end}" or path.parent.parent.name != end[:6]:
                raise SyncError(f"Filter filename does not match its build group: {raw}")
        elif test_mode and re.fullmatch(r"[0-9a-f]{2}\.hf", path.name):
            shard = path.stem
        else:
            raise SyncError(f"Unrecognized shard filename: {raw}")
        group = groups.setdefault(str(path.parent), {})
        if shard in group:
            raise SyncError(f"Duplicate shard {shard} in {path.parent}")
        group[shard] = bloom
    for group, shards in groups.items():
        missing = {f"{i:02x}" for i in range(256)} - shards.keys()
        if missing or len(shards) != 256:
            raise SyncError(f"Incomplete shard group {group}: missing {', '.join(sorted(missing))}")
    total = len(groups) * len(CANARIES)
    completed = 0
    failures = []
    for group, shards in sorted(groups.items()):
        passed = 0
        for username, password in CANARIES:
            shard = credential_digest(username, password).hex()[:2]
            progress("validate", completed, total, f"Checking {username} in {group} (shard {shard})")
            if not shards[shard].check(username, password):
                failure = f"Test credential {username} failed in group {group}, shard {shard}"
                failures.append(failure)
                log(f"ERROR {failure}")
            else:
                passed += 1
            completed += 1
        log(f"{'INFO' if passed == 4 else 'ERROR'} validated group {group}: all 256 shards loaded; {passed}/4 test credentials passed")
    if failures:
        raise SyncError("; ".join(failures))
    check_identities(payload)
    progress("validate", total, total, f"Validated all {len(groups)} groups and {total} test credential checks", status="complete")
    return len(groups)
