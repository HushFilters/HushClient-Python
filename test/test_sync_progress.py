import hashlib
import json
from importlib import import_module

import pytest

import api
from filter_sync import progress
from filter_sync.r2_client import R2Client, R2Config
from filter_sync.sync import SyncError, sync_filters
from test.test_filter_sync import FakeDownloader, FakeStreamingResponse, FakeStreamingSession, _make_zip_bytes


def downloader_fixture(tmp_path, *, corrupt=False):
    archives = {}
    zips = []
    filters = []
    for location in ("first", "second"):
        files = {"00.hf": b"first filter", "01.hf": b"second filter"}
        content = _make_zip_bytes(files)
        archives[f"filters/{location}/filters.zip"] = content
        zips.append({"path": f"{location}/filters.zip", "md5": hashlib.md5(content).hexdigest()})
        filters.extend({"path": f"{location}/{name}", "md5": hashlib.md5(data if not corrupt else b"wrong").hexdigest()}
                       for name, data in files.items())
    return FakeDownloader(manifest_payload=json.dumps({"current_filter_zips": zips, "current_filter_files": filters}), objects=archives)


def test_real_sync_reports_counted_phases_and_verified_completion(tmp_path):
    state = api._SyncOperationLogState()
    state.start("sync_filters")
    updates = []
    def receive(*args):
        updates.append(args)
        state.update_progress(*args)
    with progress.listen(receive):
        result = sync_filters(base_dir=tmp_path / "filter_sync", downloader=downloader_fixture(tmp_path))
    assert len(result.downloaded) == 2
    state.set_result({"success": True})
    state.finish()
    phases = state.snapshot()["progress"]
    assert [p["phase"] for p in phases] == ["prepare", "download", "extract", "verify"]
    assert all(p["status"] == "complete" for p in phases)
    # Half of the files in the first of two archives: 0.5 / 2 == 25%.
    assert any(phase == "extract" and done == .5 and total == 2 for phase, done, total, *_ in updates)
    assert any(phase == "verify" and done == 1.5 and total == 2 for phase, done, total, *_ in updates)
    count = len(updates)
    progress.report("download", 1, 1)
    assert len(updates) == count  # Listener is removed when the operation ends.


def test_failed_verification_retains_phase_and_does_not_complete(tmp_path):
    state = api._SyncOperationLogState()
    state.start("sync_filters")
    with progress.listen(state.update_progress), pytest.raises(SyncError):
        sync_filters(base_dir=tmp_path / "filter_sync", downloader=downloader_fixture(tmp_path, corrupt=True))
    state.set_result({"success": False})
    state.finish()
    phases = {p["phase"]: p for p in state.snapshot()["progress"]}
    assert phases["download"]["status"] == "complete"
    assert phases["verify"]["status"] == "failed"
    assert phases["verify"]["completed"] < phases["verify"]["total"]


@pytest.mark.parametrize("headers", [{"Content-Length": "8"}, {}])
def test_download_byte_progress_and_unknown_size(tmp_path, headers):
    response = FakeStreamingResponse([b"1234", b"5678"], headers=headers)
    client = R2Client(R2Config(endpoint="https://example.com", access_key_id="test", secret_access_key="test"),
                      session=FakeStreamingSession(response))
    updates = []
    with progress.listen(lambda *args: updates.append(args)):
        client.download_file("filters/archive.zip", tmp_path / "archive.zip")
    assert any(phase == "download" and done == 4 for phase, done, *_ in updates)
    assert updates[-1][1] == 8
    assert updates[-1][2] == (8 if headers else None)


def test_manual_phases_reset_and_snapshot_is_independent():
    state = api._SyncOperationLogState()
    state.start("sync_apply")
    state.update_progress("download", 3, 10)
    snapshot = state.snapshot()
    snapshot["progress"][1]["completed"] = 99
    assert state.snapshot()["progress"][1]["completed"] == 3
    state.start("sync_reload")
    assert [p["phase"] for p in state.snapshot()["progress"]] == ["reload"]
    assert state.snapshot()["progress"][0]["status"] == "running"


