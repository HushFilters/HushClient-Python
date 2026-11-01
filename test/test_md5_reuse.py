"""Checksum reuse uses local fixtures and never syncs a live service."""
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api
from filter_sync import sync as sync_module
from filter_sync.settings import SyncSettings, load_settings, save_settings
from test.test_filter_sync import FakeDownloader, _make_zip_bytes


@pytest.fixture
def sync_case(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = {"00_20260401_20260408.hf": b"first-filter",
             "01_20260401_20260408.hf": b"second-filter"}
    location = "202604/20260401_20260408"
    calls = []
    calculate = sync_module.calculate_md5

    def track(path):
        calls.append(path)
        return calculate(path)

    monkeypatch.setattr(sync_module, "calculate_md5", track)

    def run(*, staged=False, source=None, **kwargs):
        zipped = _make_zip_bytes(files)
        entries = [{"path": f"20260401_20260408/{name}", "md5": hashlib.md5(data).hexdigest()}
                   for name, data in files.items()]
        archive = f"{location}/20260401_20260408.zip"
        downloader = FakeDownloader(json.dumps({
            "current_filter_locations": [location],
            "current_filter_zips": [{"path": archive, "md5": hashlib.md5(zipped).hexdigest()}],
            "current_filter_files": entries,
        }), {f"filters/{archive}": zipped},
            text_objects={f"filters/{location}/upload_manifest.json": json.dumps(entries)})
        if staged:
            import uuid
            kwargs["staging_dir"] = tmp_path / "filters/.generations" / uuid.uuid4().hex
            kwargs["reuse_locations"] = {location: source} if source else {}
        result = sync_module.sync_filters(base_dir=tmp_path / "filter_sync", downloader=downloader, **kwargs)
        return result, downloader

    return run, calls, tmp_path / "filters/.verified_md5.json", files


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("reuse", [None, False, True])
def test_new_downloads_always_hash_and_subsequent_reuse_is_opt_in(sync_case, staged, reuse):
    run, calls, records_path, _ = sync_case
    result, downloader = run(staged=staged, reuse_verified_md5=reuse)
    assert downloader.file_requests
    assert len([path for path in calls if path.suffix == ".hf"]) == 2
    records = json.loads(records_path.read_text())["files"]
    assert len(records) == 2
    assert all(record["inode"] for record in records.values())
    source = result.verified_filters[0].path.parent
    calls.clear()
    result, downloader = run(staged=staged, source=source, reuse_verified_md5=reuse)
    assert not downloader.file_requests
    assert len(calls) == (0 if reuse else 2)
    assert len(result.verified_filters) == 2


@pytest.mark.parametrize("missing", ["file", "record", "invalid_json", "old_format", "invalid_record"])
def test_missing_or_unusable_verification_forces_hashing(sync_case, missing):
    run, calls, records_path, _ = sync_case
    run(reuse_verified_md5=True)
    records = json.loads(records_path.read_text())
    if missing == "file":
        records_path.unlink()
    elif missing == "invalid_json":
        records_path.write_text("{")
    else:
        key = next(iter(records["files"]))
        if missing == "record":
            del records["files"][key]
        elif missing == "old_format":
            del records["files"][key]["inode"]
        else:
            records["files"][key] = "invalid"
        records_path.write_text(json.dumps(records))
    calls.clear()
    _, downloader = run(reuse_verified_md5=True)
    assert not downloader.file_requests
    assert len(calls) == (2 if missing in {"file", "invalid_json"} else 1)


@pytest.mark.parametrize("field", ["device", "inode", "size", "mtime_ns", "ctime_ns", "md5"])
def test_each_metadata_or_checksum_difference_forces_rehash(sync_case, field):
    run, calls, records_path, _ = sync_case
    run(reuse_verified_md5=True)
    payload = json.loads(records_path.read_text())
    record = next(iter(payload["files"].values()))
    record[field] = "0" * 32 if field == "md5" else record[field] + 1
    records_path.write_text(json.dumps(payload))
    calls.clear()
    _, downloader = run(reuse_verified_md5=True)
    assert not downloader.file_requests
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["mtime", "replacement", "size", "remote"])
def test_changed_local_files_and_remote_md5_never_reuse_old_result(sync_case, change):
    run, calls, _, files = sync_case
    result, _ = run(reuse_verified_md5=True)
    path = result.verified_filters[0].path
    before = path.stat()
    if change == "mtime":
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    elif change == "replacement":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes())
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        assert path.stat().st_ino != before.st_ino
    elif change == "size":
        path.write_bytes(b"damaged filter contents")
    else:
        files[path.name] = b"updated remote filter"
    calls.clear()
    _, downloader = run(reuse_verified_md5=True)
    assert path in calls
    assert bool(downloader.file_requests) == (change in {"size", "remote"})
    if downloader.file_requests:
        # Even shards whose old cache matched must be hashed after extraction.
        assert len([item for item in calls if item.suffix == ".hf"]) == 3
    assert path.read_bytes() == files[path.name]


def test_failed_sync_discards_previous_cached_trust(sync_case):
    run, calls, records_path, files = sync_case
    run(reuse_verified_md5=True)
    # Model a failed hash/read without changing metadata.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sync_module, "calculate_md5", lambda path: "0" * 32)
        with pytest.raises(sync_module.SyncError):
            run(reuse_verified_md5=False)
    assert json.loads(records_path.read_text())["files"] == {}
    calls.clear()
    run(reuse_verified_md5=True)
    assert len([path for path in calls if path.suffix == ".hf"]) == len(files)


def test_saved_setting_is_used_and_can_be_overridden(sync_case):
    run, calls, _, _ = sync_case
    assert load_settings().reuse_verified_md5 is False
    run()
    save_settings(SyncSettings(reuse_verified_md5=True))
    calls.clear()
    run()
    assert calls == []
    run(reuse_verified_md5=False)
    assert len(calls) == 2
    save_settings(SyncSettings())
    calls.clear()
    run()
    assert len(calls) == 2


def test_sync_settings_api_persists_validates_and_blocks_changes_during_sync(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(api.app)
    assert client.get("/sync/settings").json() == {"reuse_verified_md5": False}
    response = client.put("/sync/settings", json={"reuse_verified_md5": True})
    assert response.status_code == 200
    assert load_settings().reuse_verified_md5 is True
    assert client.get("/sync/settings").json() == {"reuse_verified_md5": True}
    assert client.put("/sync/settings", json={"reuse_verified_md5": "false"}).status_code == 422
    with api.operation_lock:
        assert client.put("/sync/settings", json={"reuse_verified_md5": False}).status_code == 409
    assert load_settings().reuse_verified_md5 is True
    assert client.put("/sync/settings", json={"reuse_verified_md5": False}).status_code == 200
    assert not load_settings().reuse_verified_md5


def test_invalid_settings_fall_back_to_full_hashing(sync_case):
    run, calls, _, _ = sync_case
    run(reuse_verified_md5=True)
    Path(os.environ["HUSHCLIENT_SYNC_SETTINGS_PATH"]).write_text('{"reuse_verified_md5": "true"}')
    calls.clear()
    run()
    assert len(calls) == 2


def test_failed_settings_save_preserves_previous_choice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    save_settings(SyncSettings(reuse_verified_md5=True))

    def fail(settings):
        raise OSError("disk full")

    monkeypatch.setattr(api, "save_sync_settings", fail)
    response = TestClient(api.app).put("/sync/settings", json={"reuse_verified_md5": False})
    assert response.status_code == 500
    assert load_settings().reuse_verified_md5 is True
    assert not api.operation_lock.locked()
