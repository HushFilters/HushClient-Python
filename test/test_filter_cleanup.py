import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api
from filter_sync.cleanup import CleanupSettings, FilterCleanup
from filter_sync.inventory_job import InventoryJob


@pytest.fixture(autouse=True)
def isolate_inventory_job(monkeypatch):
    job = InventoryJob()
    monkeypatch.setattr(api, "cleanup_inventory_job", job)
    yield job
    job.join()


@pytest.fixture
def setup_cleanup(tmp_path):
    root = tmp_path / "filters"
    root.mkdir()
    old, new, untracked = (root / name for name in ("old.hf", "new.hf", "untracked.hf"))
    for path in (old, new, untracked):
        path.write_bytes(b"filter")
    cleanup = FilterCleanup(root, root / ".cleanup_state.json")
    cleanup.loaded([old], now=0)
    return cleanup, old, new, untracked


def test_default_retention_starts_at_successful_replacement_and_survives_restart(setup_cleanup):
    cleanup, old, new, untracked = setup_cleanup
    assert cleanup.settings == CleanupSettings(enabled=True, retention_days=31)
    cleanup.loaded([new], now=100)
    cleanup.loaded([new], now=200)  # Repeated syncs must not restart retention.
    cleanup = FilterCleanup(cleanup.root, cleanup.state_path)
    assert cleanup.cleanup([new], now=100 + 31 * 86400 - 1) == []
    assert old.exists()
    assert cleanup.cleanup([new], now=100 + 31 * 86400) == ["old.hf"]
    assert not old.exists()
    assert new.exists() and untracked.exists()


@pytest.mark.parametrize("days", [0, 1, 7, 31])
def test_configured_retention_boundary(setup_cleanup, days):
    cleanup, old, new, _ = setup_cleanup
    cleanup.configure(CleanupSettings(retention_days=days))
    cleanup.loaded([new], now=100)
    assert cleanup.cleanup([new], now=99 + days * 86400) == []
    assert cleanup.cleanup([new], now=100 + days * 86400) == ["old.hf"]


def test_off_persists_and_reenable_uses_original_retirement_time(setup_cleanup):
    cleanup, old, new, _ = setup_cleanup
    cleanup.configure(CleanupSettings(enabled=False, retention_days=1))
    cleanup.loaded([new], now=100)
    cleanup = FilterCleanup(cleanup.root, cleanup.state_path)
    assert cleanup.cleanup([new], now=1000000) == []
    assert old.exists()
    cleanup.configure(CleanupSettings(retention_days=1))
    assert cleanup.cleanup([new], now=1000000) == ["old.hf"]


def test_active_and_pending_reload_files_are_protected(setup_cleanup):
    cleanup, old, new, _ = setup_cleanup
    cleanup.configure(CleanupSettings(retention_days=0))
    cleanup.loaded([new], now=100)
    assert cleanup.cleanup([old, new], now=200) == []
    cleanup.loaded([old], now=300)
    assert "old.hf" not in cleanup.retired
    assert cleanup.cleanup([old], now=300) == ["new.hf"]
    assert old.exists()


def test_empty_load_does_not_retire_active_files(setup_cleanup):
    cleanup, old, _, _ = setup_cleanup
    cleanup.loaded([], now=100)
    assert cleanup.active == {"old.hf"}
    assert cleanup.retired == {}


def test_changed_or_manually_deleted_files_are_not_deleted(setup_cleanup):
    cleanup, old, new, _ = setup_cleanup
    cleanup.configure(CleanupSettings(retention_days=0))
    cleanup.loaded([new], now=100)
    old.write_bytes(b"a newer download now lives here")
    assert cleanup.cleanup([new], now=200) == []
    assert old.exists()
    cleanup.loaded([old], now=300)
    new.unlink()
    assert cleanup.cleanup([old], now=400) == []
    assert cleanup.retired == {}


def test_deletion_failure_retries_without_losing_retirement(setup_cleanup, monkeypatch):
    cleanup, old, new, _ = setup_cleanup
    cleanup.configure(CleanupSettings(retention_days=0))
    cleanup.loaded([new], now=100)
    with monkeypatch.context() as patch:
        def denied(_path):
            raise PermissionError("file busy")
        patch.setattr(Path, "unlink", denied)
        assert cleanup.cleanup([new], now=200) == []
        assert "old.hf" in cleanup.retired
    assert cleanup.cleanup([new], now=201) == ["old.hf"]