def test_failure_after_verification_does_not_mark_unstarted_reload_failed():
    state = api._SyncOperationLogState()
    state.start("sync_apply")
    for phase in ("prepare", "download", "extract", "verify"):
        state.update_progress(phase, 1, 1, status="complete")
    state.set_result({"success": False, "detail": "Archive cleanup failed"})
    phases = {p["phase"]: p for p in state.snapshot()["progress"]}
    assert phases["verify"]["status"] == "failed"
    assert phases["reload"]["status"] == "pending"


def local_filters_fixture(tmp_path):
    downloader = downloader_fixture(tmp_path)
    for location in ("first", "second"):
        directory = tmp_path / "filters" / location
        directory.mkdir(parents=True)
        entries = []
        for name, content in {"00.hf": b"first filter", "01.hf": b"second filter"}.items():
            (directory / name).write_bytes(content)
            entries.append({"path": name, "md5": hashlib.md5(content).hexdigest()})
        downloader._text_objects[f"filters/{location}/upload_manifest.json"] = json.dumps({"files": entries})
    return downloader


def test_local_verification_publishes_live_progress_before_archive_finishes(tmp_path, monkeypatch):
    downloader = local_filters_fixture(tmp_path)
    state = api._SyncOperationLogState()
    state.start("sync_filters")
    sync_module = import_module("filter_sync.sync")
    calculate_md5 = sync_module.calculate_md5
    live_snapshots = []

    def inspect_progress_while_hashing(path):
        if path.name == "01.hf":
            # Inspect the same payload the UI polls, before the second hash
            # completes. File one must already have advanced this archive.
            payload = api.SyncStatusResponse(**state.snapshot()).model_dump()
            assert payload["active"] is True
            live_snapshots.append(next(p for p in payload["progress"] if p["phase"] == "download"))
        return calculate_md5(path)

    monkeypatch.setattr(sync_module, "calculate_md5", inspect_progress_while_hashing)
    with progress.listen(state.update_progress):
        result = sync_filters(base_dir=tmp_path / "filter_sync", downloader=downloader)

    assert len(live_snapshots) == 2
    for index, phase in enumerate(live_snapshots):
        assert phase["status"] == "running"
        assert index / 2 < phase["completed"] / phase["total"] < (index + 1) / 2
        assert f"Archive {index + 1}/2" in phase["detail"]
        assert "1/2 verified" in phase["detail"]
    assert len(result.verified_existing) == 2
    assert downloader.file_requests == []
    phases = {p["phase"]: p for p in state.snapshot()["progress"]}
    assert phases["download"]["status"] == "complete"
    assert phases["download"]["completed"] == phases["download"]["total"]
    assert phases["extract"]["status"] == phases["verify"]["status"] == "skipped"


@pytest.mark.parametrize("unusable", ["missing", "mismatch"])
def test_local_verification_fallback_keeps_download_progress_moving_forward(tmp_path, unusable):
    downloader = local_filters_fixture(tmp_path)
    local_file = tmp_path / "filters" / "first" / "01.hf"
    if unusable == "missing":
        local_file.unlink()
    else:
        local_file.write_bytes(b"corrupted filter")

    download_file = downloader.download_file
    def download_with_progress(key, destination):
        progress.report("download", 0, 2, "Downloading archive")
        progress.report("download", 1, 2, "Downloading archive")
        download_file(key, destination)
        progress.report("download", 2, 2, "Downloading archive")
    downloader.download_file = download_with_progress
    updates = []
    with progress.listen(lambda *args: updates.append(args)):
        result = sync_filters(base_dir=tmp_path / "filter_sync", downloader=downloader)

    download_updates = [update for update in updates if update[0] == "download"]
    fractions = [completed / total for _, completed, total, *_ in download_updates]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1
    assert any("1/2 verified" in update[3] for update in download_updates)
    assert len(result.downloaded) == len(result.verified_existing) == 1
    assert local_file.read_bytes() == b"second filter"
