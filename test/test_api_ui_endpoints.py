from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api
from filter_sync.sync import SyncResult


class DummyCheckResult:
    def __init__(self, found: bool = False, matching_filters: list[str] | None = None):
        self.found = found
        self.matching_filters = matching_filters or []


class DummyFilterManager:
    def __init__(self, manifest_path: str | None = None):
        self.filters = []
        self.manifest_path = manifest_path

    def close(self) -> None:
        return None

    def get_stats(self) -> dict:
        return {"filter_count": 0, "filters": [], "max_nk": 0}

    def check(self, username: str, password: str) -> DummyCheckResult:
        return DummyCheckResult(found=bool(username and password), matching_filters=["filters/test.hf"])

    def check_batch(self, credentials: list[tuple[str, str]]) -> list[DummyCheckResult]:
        return [self.check(username, password) for username, password in credentials]

    def check_sha256_hash(self, hash_value: str) -> DummyCheckResult:
        return DummyCheckResult(found=bool(hash_value), matching_filters=["filters/test.hf"])

    def check_sha256_batch(self, hash_values: list[str]) -> list[DummyCheckResult]:
        return [self.check_sha256_hash(hash_value) for hash_value in hash_values]


def test_root_and_ui_endpoints(monkeypatch) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)

    with TestClient(api.app) as client:
        root_response = client.get("/")
        assert root_response.status_code == 200
        assert root_response.json()["test_mode"] is False
        assert root_response.json()["endpoints"]["ui_check"] == "/ui-check"
        assert root_response.json()["endpoints"]["ui_sync"] == "/ui-sync"
        assert root_response.json()["endpoints"]["sync_filters"] == "/sync/filters"
        assert root_response.json()["endpoints"]["sync_apply"] == "/sync/apply"
        assert root_response.json()["endpoints"]["sync_status"] == "/sync/status"
        assert root_response.json()["endpoints"]["auto_update"] == "/sync/auto-update"
        assert root_response.json()["endpoints"]["update_manifest"] == "/sync/manifest"
        assert root_response.json()["endpoints"]["reload_filters"] == "/sync/reload"
        assert "ui" not in root_response.json()["endpoints"]

        check_response = client.get("/ui-check/")
        assert check_response.status_code == 200
        assert "Breached Credential Check" in check_response.text
        assert "TEST MODE" not in check_response.text
        assert "{{TEST_MODE_BANNER}}" not in check_response.text

        sync_response = client.get("/ui-sync/")
        assert sync_response.status_code == 200
        assert "sync, update manifest, and reload filters" in sync_response.text
        assert "/ui-sync/app.js?v=20260903a" in sync_response.text
        assert "Daily Auto-Update" in sync_response.text
        assert "Recent automatic updates" in sync_response.text
        assert "sync filters from nWebbed" in sync_response.text
        assert "update manifest" in sync_response.text
        assert "reload with new filters" in sync_response.text
        assert "TEST MODE" not in sync_response.text
        assert "{{TEST_MODE_BANNER}}" not in sync_response.text


def test_test_mode_is_exposed_in_api_responses_and_ui(monkeypatch) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None
    monkeypatch.setenv("HUSHFILTER_TEST_MODE", "1")

    with TestClient(api.app) as client:
        root_response = client.get("/")
        assert root_response.status_code == 200
        assert root_response.json()["test_mode"] is True

        health_response = client.get("/health")
        assert health_response.status_code == 200
        assert health_response.json()["test_mode"] is True

        stats_response = client.get("/stats")
        assert stats_response.status_code == 200
        assert stats_response.json()["test_mode"] is True

        checkhash_response = client.post("/checkhash", json={"hash": "a" * 64})
        assert checkhash_response.status_code == 200
        assert checkhash_response.json()["test_mode"] is True

        validation_response = client.get("/check")
        assert validation_response.status_code == 422
        assert validation_response.json()["test_mode"] is True

        check_page_response = client.get("/ui-check/")
        assert check_page_response.status_code == 200
        assert "TEST MODE" in check_page_response.text

        sync_page_response = client.get("/ui-sync/")
        assert sync_page_response.status_code == 200
        assert "TEST MODE" in sync_page_response.text