@pytest.mark.parametrize("name", ["../outside.hf", "nested/../../outside.hf", "settings.json"])
def test_unsafe_state_fails_closed(setup_cleanup, name):
    cleanup, _, _, _ = setup_cleanup
    data = json.loads(cleanup.state_path.read_text())
    data["active"] = [name]
    cleanup.state_path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        FilterCleanup(cleanup.root, cleanup.state_path)


def test_settings_api_validates_persists_and_serializes_with_sync(setup_cleanup, monkeypatch):
    cleanup, _, _, _ = setup_cleanup
    monkeypatch.setattr(api, "filter_cleanup", cleanup)
    client = TestClient(api.app)
    assert client.get("/sync/cleanup").json() == {"enabled": True, "retention_days": 31}
    for value in (-1, 32, 1.5, True, "7"):
        assert client.put("/sync/cleanup", json={"retention_days": value}).status_code == 422
    for settings in ({"enabled": True, "retention_days": 0}, {"enabled": False, "retention_days": 7}):
        assert client.put("/sync/cleanup", json=settings).json() == settings
        assert FilterCleanup(cleanup.root, cleanup.state_path).settings.model_dump() == settings
    with api.operation_lock:
        assert client.put("/sync/cleanup", json={"retention_days": 1}).status_code == 409


def test_partial_reload_preserves_previous_manager_and_files(setup_cleanup, monkeypatch):
    cleanup, old, new, _ = setup_cleanup
    cleanup.configure(CleanupSettings(retention_days=0))
    class Manager:
        filters = [(str(new), object())]
        _requested_filter_count = 2
        closed = False
        def close(self):
            self.closed = True
    candidate = Manager()
    previous = object()
    monkeypatch.setattr(api, "filter_manager", previous)
    monkeypatch.setattr(api, "filter_cleanup", cleanup)
    monkeypatch.setattr(api, "FilterManager", lambda **kwargs: candidate)
    monkeypatch.setattr(api.activation, "load_candidate", lambda current: (
        Path("candidate.json"), {"filters": [str(new)], "verified_files": {}}, True))
    monkeypatch.setattr(api.activation, "check_identities", lambda payload: None)
    result = api._run_filter_reload_with_logs()
    assert not result.success
    assert api.filter_manager is previous
    assert candidate.closed
    assert old.exists()
    assert cleanup.retired == {}


def test_successful_reload_closes_old_mappings_before_immediate_cleanup(setup_cleanup, monkeypatch):
    cleanup, old, new, _ = setup_cleanup
    cleanup.configure(CleanupSettings(retention_days=0))
    class Manager:
        _requested_filter_count = 1
        def __init__(self, path):
            self.filters = [(str(path), object())]
            self.closed = False
        def close(self):
            assert old.exists()
            self.closed = True
        def get_stats(self):
            return {"filter_count": 1, "filters": [str(new)], "max_nk": 1}
    previous, candidate = Manager(old), Manager(new)
    monkeypatch.setattr(api, "filter_manager", previous)
    monkeypatch.setattr(api, "filter_cleanup", cleanup)
    monkeypatch.setattr(api, "FilterManager", lambda **kwargs: candidate)
    monkeypatch.setattr(api.activation, "load_candidate", lambda current: (
        Path("candidate.json"), {"filters": [str(new)], "verified_files": {}}, True))
    monkeypatch.setattr(api.activation, "check_identities", lambda payload: None)
    monkeypatch.setattr(api.activation, "validate_groups", lambda *args, **kwargs: 1)
    monkeypatch.setattr(api.activation, "inspect_retained_groups", lambda *args: None)
    monkeypatch.setattr(api, "_cleanup_protected_paths", lambda: [new])
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    result = api._run_filter_reload_with_logs()
    assert result.success and previous.closed
    assert api.filter_manager is candidate
    assert not old.exists() and new.exists()


