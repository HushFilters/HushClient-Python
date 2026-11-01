"""Activation tests use small local filters; never contact or reload a live service."""
import hashlib
import json
import struct
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import api
from core.filter_core import FilterManager
from core.hash import bloom_probe_hashes
from filter_sync import activation
from filter_sync.cleanup import CleanupSettings, FilterCleanup
from filter_sync.sync import sync_filters
from test.test_filter_sync import FakeDownloader, _make_zip_bytes


@pytest.fixture(scope="module")
def public_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def group_bytes(public_key, start, end, *, omit_canary=None, version=1):
    result = {}
    for index in range(256):
        bits = bytearray(128)
        for number, (username, password) in enumerate(activation.CANARIES, start=1):
            digest = hashlib.sha256((username.lower() + "nWebbed" + password).encode()).digest()
            if digest[0] == index and number != omit_canary:
                for probe in bloom_probe_hashes(digest, nk=3):
                    pos = probe % 1024
                    bits[pos // 8] |= 1 << (pos % 8)
        offset = 30 + 10 + 2 + len(public_key) + 6 + 256
        header = struct.pack(">4sHQBBIQH", b"HUSH", 1, version, 1, 1, offset, len(bits), 10)
        header += struct.pack(">QBBH", 1024, 1, 3, len(public_key)) + public_key
        header += struct.pack(">BBHH", 1, 16, 256, 1) + bytes(256)
        result[f"{index:02x}_{start}_{end}.hf"] = header + bits
    return result


def install_group(root, start, end, files):
    directory = root / end[:6] / f"{start}_{end}"
    directory.mkdir(parents=True)
    for name, contents in files.items():
        (directory / name).write_bytes(contents)
    return directory


def remote_group(start, end, files):
    build = f"{start}_{end}"
    location = f"{end[:6]}/{build}"
    zipped = _make_zip_bytes(files)
    payload = json.dumps({
        "current_filter_locations": [location],
        "current_filter_zips": [{"path": f"{location}/{build}.zip", "md5": hashlib.md5(zipped).hexdigest()}],
        "current_filter_files": [{"path": f"{build}/{name}", "md5": hashlib.md5(data).hexdigest()}
                                 for name, data in files.items()],
    })
    return FakeDownloader(payload, {f"filters/{location}/{build}.zip": zipped})


@pytest.fixture
def installation(tmp_path, monkeypatch, public_key):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HUSHCLIENT_ACTIVE_MANIFEST_PATH", "filters/.active_manifest.json")
    monkeypatch.setenv("HUSHCLIENT_PENDING_MANIFEST_PATH", "filters/.pending_manifest.json")
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    old_files = group_bytes(public_key, "20260101", "20260901")
    directory = install_group(Path("filters"), "20260101", "20260901", old_files)
    paths = sorted(str(path) for path in directory.glob("*.hf"))
    Path("manifest.json").write_text(json.dumps({"version": "1.0", "filters": paths}))
    manager = FilterManager("manifest.json")
    monkeypatch.setattr(api, "filter_manager", manager)
    cleanup = FilterCleanup(Path("filters"), Path("filters/.cleanup_state.json"))
    cleanup.loaded(paths, now=0)
    monkeypatch.setattr(api, "filter_cleanup", cleanup)
    yield tmp_path, manager, directory, cleanup
    api.filter_manager.close()
    if api.filter_manager is not manager:
        manager.close()


def run_apply(monkeypatch, installation, downloader):
    root, _, _, _ = installation
    monkeypatch.setattr(api, "sync_filters", lambda **kwargs: sync_filters(
        base_dir=root / "filter_sync", downloader=downloader, **kwargs))
    api.sync_operation_logs.start("sync_apply")
    try:
        with api.operation_lock:
            return api._run_sync_apply_with_logs()
    finally:
        api.sync_operation_logs.finish()


def test_full_apply_checks_four_canaries_and_commits_generation(monkeypatch, installation, public_key):
    _, previous, directory, cleanup = installation
    legacy_manifest = Path("manifest.json").read_bytes()
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001")))
    assert result.success, result.detail
    assert api.filter_manager is not previous
    assert all(bloom.mm is None for _, bloom in previous.filters)
    assert all(api._filter_call("check", username, password).found for username, password in activation.CANARIES)
    assert len(api.filter_manager.filters) == 256
    assert all(".generations" in path for path, _ in api.filter_manager.filters)
    assert any("all four test credentials passed in every group" in line for line in result.logs)
    assert any("retained group" in line and "all four" in line for line in result.logs)
    assert len(cleanup.retired) == 256 and all(path.exists() for path in directory.glob("*.hf"))
    assert Path(api._current_filter_configuration()) == activation.active_path()
    assert not activation.pending_path().exists()
    assert Path("manifest.json").read_bytes() == legacy_manifest
    # Reconstructing the manager after a restart uses the committed generation.
    restarted = FilterManager(api._current_filter_configuration())
    try:
        assert len(restarted.filters) == 256
        assert restarted.check(*activation.CANARIES[3]).found
    finally:
        restarted.close()


@pytest.mark.parametrize("number", [1, 2, 3, 4])
def test_any_failed_canary_preserves_active_manifest_and_cleanup(monkeypatch, installation, public_key, number):
    _, previous, _, cleanup = installation
    state = cleanup.state_path.read_bytes()
    legacy = Path("manifest.json").read_bytes()
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001", omit_canary=number)))
    assert not result.success
    assert f"testusername{number}@nwebbed.com" in result.detail
    assert api.filter_manager is previous
    assert not activation.active_path().exists()
    assert Path("manifest.json").read_bytes() == legacy
    assert cleanup.state_path.read_bytes() == state
    assert all(bloom.mm is not None for _, bloom in previous.filters)
    assert previous.check(*activation.CANARIES[0]).found


def test_incomplete_group_cannot_activate(monkeypatch, installation, public_key):
    _, previous, _, _ = installation
    files = group_bytes(public_key, "20260101", "20261001")
    files.pop("ff_20260101_20261001.hf")
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001", files))
    assert not result.success and "Incomplete shard group" in result.detail
    assert api.filter_manager is previous


def test_same_range_replacement_never_overwrites_active_files(monkeypatch, installation, public_key):
    _, previous, directory, _ = installation
    before = {path.name: path.read_bytes() for path in directory.glob("*.hf")}
    downloader = remote_group("20260101", "20260901", group_bytes(public_key, "20260101", "20260901", version=2))
    download = downloader.download_file
    def during_download(key, destination):
        assert api.filter_manager is previous
        assert previous.check(*activation.CANARIES[0]).found
        assert all(path.read_bytes() == before[path.name] for path in directory.glob("*.hf"))
        download(key, destination)
    downloader.download_file = during_download
    result = run_apply(monkeypatch, installation, downloader)
    assert result.success, result.detail
    assert downloader.file_requests
    assert all(path.read_bytes() == before[path.name] for path in directory.glob("*.hf"))


def test_verified_current_group_is_reused_without_copy_or_download(monkeypatch, installation, public_key):
    _, _, directory, _ = installation
    downloader = remote_group("20260101", "20260901", group_bytes(public_key, "20260101", "20260901"))
    result = run_apply(monkeypatch, installation, downloader)
    assert result.success, result.detail
    assert downloader.file_requests == []
    assert all(Path(path).parent == directory for path, _ in api.filter_manager.filters)


def test_opt_in_reuses_md5_across_full_apply_without_rehashing_at_reload(monkeypatch, installation, public_key):
    from filter_sync import sync as sync_module
    from filter_sync.settings import SyncSettings, save_settings

    def downloader():
        return remote_group("20260101", "20260901", group_bytes(public_key, "20260101", "20260901"))

    first = run_apply(monkeypatch, installation, downloader())
    assert first.success, first.detail
    save_settings(SyncSettings(reuse_verified_md5=True))

    def unexpected_hash(path):
        pytest.fail(f"Unchanged file was rehashed: {path}")

    monkeypatch.setattr(sync_module, "calculate_md5", unexpected_hash)
    monkeypatch.setattr(activation, "calculate_md5", unexpected_hash)
    remote = downloader()
    second = run_apply(monkeypatch, installation, remote)
    assert second.success, second.detail
    assert not remote.file_requests
    assert len(api.filter_manager.filters) == 256


def test_publish_failure_keeps_old_reader_and_manifest(monkeypatch, installation, public_key):
    _, previous, _, cleanup = installation
    original = activation.atomic_json
    def failed_write(path, payload):
        if path == activation.active_path():
            raise OSError("disk full")
        return original(path, payload)
    monkeypatch.setattr(activation, "atomic_json", failed_write)
    state = cleanup.state_path.read_bytes()
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001")))
    assert not result.success and "disk full" in result.detail
    assert api.filter_manager is previous and not activation.active_path().exists()
    assert cleanup.state_path.read_bytes() == state


def test_swap_waits_for_batch_reader_before_close_and_cleanup(monkeypatch, tmp_path):
    entered, release, swapped = threading.Event(), threading.Event(), threading.Event()
    class Previous:
        closed = False
        def check_batch(self, values):
            entered.set()
            assert release.wait(5)
            assert not self.closed
            return values
        def close(self):
            self.closed = True
    class Candidate:
        def check(self):
            return "new manager"
    previous, candidate = Previous(), Candidate()
    monkeypatch.setattr(api, "filter_manager", previous)
    monkeypatch.setattr(activation, "atomic_json", lambda *args: None)
    reader = threading.Thread(target=lambda: api._filter_call("check_batch", ["batch"]))
    def swap():
        api._publish_filter_manager(candidate, {"filters": []})
        swapped.set()
    worker = threading.Thread(target=swap)
    reader.start()
    assert entered.wait(1)
    worker.start()
    assert not swapped.wait(.05)
    assert not previous.closed
    assert api._filter_call("check") == "new manager"
    release.set()
    reader.join(2)
    worker.join(2)
    assert swapped.is_set() and previous.closed
    assert api.filter_manager is candidate and api.manager_readers == {}


def test_each_group_is_validated_even_when_another_group_matches(monkeypatch, installation, public_key):
    root, previous, directory, cleanup = installation
    # The older selected group fails credential 3; the incoming group has all four.
    old_files = group_bytes(public_key, "20260101", "20260901", omit_canary=3)
    previous.close()  # Prepare this fixture before exposing it to requests (Windows mmap safety).
    for name, contents in old_files.items():
        (directory / name).write_bytes(contents)
    previous = FilterManager("manifest.json")
    monkeypatch.setattr(api, "filter_manager", previous)
    installation = (root, previous, directory, cleanup)
    older = remote_group("20260101", "20260901", old_files)
    newer = remote_group("20261001", "20261008", group_bytes(public_key, "20261001", "20261008"))
    manifests = [json.loads(downloader._manifest_payload) for downloader in (older, newer)]
    payload = {key: manifests[0][key] + manifests[1][key] for key in manifests[0]}
    downloader = FakeDownloader(json.dumps(payload), {**older._objects, **newer._objects})
    result = run_apply(monkeypatch, installation, downloader)
    assert not result.success
    assert "testusername3@nwebbed.com" in result.detail and "20260101_20260901" in result.detail
    assert api.filter_manager is previous


def test_changed_staged_file_cannot_be_activated_later(monkeypatch, installation, public_key):
    root, previous, _, cleanup = installation
    downloader = remote_group("20260101", "20261001", group_bytes(public_key, "20260101", "20261001"))
    monkeypatch.setattr(api, "sync_filters", lambda **kwargs: sync_filters(
        base_dir=root / "filter_sync", downloader=downloader, **kwargs))
    state = cleanup.state_path.read_bytes()
    assert api._run_filter_sync_with_logs().success
    assert api.filter_manager is previous and not activation.active_path().exists()
    assert api._run_manifest_update_with_logs().success
    pending = json.loads(activation.pending_path().read_text())
    path = Path(pending["filters"][-1])
    path.write_bytes(path.read_bytes() + b"changed")
    result = api._run_filter_reload_with_logs()
    assert not result.success and "changed after verification" in result.detail
    assert api.filter_manager is previous
    assert cleanup.state_path.read_bytes() == state


def test_retained_broken_group_is_diagnostic_and_does_not_block_repair(monkeypatch, installation, public_key):
    install_group(Path("filters"), "20260401", "20260408",
        group_bytes(public_key, "20260401", "20260408", omit_canary=4))
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001")))
    assert result.success, result.detail
    assert any("WARNING retained group" in line and "testusername4" in line for line in result.logs)


def test_checksum_failure_leaves_live_files_and_cleanup_unchanged(monkeypatch, installation, public_key):
    _, previous, directory, cleanup = installation
    downloader = remote_group("20260101", "20261001", group_bytes(public_key, "20260101", "20261001"))
    key = next(iter(downloader._objects))
    downloader._objects[key] = b"not the expected zip"
    state = cleanup.state_path.read_bytes()
    result = run_apply(monkeypatch, installation, downloader)
    assert not result.success and "MD5 mismatch" in result.detail
    assert api.filter_manager is previous and previous.check(*activation.CANARIES[0]).found
    assert len(list(directory.glob("*.hf"))) == 256
    assert cleanup.state_path.read_bytes() == state
    assert not activation.pending_path().exists()


def test_bad_shard_header_closes_candidate_and_keeps_old_manager(monkeypatch, installation, public_key):
    _, previous, _, _ = installation
    files = group_bytes(public_key, "20260101", "20261001")
    files["ff_20260101_20261001.hf"] = b"not a valid HushFilter"
    created = []
    def manager(**kwargs):
        candidate = FilterManager(**kwargs)
        created.append(candidate)
        return candidate
    monkeypatch.setattr(api, "FilterManager", manager)
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001", files))
    assert not result.success and "Some configured filters" in result.detail
    assert api.filter_manager is previous
    assert created and all(bloom.mm is None for _, bloom in created[0].filters)


def test_second_failed_activation_preserves_committed_generation(monkeypatch, installation, public_key):
    first = run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001")))
    assert first.success
    previous = api.filter_manager
    active = activation.active_path().read_bytes()
    state = api.filter_cleanup.state_path.read_bytes()
    second = run_apply(monkeypatch, installation, remote_group("20260101", "20261101",
        group_bytes(public_key, "20260101", "20261101", omit_canary=2)))
    assert not second.success
    assert api.filter_manager is previous
    assert activation.active_path().read_bytes() == active
    assert api.filter_cleanup.state_path.read_bytes() == state
    assert api._filter_call("check", *activation.CANARIES[1]).found


def test_immediate_cleanup_waits_for_successful_activation(monkeypatch, installation, public_key):
    _, _, directory, cleanup = installation
    cleanup.configure(CleanupSettings(retention_days=0))
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001")))
    assert result.success
    assert not list(directory.glob("*.hf"))
    assert all(Path(path).exists() for path, _ in api.filter_manager.filters)


def test_pending_generation_is_protected_from_manual_cleanup(monkeypatch, installation, public_key):
    root, previous, _, _ = installation
    downloader = remote_group("20260101", "20260901", group_bytes(public_key, "20260101", "20260901", version=2))
    monkeypatch.setattr(api, "sync_filters", lambda **kwargs: sync_filters(
        base_dir=root / "filter_sync", downloader=downloader, **kwargs))
    assert api._run_filter_sync_with_logs().success
    pending = json.loads(activation.pending_path().read_text())
    inventory = api._inspect_or_delete_superseded_filters()
    assert inventory["filter_count"] == 0
    assert api.filter_manager is previous
    assert all(Path(path).exists() for path in pending["filters"])


def test_later_activation_retires_previous_generation_files(monkeypatch, installation, public_key):
    first = run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001")))
    assert first.success
    previous_generation = [Path(path) for path, _ in api.filter_manager.filters]
    api.filter_cleanup.configure(CleanupSettings(retention_days=0))
    second = run_apply(monkeypatch, installation, remote_group("20260101", "20261101",
        group_bytes(public_key, "20260101", "20261101")))
    assert second.success, second.detail
    assert all(not path.exists() for path in previous_generation)
    assert all(Path(path).exists() for path, _ in api.filter_manager.filters)


def test_standalone_reload_revalidates_active_manifest(monkeypatch, installation, public_key):
    assert run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001"))).success
    previous = api.filter_manager
    assert not activation.pending_path().exists()
    result = api._run_filter_reload_with_logs()
    assert result.success, result.detail
    assert api.filter_manager is not previous
    assert api.filter_manager.check(*activation.CANARIES[3]).found


def test_test_mode_reload_cannot_publish_a_production_manifest(monkeypatch, installation):
    _, previous, _, _ = installation
    Path("test_manifest.json").write_bytes(Path("manifest.json").read_bytes())
    monkeypatch.setenv("HUSHFILTER_TEST_MODE", "1")
    result = api._run_filter_reload_with_logs()
    assert result.success, result.detail
    assert api.filter_manager is not previous
    assert not activation.active_path().exists()


@pytest.mark.parametrize("parameter", ["nbits", "nk", "bounds"])
def test_invalid_bloom_parameters_are_rejected_before_canary_checks(monkeypatch, installation, public_key, parameter):
    files = group_bytes(public_key, "20260101", "20261001")
    name = "ff_20260101_20261001.hf"
    data = bytearray(files[name])
    if parameter == "nbits":
        struct.pack_into(">Q", data, 30, 0)
    elif parameter == "nk":
        data[39] = 0
    else:
        struct.pack_into(">I", data, 16, 0)
    files[name] = bytes(data)
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001", files))
    assert not result.success and "Some configured filters" in result.detail


def test_cli_default_follows_active_manifest_but_explicit_manifest_is_respected(monkeypatch, tmp_path):
    import hush
    activation.active_path().write_text('{"filters": []}')
    selected = []
    monkeypatch.setattr(hush, "run_manifest_checks", lambda args: selected.append(args.manifest) or 0)
    for options in ([], ["-m", "custom.json"], ["--test"]):
        monkeypatch.setattr("sys.argv", ["hush.py", "-u", "test", *options])
        assert hush.main() == 0
    assert selected == [str(activation.active_path()), "custom.json", "test_manifest.json"]


def test_failed_reader_releases_its_manager_lease(monkeypatch):
    class Previous:
        closed = False
        def check(self):
            raise ValueError("lookup failed")
        def close(self):
            self.closed = True
    previous = Previous()
    monkeypatch.setattr(api, "filter_manager", previous)
    monkeypatch.setattr(activation, "atomic_json", lambda *args: None)
    with pytest.raises(ValueError):
        api._filter_call("check")
    assert not api.manager_readers
    api._publish_filter_manager(object(), {"filters": []})
    assert previous.closed


def test_atomic_publish_error_preserves_existing_manifest(monkeypatch):
    activation.atomic_json(activation.active_path(), {"filters": ["previous.hf"]})
    before = activation.active_path().read_bytes()
    def fail_replace(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(activation.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        activation.atomic_json(activation.active_path(), {"filters": ["candidate.hf"]})
    assert activation.active_path().read_bytes() == before
    assert not list(activation.active_path().parent.glob(".active.json.*.tmp"))
