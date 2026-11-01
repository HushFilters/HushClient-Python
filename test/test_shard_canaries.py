"""Full-suite coverage, manifest validation, and same-range migration."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

import api
from core.filter_core import FilterManager
from filter_sync import activation
from filter_sync.canaries import LEGACY_ID, LEGACY_CREDENTIALS, SHARD_ID, manifest_test_sets
from filter_sync.sync import FilterManifest, SyncError
from helpers.generate_manifest import generate_manifest
from test.test_activation import (SHARD_TEST_SET, SHARD_CANARIES, group_bytes,
    install_group, remote_group, run_apply, canary_metadata as metadata_for,
    installation, public_key)


def test_full_suite_has_unchanged_legacy_credentials_and_known_digests():
    assert SHARD_CANARIES[:4] == LEGACY_CREDENTIALS
    assert len(SHARD_CANARIES) == 256
    assert {c["shard"] for c in SHARD_TEST_SET["credentials"]} == {f"{i:02x}" for i in range(256)}
    for entry in SHARD_TEST_SET["credentials"]:
        assert hashlib.sha256((entry["username"] + "nWebbed" + entry["password"]).encode()).hexdigest() == entry["sha256"]


def test_every_individual_shard_is_checked(public_key, tmp_path):
    files = group_bytes(public_key, "20010101", "20260101", canaries=SHARD_CANARIES)
    directory = install_group(tmp_path, "20010101", "20260101", files)
    payload = {"filters": sorted(str(p) for p in directory.glob("*.hf")),
               "source_manifest": {"current_filter_locations": ["202601/20010101_20260101"],
                                   **metadata_for("202601/20010101_20260101")}}
    config = tmp_path / "manifest.json"
    config.write_text(json.dumps(payload))
    payload = activation.verify_manual_manifest(payload, lambda *a, **k: None)
    manager = FilterManager(str(config))
    log = []
    try:
        assert activation.validate_groups(manager, payload, lambda *a, **k: None, log.append) == 1
        assert "256/256" in log[-1]
        # Each of the 256 files must independently be able to veto activation.
        for raw, bloom in manager.filters:
            original = bloom.check
            bloom.check = lambda *a: False
            try:
                with pytest.raises(SyncError, match=f"shard {Path(raw).name[:2]}"):
                    activation.validate_groups(manager, payload, lambda *a, **k: None, lambda line: None)
            finally:
                bloom.check = original
    finally:
        manager.close()


def test_same_historic_range_rebuild_upgrades_checks_and_survives_reload(monkeypatch, installation, public_key):
    files = group_bytes(public_key, "20260101", "20260901", version=2, canaries=SHARD_CANARIES)
    remote = remote_group("20260101", "20260901", files,
                          metadata=metadata_for("202609/20260101_20260901"))
    result = run_apply(monkeypatch, installation, remote)
    assert result.success, result.detail
    assert remote.file_requests  # New content replaces the same range.
    assert any("256/256" in line for line in result.logs)
    payload = json.loads(activation.active_path().read_text())
    assert payload["source_manifest"]["current_filter_tests"]["202609/20260101_20260901"] == SHARD_ID
    reloaded = api._run_filter_reload_with_logs()
    assert reloaded.success, reloaded.detail
    assert any("256/256" in line for line in reloaded.logs)


def test_new_suite_rejects_legacy_only_content_without_replacing_active(monkeypatch, installation, public_key):
    _, previous, _, _ = installation
    result = run_apply(monkeypatch, installation, remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001"), metadata=metadata_for("202610/20260101_20261001")))
    assert not result.success and "Test credential" in result.detail
    assert api.filter_manager is previous


def test_mixed_legacy_and_full_groups_use_their_own_suite(monkeypatch, installation, public_key):
    remote = remote_group("20260101", "20261001",
        group_bytes(public_key, "20260101", "20261001", canaries=SHARD_CANARIES),
        metadata=metadata_for("202610/20260101_20261001"))
    # Keep the existing legacy group selected alongside the full-suite group.
    payload = json.loads(remote._manifest_payload)
    old_files = group_bytes(public_key, "20260101", "20260901")
    old = json.loads(remote_group("20260101", "20260901", old_files)._manifest_payload)
    for key in ("current_filter_locations", "current_filter_zips", "current_filter_files"):
        payload[key].extend(old[key])
    legacy_entries = []
    for username, password in LEGACY_CREDENTIALS:
        digest = hashlib.sha256((username + "nWebbed" + password).encode()).hexdigest()
        legacy_entries.append(dict(username=username, password=password, sha256=digest, shard=digest[:2]))
    payload["test_sets"][LEGACY_ID] = {"id": LEGACY_ID, "credentials": legacy_entries}
    payload["current_filter_tests"]["202609/20260101_20260901"] = LEGACY_ID
    remote._manifest_payload = json.dumps(payload)
    result = run_apply(monkeypatch, installation, remote)
    assert result.success, result.detail
    assert any("4/4" in line for line in result.logs)
    assert any("256/256" in line for line in result.logs)


@pytest.mark.parametrize("fault", ["empty", "duplicate", "hash", "shard", "missing_reference", "unknown_id", "null"])
def test_malformed_test_metadata_cannot_downgrade_or_skip_checks(fault):
    source = {"current_filter_locations": ["202601/20010101_20260101"],
              **copy.deepcopy(metadata_for("202601/20010101_20260101"))}
    entries = source["test_sets"][SHARD_ID]["credentials"]
    if fault == "empty": entries.clear()
    elif fault == "duplicate": entries[-1] = entries[-2]
    elif fault == "hash": entries[4]["sha256"] = "0" * 64
    elif fault == "shard": entries[4]["shard"] = "wrong"
    elif fault == "missing_reference": source["current_filter_tests"].clear()
    elif fault == "unknown_id": source["current_filter_tests"]["202601/20010101_20260101"] = "unknown"
    else: source["test_sets"] = None
    with pytest.raises(ValueError):
        manifest_test_sets(source)
    with pytest.raises(SyncError):
        FilterManifest.from_json(json.dumps(source))


def test_local_manifest_generation_preserves_per_range_test_metadata(tmp_path):
    filters = tmp_path / "filters"
    directory = filters / "202601" / "20010101_20260101"
    directory.mkdir(parents=True)
    name = "00_20010101_20260101.hf"
    (directory / name).write_bytes(b"fixture")
    source = {"current_filter_locations": ["202601/20010101_20260101"],
              "current_filter_files": [{"path": f"20010101_20260101/{name}", "md5": "unused"}],
              **metadata_for("202601/20010101_20260101")}
    (filters / "manifest_current.json").write_text(json.dumps(source))
    output = tmp_path / "manifest.json"
    assert generate_manifest(str(filters), str(output)) == 0
    assert json.loads(output.read_text())["source_manifest"] == source