def test_startup_records_filters_but_defers_expired_deletion_until_reload(setup_cleanup, monkeypatch):
    cleanup, old, new, _ = setup_cleanup
    cleanup.configure(CleanupSettings(retention_days=0))
    cleanup.loaded([new], now=100)
    monkeypatch.setattr(api, "filter_cleanup", cleanup)
    monkeypatch.setattr(api, "filter_manager", type("Manager", (), {"filters": [(str(new), None)]})())
    monkeypatch.setattr(api, "_cleanup_protected_paths", lambda: [new])
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    api._record_loaded_filter_cleanup(delete_expired=False)
    assert old.exists()
    api._record_loaded_filter_cleanup()
    assert not old.exists()


def test_failed_settings_write_keeps_current_policy(setup_cleanup, monkeypatch):
    cleanup, _, _, _ = setup_cleanup
    def denied(*args):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(OSError):
        cleanup.configure(CleanupSettings(enabled=False))
    assert cleanup.settings.enabled


@pytest.mark.skipif(os.name != "nt", reason="Case-insensitive Windows paths")
def test_casing_change_cannot_retire_active_file(setup_cleanup):
    cleanup, old, _, _ = setup_cleanup
    cleanup.configure(CleanupSettings(retention_days=0))
    cleanup.loaded([old.with_name("OLD.hf")], now=100)
    assert cleanup.retired == {}
    assert cleanup.cleanup([old], now=200) == []
    assert old.exists()


def test_protection_includes_both_pending_manifests(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "filter_manager", type("Manager", (), {"filters": [("filters/active.hf", None)]})())
    monkeypatch.setattr(api, "_current_filter_configuration", lambda: "manifest.json")
    (tmp_path / "manifest.json").write_text(json.dumps({"filters": ["filters/pending.hf"]}))
    root = tmp_path / "filters"
    root.mkdir()
    (root / "manifest_current.json").write_text(json.dumps({
        "current_filter_locations": ["month/batch"],
        "current_filter_files": [{"path": "batch/new.hf"}],
    }))
    assert set(map(Path, api._cleanup_protected_paths())) == {
        Path("filters/active.hf"), Path("filters/pending.hf"), Path("filters/month/batch/new.hf"),
    }


@pytest.fixture
def manual_client(setup_cleanup, monkeypatch):
    cleanup, old, new, untracked = setup_cleanup
    cleanup.configure(CleanupSettings(enabled=False, retention_days=31))
    cleanup.loaded([new])
    monkeypatch.setattr(api, "filter_cleanup", cleanup)
    monkeypatch.setattr(api, "filter_manager", type("Manager", (), {"filters": [(str(new), None)]})())
    monkeypatch.setattr(api, "_cleanup_protected_paths", lambda: [new])
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    return TestClient(api.app), cleanup, old, new, untracked


def test_inventory_and_confirmed_delete_work_with_automatic_cleanup_off(manual_client, isolate_inventory_job):
    client, cleanup, old, new, untracked = manual_client
    inventory = client.get("/sync/cleanup/files").json()
    assert inventory["filter_count"] == 1
    assert inventory["total_bytes"] == old.stat().st_size
    assert old.exists()  # Reading totals has no deletion side effects.
    assert client.post("/sync/cleanup/delete", json={}).status_code == 422
    result = client.post("/sync/cleanup/delete", json={"confirmation_token": inventory["confirmation_token"]})
    assert result.status_code == 200
    assert result.json()["deleted_count"] == 1
    assert result.json()["deleted_bytes"] == 6
    assert result.json()["filter_count"] == result.json()["total_bytes"] == 0
    assert not old.exists() and new.exists() and untracked.exists()
    assert not cleanup.settings.enabled
    isolate_inventory_job.join()
    snapshot = isolate_inventory_job.snapshot()
    assert snapshot["inventory"]["filter_count"] == 0
    assert snapshot["checked_at"] and snapshot["error"] is None


def test_manual_delete_waits_for_sync_and_rejects_changed_set(manual_client):
    client, cleanup, old, new, _ = manual_client
    token = client.get("/sync/cleanup/files").json()["confirmation_token"]
    with ThreadPoolExecutor() as pool:
        with api.operation_lock:
            pending = pool.submit(client.post, "/sync/cleanup/delete", json={"confirmation_token": token})
            assert client.get("/sync/cleanup/files").status_code == 409
            assert not pending.done()
            old.write_bytes(b"replacement download")
        assert pending.result(timeout=5).status_code == 409
    assert old.exists() and new.exists()
    assert client.get("/sync/cleanup/files").json()["filter_count"] == 0