def test_sync_filters_endpoint_returns_logs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None

    def fake_sync_filters() -> SyncResult:
        logging.getLogger("filter_sync.sync").info("starting filter md5 verification")
        logging.getLogger("filter_sync.sync").info(
            "local filter verification zip=20260401_20260408.zip 5/10 complete - pass"
        )
        logging.getLogger("filter_sync.sync").info("finished filter md5 verification")
        return SyncResult(
            manifest_path=tmp_path / "filters" / "manifest_current.json",
            filters_dir=tmp_path / "filters",
            downloaded=(tmp_path / "filters" / "202604" / "downloaded.zip",),
            redownloaded=(),
            verified_existing=(tmp_path / "filters" / "202604" / "existing.zip",),
        )

    monkeypatch.setattr(api, "sync_filters", fake_sync_filters)

    with TestClient(api.app) as client:
        response = client.post("/sync/filters")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["test_mode"] is False
    assert payload["downloaded"] == [str(tmp_path / "filters" / "202604" / "downloaded.zip")]
    assert payload["verified_existing"] == [str(tmp_path / "filters" / "202604" / "existing.zip")]
    assert payload["logs"] == [
        "INFO starting filter md5 verification",
        "INFO local filter verification zip=20260401_20260408.zip 5/10 complete - pass",
        "INFO finished filter md5 verification",
    ]