def test_inventory_excludes_protected_and_missing_files(manual_client, monkeypatch):
    client, _, old, new, _ = manual_client
    monkeypatch.setattr(api, "_cleanup_protected_paths", lambda: [old, new])
    inventory = client.get("/sync/cleanup/files").json()
    assert inventory["filter_count"] == inventory["total_bytes"] == 0
    monkeypatch.setattr(api, "_cleanup_protected_paths", lambda: [new])
    old.unlink()
    assert client.get("/sync/cleanup/files").json()["total_bytes"] == 0


def test_manual_delete_reports_files_remaining_after_failure(manual_client, monkeypatch):
    client, cleanup, old, _, _ = manual_client
    token = client.get("/sync/cleanup/files").json()["confirmation_token"]
    def denied(path):
        raise PermissionError("File busy")
    monkeypatch.setattr(Path, "unlink", denied)
    result = client.post("/sync/cleanup/delete", json={"confirmation_token": token}).json()
    assert result["deleted_count"] == result["deleted_bytes"] == 0
    assert result["filter_count"] == 1 and result["total_bytes"] == 6
    assert old.exists() and cleanup.retired


def test_manual_delete_refuses_partial_load(manual_client, monkeypatch):
    client, _, old, _, _ = manual_client
    token = client.get("/sync/cleanup/files").json()["confirmation_token"]
    monkeypatch.setattr(api.filter_manager, "_requested_filter_count", 2, raising=False)
    assert client.post("/sync/cleanup/delete", json={"confirmation_token": token}).status_code == 409
    assert old.exists()


def dated_filter(root, start, end, shard="00"):
    path = root / end[:6] / f"{start}_{end}" / f"{shard}_{start}_{end}.hf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"filter")
    return path


@pytest.mark.parametrize("generation", [False, True])
def test_cleanup_removes_empty_builds_after_last_shard_and_from_previous_runs(tmp_path, generation):
    root = tmp_path / "filters"
    build_root = root / ".generations" / ("a" * 32) if generation else root
    old = dated_filter(build_root, "20260101", "20260901")
    new = dated_filter(root, "20260101", "20261001")
    leftover = build_root / "202604" / "20260401_20260408"
    leftover.mkdir(parents=True)
    cleanup = FilterCleanup(root, root / "state.json")
    cleanup.loaded([new], now=100)

    # Empty leftovers need no retirement record; retained shard files keep their folder.
    assert cleanup.cleanup([new], now=100) == []
    assert not leftover.exists() and old.parent.exists()
    assert cleanup.cleanup([new], now=100 + 31 * 86400) == [old.relative_to(root).as_posix()]
    assert not old.parent.exists()
    assert new.exists() and build_root.exists()


def test_empty_build_cleanup_preserves_protected_nonempty_and_unrecognized_folders(tmp_path):
    root = tmp_path / "filters"
    new = dated_filter(root, "20260101", "20261001")
    active_missing = root / "202609" / "20260101_20260901" / "00_20260101_20260901.hf"
    active_missing.parent.mkdir(parents=True)
    nonempty = root / "202604" / "20260401_20260408"
    nonempty.mkdir(parents=True)
    (nonempty / "notes.txt").write_text("keep")
    nested = root / "202605" / "20260501_20260508"
    (nested / "child").mkdir(parents=True)
    unknown = root / "202606" / "custom-build"
    unknown.mkdir(parents=True)
    cleanup = FilterCleanup(root, root / "state.json")
    cleanup.loaded([new, active_missing], now=100)
    # Also protect a missing shard in a pending manual reload's empty folder.
    pending = root / "202607" / "20260701_20260708" / "00_20260701_20260708.hf"
    pending.parent.mkdir(parents=True)
    assert cleanup.cleanup([new, pending], manual=True) == []
    assert all(path.is_dir() for path in (active_missing.parent, pending.parent, nonempty, nested, unknown))
    assert (nonempty / "notes.txt").read_text() == "keep"


def test_disabled_cleanup_leaves_empty_builds_until_manual_cleanup(tmp_path):
    root = tmp_path / "filters"
    empty = root / "202604" / "20260401_20260408"
    empty.mkdir(parents=True)
    cleanup = FilterCleanup(root, root / "state.json")
    cleanup.configure(CleanupSettings(enabled=False))
    assert cleanup.cleanup([]) == []
    assert empty.is_dir()
    assert cleanup.cleanup([], manual=True) == []
    assert not empty.exists()


def test_empty_build_cleanup_does_not_follow_linked_ancestors(tmp_path):
    root = tmp_path / "filters"
    root.mkdir()
    outside = tmp_path / "outside"
    empty = outside / "20260401_20260408"
    empty.mkdir(parents=True)
    try:
        (root / "202604").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    cleanup = FilterCleanup(root, root / "state.json")
    assert cleanup.cleanup([], manual=True) == []
    assert empty.is_dir()


def test_october_ytd_discovers_existing_older_builds_without_load_history(tmp_path):
    root = tmp_path / "filters"
    historic = dated_filter(root, "20010101", "20260101")
    october = dated_filter(root, "20260101", "20261001")
    old = [dated_filter(root, "20260101", f"2026{month:02d}01") for month in range(2, 10)]
    old += [dated_filter(root, "20260601", "20260608"), dated_filter(root, "20260929", "20260930")]
    later = dated_filter(root, "20261001", "20261002")
    missing_shard = dated_filter(root, "20260101", "20260901", "01")
    cleanup = FilterCleanup(root, root / "state.json")
    cleanup.loaded([historic, october], now=100)
    inventory = cleanup.inventory([historic, october])
    assert inventory["filter_count"] == len(old)
    assert inventory["total_bytes"] == len(old) * 6
    assert cleanup.cleanup([historic, october], now=100 + 31 * 86400 - 1) == []
    cleanup = FilterCleanup(root, root / "state.json")
    cleanup.loaded([historic, october], now=200)
    assert len(cleanup.cleanup([historic, october], now=100 + 31 * 86400)) == len(old)
    assert all(not path.exists() for path in old)
    assert all(path.exists() for path in (historic, october, later, missing_shard))


@pytest.mark.parametrize("ranges, expected", [
    ([("20261001", "20261008")], True),  # Weekly replaces the daily range.
    ([("20261001", "20261003"), ("20261003", "20261008")], True),
    ([("20261001", "20261002"), ("20261003", "20261008")], False),
    ([("20261004", "20261008")], False),  # Partial overlap is insufficient.
    ([("20261015", "20261022")], False),  # Newer does not mean superseded.
])
def test_range_coverage_requires_no_gaps(tmp_path, ranges, expected):
    root = tmp_path / "filters"
    old = dated_filter(root, "20261002", "20261004")
    active = [dated_filter(root, start, end) for start, end in ranges]
    cleanup = FilterCleanup(root, root / "state.json")
    cleanup.loaded(active, now=100)
    assert cleanup.inventory(active)["filter_count"] == int(expected)
    cleanup.cleanup(active, manual=True)
    assert old.exists() is not expected


def test_dated_retirement_is_retracted_when_replacement_coverage_is_lost(tmp_path):
    root = tmp_path / "filters"
    old = dated_filter(root, "20261001", "20261002")
    weekly = dated_filter(root, "20261001", "20261008")
    later = dated_filter(root, "20261015", "20261022")
    cleanup = FilterCleanup(root, root / "state.json")
    cleanup.loaded([weekly], now=100)
    assert cleanup.inventory([weekly])["filter_count"] == 1
    cleanup.loaded([later], now=200)
    assert cleanup.inventory([later])["filter_count"] == 0
    assert cleanup.cleanup([later], manual=True) == []
    assert old.exists() and weekly.exists()


def test_inventory_api_discovers_preexisting_builds_from_successfully_loaded_coverage(tmp_path, monkeypatch):
    root = tmp_path / "filters"
    old = dated_filter(root, "20260101", "20260901")
    new = dated_filter(root, "20260101", "20261001")
    cleanup = FilterCleanup(root, root / "state.json")
    monkeypatch.setattr(api, "filter_cleanup", cleanup)
    monkeypatch.setattr(api, "filter_manager", type("Manager", (), {"filters": [(str(new), None)]})())
    monkeypatch.setattr(api, "_cleanup_protected_paths", lambda: [new])
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    client = TestClient(api.app)
    inventory = client.get("/sync/cleanup/files").json()
    assert inventory["filter_count"] == 1 and old.exists()
    result = client.post("/sync/cleanup/delete", json={"confirmation_token": inventory["confirmation_token"]})
    assert result.status_code == 200 and result.json()["deleted_count"] == 1
    assert not old.exists() and new.exists()