def test_sync_filters_endpoint_mirrors_logs_to_stdout(
    monkeypatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None

    def fake_sync_filters() -> SyncResult:
        logging.getLogger("filter_sync.sync").info("download still progressing")
        return SyncResult(
            manifest_path=tmp_path / "filters" / "manifest_current.json",
            filters_dir=tmp_path / "filters",
            downloaded=(),
            redownloaded=(),
            verified_existing=(),
        )

    monkeypatch.setattr(api, "sync_filters", fake_sync_filters)

    with TestClient(api.app) as client:
        response = client.post("/sync/filters")

    assert response.status_code == 200
    captured = capsys.readouterr()
    assert "INFO download still progressing" in captured.out


def test_sync_filters_endpoint_returns_failure_logs(monkeypatch) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None

    def fake_sync_filters() -> SyncResult:
        logging.getLogger("filter_sync.sync").error("ZIP MD5 mismatch path=filters/a.zip")
        raise api.SyncError("zip verification failed")

    monkeypatch.setattr(api, "sync_filters", fake_sync_filters)

    with TestClient(api.app) as client:
        response = client.post("/sync/filters")

    assert response.status_code == 500
    payload = response.json()
    assert payload["success"] is False
    assert payload["test_mode"] is False
    assert payload["detail"] == "zip verification failed"
    assert payload["logs"] == ["ERROR ZIP MD5 mismatch path=filters/a.zip"]


def test_sync_status_endpoint_reports_live_logs_during_running_sync(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None
    started = threading.Event()
    release = threading.Event()
    response_holder: dict[str, object] = {}

    def fake_sync_filters() -> SyncResult:
        logging.getLogger("filter_sync.sync").info("download started")
        started.set()
        assert release.wait(timeout=2.0)
        logging.getLogger("filter_sync.sync").info("download finished")
        return SyncResult(
            manifest_path=tmp_path / "filters" / "manifest_current.json",
            filters_dir=tmp_path / "filters",
            downloaded=(),
            redownloaded=(),
            verified_existing=(),
        )

    monkeypatch.setattr(api, "sync_filters", fake_sync_filters)

    with TestClient(api.app) as client:
        def run_request() -> None:
            response_holder["response"] = client.post("/sync/filters")

        worker = threading.Thread(target=run_request)
        worker.start()
        assert started.wait(timeout=1.0)

        live_payload = None
        deadline = time.time() + 2.0
        while time.time() < deadline:
            status_response = client.get("/sync/status")
            assert status_response.status_code == 200
            payload = status_response.json()
            if payload["active"] and "INFO download started" in payload["logs"]:
                live_payload = payload
                break
            time.sleep(0.05)

        assert live_payload is not None
        assert live_payload["operation"] == "sync_filters"
        release.set()
        worker.join(timeout=2.0)

        response = response_holder["response"]
        assert response.status_code == 200

        final_status = client.get("/sync/status")
        assert final_status.status_code == 200
        final_payload = final_status.json()
        assert final_payload["active"] is False
        assert final_payload["success"] is True
        assert final_payload["detail"] is None
        assert "INFO download finished" in final_payload["logs"]


def test_update_manifest_endpoint_returns_logs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None
    monkeypatch.chdir(tmp_path)

    def fake_generate_manifest(filters_dir: str, output_file: str) -> int:
        Path(output_file).write_text(
            json.dumps({"version": "1.0", "filters": ["filters/a.hf", "filters/b.hf"]}),
            encoding="utf-8",
        )
        print(f"Generated {output_file} with 2 filters")
        return 0

    monkeypatch.setattr(api, "generate_manifest", fake_generate_manifest)

    with TestClient(api.app) as client:
        response = client.post("/sync/manifest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["test_mode"] is False
    assert payload["output_file"] == "manifest.json"
    assert payload["filter_count"] == 2
    assert payload["logs"] == ["Generated manifest.json with 2 filters"]


def test_reload_filters_endpoint_swaps_manager(monkeypatch) -> None:
    class ReloadableFilterManager(DummyFilterManager):
        created: list["ReloadableFilterManager"] = []

        def __init__(self, manifest_path: str | None = None):
            super().__init__(manifest_path=manifest_path)
            self.closed = False
            self.filters = [("filters/new.hf", object())]
            ReloadableFilterManager.created.append(self)

        def close(self) -> None:
            self.closed = True

        def get_stats(self) -> dict:
            return {
                "filter_count": 1,
                "filters": ["filters/new.hf"],
                "max_nk": 7,
            }

    monkeypatch.setattr(api, "FilterManager", ReloadableFilterManager)
    api.filter_manager = None

    with TestClient(api.app) as client:
        startup_manager = ReloadableFilterManager.created[0]
        response = client.post("/sync/reload")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["test_mode"] is False
    assert payload["filter_count"] == 1
    assert payload["filters"] == ["filters/new.hf"]
    assert payload["logs"] == [
        "INFO loaded 1 filters from manifest.json",
        "INFO closed previous filter mappings",
    ]
    assert startup_manager.closed is True
    assert len(ReloadableFilterManager.created) >= 2


def test_sync_apply_endpoint_starts_background_sequence_and_reports_completion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None
    calls: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def fake_sync() -> api.SyncFiltersResponse:
        calls.append("sync")
        started.set()
        assert release.wait(timeout=2.0)
        return api.SyncFiltersResponse(
            success=True,
            manifest_path=str(tmp_path / "filters" / "manifest_current.json"),
            downloaded=[str(tmp_path / "filters" / "202604" / "downloaded.zip")],
            redownloaded=[],
            verified_existing=[str(tmp_path / "filters" / "202604" / "existing.zip")],
            logs=["INFO sync step complete"],
        )

    def fake_manifest() -> api.ManifestUpdateResponse:
        calls.append("manifest")
        return api.ManifestUpdateResponse(
            success=True,
            output_file="manifest.json",
            filter_count=2,
            logs=["INFO manifest step complete"],
        )

    def fake_reload() -> api.ReloadFiltersResponse:
        calls.append("reload")
        return api.ReloadFiltersResponse(
            success=True,
            filter_count=2,
            filters=["filters/a.hf", "filters/b.hf"],
            max_nk=9,
            logs=["INFO reload step complete"],
        )

    monkeypatch.setattr(api, "_run_filter_sync_with_logs", fake_sync)
    monkeypatch.setattr(api, "_run_manifest_update_with_logs", fake_manifest)
    monkeypatch.setattr(api, "_run_filter_reload_with_logs", fake_reload)

    with TestClient(api.app) as client:
        response = client.post("/sync/apply")
        assert response.status_code == 202
        payload = response.json()
        assert payload["started"] is True
        assert payload["operation"] == "sync_apply"
        assert started.wait(timeout=1.0)

        live_status = client.get("/sync/status")
        assert live_status.status_code == 200
        live_payload = live_status.json()
        assert live_payload["active"] is True
        assert live_payload["operation"] == "sync_apply"

        release.set()

        deadline = time.time() + 2.0
        final_status_payload = None
        while time.time() < deadline:
            status_response = client.get("/sync/status")
            assert status_response.status_code == 200
            candidate = status_response.json()
            if candidate["operation"] == "sync_apply" and candidate["active"] is False:
                final_status_payload = candidate
                break
            time.sleep(0.05)

    assert final_status_payload is not None
    payload = response.json()
    assert payload["started"] is True
    assert payload["test_mode"] is False
    assert calls == ["sync", "manifest", "reload"]
    assert final_status_payload["downloaded"] == [str(tmp_path / "filters" / "202604" / "downloaded.zip")]
    assert final_status_payload["verified_existing"] == [str(tmp_path / "filters" / "202604" / "existing.zip")]
    assert final_status_payload["output_file"] == "manifest.json"
    assert final_status_payload["filter_count"] == 2
    assert final_status_payload["filters"] == ["filters/a.hf", "filters/b.hf"]
    assert final_status_payload["logs"] == [
        "INFO starting filter sync, manifest update, and reload sequence",
        "INFO step 1/3: filter sync",
        "INFO sync step complete",
        "INFO step 2/3: manifest update",
        "INFO manifest step complete",
        "INFO step 3/3: reload filters",
        "INFO reload step complete",
        "INFO completed filter sync, manifest update, and reload sequence",
    ]
    assert final_status_payload["active"] is False
    assert final_status_payload["operation"] == "sync_apply"
    assert final_status_payload["success"] is True
    assert final_status_payload["detail"] is None


def test_sync_apply_endpoint_reports_failed_background_sequence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    api.filter_manager = None
    calls: list[str] = []

    def fake_sync() -> api.SyncFiltersResponse:
        calls.append("sync")
        return api.SyncFiltersResponse(
            success=True,
            manifest_path=str(tmp_path / "filters" / "manifest_current.json"),
            downloaded=[],
            redownloaded=[],
            verified_existing=[],
            logs=["INFO sync step complete"],
        )

    def fake_manifest() -> api.ManifestUpdateResponse:
        calls.append("manifest")
        return api.ManifestUpdateResponse(
            success=False,
            output_file="manifest.json",
            filter_count=0,
            logs=["ERROR manifest step failed"],
            detail="generate_manifest failed",
        )

    def fake_reload() -> api.ReloadFiltersResponse:
        calls.append("reload")
        return api.ReloadFiltersResponse(
            success=True,
            filter_count=1,
            filters=["filters/a.hf"],
            max_nk=1,
            logs=["INFO reload step complete"],
        )

    monkeypatch.setattr(api, "_run_filter_sync_with_logs", fake_sync)
    monkeypatch.setattr(api, "_run_manifest_update_with_logs", fake_manifest)
    monkeypatch.setattr(api, "_run_filter_reload_with_logs", fake_reload)

    with TestClient(api.app) as client:
        response = client.post("/sync/apply")
        assert response.status_code == 202

        deadline = time.time() + 2.0
        final_status_payload = None
        while time.time() < deadline:
            status_response = client.get("/sync/status")
            assert status_response.status_code == 200
            candidate = status_response.json()
            if candidate["operation"] == "sync_apply" and candidate["active"] is False:
                final_status_payload = candidate
                break
            time.sleep(0.05)

    assert final_status_payload is not None
    payload = response.json()
    assert payload["started"] is True
    assert payload["test_mode"] is False
    assert calls == ["sync", "manifest"]
    assert final_status_payload["logs"] == [
        "INFO starting filter sync, manifest update, and reload sequence",
        "INFO step 1/3: filter sync",
        "INFO sync step complete",
        "INFO step 2/3: manifest update",
        "ERROR manifest step failed",
        "ERROR sequence stopped during manifest update",
    ]
    assert final_status_payload["active"] is False
    assert final_status_payload["operation"] == "sync_apply"
    assert final_status_payload["success"] is False
    assert final_status_payload["detail"] == "generate_manifest failed"


def test_current_filter_configuration_uses_hushfilter_test_mode(monkeypatch) -> None:
    monkeypatch.delenv("MANIFEST_PATH", raising=False)
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    monkeypatch.delenv("HUSH_TEST_MODE", raising=False)
    monkeypatch.delenv("TEST_MODE", raising=False)

    manifest_path = api._current_filter_configuration()
    assert manifest_path == "manifest.json"

    monkeypatch.setenv("HUSHFILTER_TEST_MODE", "1")

    manifest_path = api._current_filter_configuration()
    assert manifest_path == "test_manifest.json"

    monkeypatch.setenv("HUSHFILTER_TEST_MODE", "true")

    manifest_path = api._current_filter_configuration()
    assert manifest_path == "manifest.json"


def test_auto_update_enabled_only_when_env_is_one(monkeypatch) -> None:
    monkeypatch.delenv("AUTO_UPDATE_FILTERS", raising=False)
    assert api._is_auto_update_enabled() is False

    monkeypatch.setenv("AUTO_UPDATE_FILTERS", "0")
    assert api._is_auto_update_enabled() is False

    monkeypatch.setenv("AUTO_UPDATE_FILTERS", "1")
    assert api._is_auto_update_enabled() is True


def test_configured_auto_update_hour_parses_integer_env(monkeypatch) -> None:
    monkeypatch.setenv("AUTO_UPDATE_TIME", "23")
    assert api._configured_auto_update_hour() == 23

    monkeypatch.setenv("AUTO_UPDATE_TIME", "2")
    assert api._configured_auto_update_hour() == 2


def test_configured_auto_update_hour_rejects_invalid_values(monkeypatch) -> None:
    monkeypatch.delenv("AUTO_UPDATE_TIME", raising=False)
    assert api._configured_auto_update_hour() is None

    monkeypatch.setenv("AUTO_UPDATE_TIME", "abc")
    assert api._configured_auto_update_hour() is None

    monkeypatch.setenv("AUTO_UPDATE_TIME", "-1")
    assert api._configured_auto_update_hour() is None

    monkeypatch.setenv("AUTO_UPDATE_TIME", "24")
    assert api._configured_auto_update_hour() is None


def test_seconds_until_next_auto_update_uses_next_matching_hour() -> None:
    now = datetime(2026, 4, 25, 21, 30, tzinfo=timezone.utc)
    assert api._seconds_until_next_auto_update(now, 23) == 5400


def test_seconds_until_next_auto_update_rolls_to_next_day_after_hour() -> None:
    now = datetime(2026, 4, 25, 23, 30, tzinfo=timezone.utc)
    assert api._seconds_until_next_auto_update(now, 23) == 84600


def test_run_scheduled_auto_update_executes_sync_apply(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_sync_apply() -> api.SyncApplyResponse:
        calls.append("sync_apply")
        return api.SyncApplyResponse(success=True, logs=["INFO sequence complete"])

    monkeypatch.setattr(api, "_run_sync_apply_with_logs", fake_sync_apply)
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "auto-update.json"))
    api._load_auto_update_state()
    api.sync_operation_logs.finish()

    api._run_scheduled_auto_update()

    assert calls == ["sync_apply"]
    status = api.sync_operation_logs.snapshot()
    assert status["active"] is False
    assert status["operation"] == "auto_sync_apply"
    history = api._auto_update_status_snapshot()["history"]
    assert len(history) == 1
    assert history[0]["status"] == "success"
    assert history[0]["logs"] == ["INFO sequence complete"]

    persisted = json.loads((tmp_path / "auto-update.json").read_text(encoding="utf-8"))
    assert persisted["history"][0]["status"] == "success"


def test_run_scheduled_auto_update_skips_when_operation_in_progress(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_sync_apply() -> api.SyncApplyResponse:
        calls.append("sync_apply")
        return api.SyncApplyResponse(success=True)

    monkeypatch.setattr(api, "_run_sync_apply_with_logs", fake_sync_apply)
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "auto-update.json"))
    api._load_auto_update_state()

    assert api.operation_lock.acquire(blocking=False)
    try:
        api._run_scheduled_auto_update()
    finally:
        api.operation_lock.release()

    assert calls == []
    history = api._auto_update_status_snapshot()["history"]
    assert history[0]["status"] == "skipped"


def test_auto_update_api_manages_and_persists_schedule(monkeypatch, tmp_path: Path) -> None:
    state_path = tmp_path / "auto-update.json"
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(state_path))
    monkeypatch.setenv("AUTO_UPDATE_FILTERS", "1")
    monkeypatch.setenv("AUTO_UPDATE_TIME", "2")
    api.filter_manager = None

    with TestClient(api.app) as client:
        initial_response = client.get("/sync/auto-update")
        assert initial_response.status_code == 200
        initial = initial_response.json()
        assert initial["enabled"] is True
        assert initial["hour"] == 2
        assert initial["next_update_at"] is not None
        assert initial["timezone"]

        update_response = client.put(
            "/sync/auto-update",
            json={"enabled": True, "hour": 14},
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["enabled"] is True
        assert updated["hour"] == 14
        assert updated["next_update_at"] is not None

        disable_response = client.put(
            "/sync/auto-update",
            json={"enabled": False, "hour": 14},
        )
        assert disable_response.status_code == 200
        disabled = disable_response.json()
        assert disabled["enabled"] is False
        assert disabled["next_update_at"] is None

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["enabled"] is False
    assert persisted["hour"] == 14


def test_auto_update_api_requires_hour_when_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "auto-update.json"))
    monkeypatch.delenv("AUTO_UPDATE_FILTERS", raising=False)
    monkeypatch.delenv("AUTO_UPDATE_TIME", raising=False)
    api.filter_manager = None

    with TestClient(api.app) as client:
        response = client.put(
            "/sync/auto-update",
            json={"enabled": True, "hour": None},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "hour is required when automatic updates are enabled"