def test_unrecognized_paths_and_missing_replacement_files_are_not_coverage(tmp_path):
    root = tmp_path / "filters"
    old = dated_filter(root, "20260101", "20260901")
    new = dated_filter(root, "20260101", "20261001")
    unknown = root / "202609" / "20260101_20260901" / "notes.hf"
    unknown.write_bytes(b"custom")
    cleanup = FilterCleanup(root, root / "state.json")
    assert cleanup.superseded_paths(cleanup._names([new])) == {old.relative_to(root).as_posix()}
    new.unlink()
    assert cleanup.superseded_paths(cleanup._names([new])) == set()
    assert unknown.exists()


def test_discovery_compares_folders_before_enumerating_uncovered_files(tmp_path, monkeypatch):
    root = tmp_path / "filters"
    old = dated_filter(root, "20260101", "20260901")
    new = dated_filter(root, "20260101", "20261001")
    later = dated_filter(root, "20261001", "20261008")
    cleanup = FilterCleanup(root, root / "state.json")
    scanned = []
    original = os.scandir
    def listing(path):
        scanned.append(Path(path))
        return original(path)
    def redundant_path_check(*args, **kwargs):
        raise AssertionError("Discovery must use directory checks, not resolve each file")
    monkeypatch.setattr(os, "scandir", listing)
    monkeypatch.setattr(cleanup, "_path", redundant_path_check)
    monkeypatch.setattr(Path, "resolve", redundant_path_check)
    cleanup.loaded([new], now=100)
    inventory = cleanup.inventory([new])
    assert inventory["filter_count"] == 1 and inventory["total_bytes"] == 6
    assert later.parent not in scanned
    assert old.parent in scanned


def test_discovery_and_inventory_share_one_metadata_read_per_candidate(tmp_path, monkeypatch):
    root = tmp_path / "filters"
    old = [dated_filter(root, "20260101", "20260901", shard) for shard in ("00", "01")]
    new = [dated_filter(root, "20260101", "20261001", shard) for shard in ("00", "01")]
    cleanup = FilterCleanup(root, root / "state.json")
    metadata_reads = []
    original = os.scandir
    class Entry:
        def __init__(self, entry):
            self.entry, self.name = entry, entry.name
        def is_file(self, **kwargs):
            return self.entry.is_file(**kwargs)
        def stat(self, **kwargs):
            metadata_reads.append(Path(self.entry.path))
            return self.entry.stat(**kwargs)
    class Listing:
        def __init__(self, path):
            self.listing = original(path)
        def __enter__(self):
            return (Entry(entry) for entry in self.listing)
        def __exit__(self, *args):
            self.listing.close()
    monkeypatch.setattr(os, "scandir", Listing)
    scan = cleanup.scan()
    cleanup.loaded(new, now=100, scan=scan)
    assert cleanup.inventory(new, scan=scan)["filter_count"] == 2
    assert sorted(metadata_reads) == sorted(old)
    # Deletion must recheck the disk, rather than use this cached inventory.
    old[0].write_bytes(b"a replacement file")
    deleted = cleanup.cleanup(new, manual=True)
    assert deleted == [old[1].relative_to(root).as_posix()]
    assert old[0].exists()


@pytest.mark.parametrize("link_kind", ["directory", "file"])
def test_scan_does_not_follow_linked_builds_or_files(tmp_path, link_kind):
    root = tmp_path / "filters"
    new = dated_filter(root, "20260101", "20261001")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "00_20260101_20260901.hf"
    target.write_bytes(b"outside")
    build = root / "202609" / "20260101_20260901"
    build.parent.mkdir()
    try:
        if link_kind == "directory":
            build.symlink_to(outside, target_is_directory=True)
        else:
            build.mkdir()
            (build / target.name).symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    cleanup = FilterCleanup(root, root / "state.json")
    cleanup.loaded([new], now=100)
    assert cleanup.inventory([new])["filter_count"] == 0
    assert cleanup.cleanup([new], manual=True) == []
    assert target.read_bytes() == b"outside"
