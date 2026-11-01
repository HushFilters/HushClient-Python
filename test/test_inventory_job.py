import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from fastapi.testclient import TestClient

import api
from filter_sync.inventory_job import InventoryJob


def test_refresh_returns_immediately_and_coalesces_requests(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    job = InventoryJob()
    def scan(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return {"filter_count": 12, "total_bytes": 48, "confirmation_token": "test"}
    monkeypatch.setattr(api, "cleanup_inventory_job", job)
    monkeypatch.setattr(api, "filter_cleanup", object())
    monkeypatch.setattr(api, "filter_manager", type("Manager", (), {"filters": []})())
    monkeypatch.setattr(api, "_inspect_or_delete_superseded_filters", scan)
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    client = TestClient(api.app)
    try:
        response = client.post("/sync/cleanup/files/refresh")
        assert response.status_code == 202 and response.json()["active"]
        assert entered.wait(1)
        assert client.post("/sync/cleanup/files/refresh").json()["active"]
        assert client.get("/sync/cleanup/files/status").json()["active"]
        assert client.get("/health").status_code == 200
        assert len(calls) == 1
    finally:
        release.set()
        job.join()
    assert client.get("/sync/cleanup/files/status").json()["inventory"]["filter_count"] == 12


def test_failed_refresh_keeps_last_inventory_and_can_retry():
    job = InventoryJob()
    job.start(lambda: {"filter_count": 3})
    job.join()
    checked_at = job.snapshot()["checked_at"]
    assert datetime.fromisoformat(checked_at).tzinfo is not None
    def fail():
        raise OSError("disk unavailable")
    job.start(fail)
    job.join()
    assert not job.snapshot()["active"]
    assert job.snapshot()["inventory"] == {"filter_count": 3}
    assert job.snapshot()["error"]
    assert job.snapshot()["checked_at"] == checked_at
    job.start(lambda: {"filter_count": 4})
    job.join()
    assert job.snapshot()["error"] is None
    assert job.snapshot()["inventory"] == {"filter_count": 4}


def test_operation_refresh_during_scan_requests_a_followup():
    entered, release = threading.Event(), threading.Event()
    job = InventoryJob()
    calls = []
    def scan():
        entered.set()
        assert release.wait(5)
        return {"filter_count": 1}
    def rescan():
        calls.append("rescan")
        return {"filter_count": 2}
    try:
        job.start(scan)
        assert entered.wait(1)
        job.start(rescan, rerun=True)
        job.start(rescan, rerun=True)
    finally:
        release.set()
        job.join()
    assert calls == ["rescan"]
    assert job.snapshot()["inventory"]["filter_count"] == 2
    assert not job.snapshot()["active"]


def test_startup_scan_waits_for_current_operation(monkeypatch, tmp_path):
    job = InventoryJob()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "filter_manager", None)
    monkeypatch.setattr(api, "filter_cleanup", None)
    with api.operation_lock:
        job.start(api._initialize_filter_cleanup)
        assert job.snapshot()["active"]
        assert api.filter_cleanup is None
    job.join()
    assert job.snapshot()["error"] is None
    assert job.snapshot()["inventory"]["filter_count"] == 0


def test_cached_status_does_not_scan(monkeypatch):
    job = InventoryJob()
    job.start(lambda: {"filter_count": 3})
    job.join()
    monkeypatch.setattr(api, "cleanup_inventory_job", job)
    def unexpected_scan():
        raise AssertionError("Cached status must not start discovery")
    monkeypatch.setattr(api, "_scan_cleanup_inventory", unexpected_scan)
    client = TestClient(api.app)
    for _ in range(3):
        assert client.get("/sync/cleanup/files/status").json() == job.snapshot()


def test_requested_scan_waits_for_sync(monkeypatch, tmp_path):
    job = InventoryJob()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "cleanup_inventory_job", job)
    monkeypatch.setattr(api, "filter_manager", None)
    monkeypatch.setattr(api, "filter_cleanup", None)
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    client = TestClient(api.app)
    with api.operation_lock:
        assert client.post("/sync/cleanup/files/refresh").status_code == 202
        status = client.get("/sync/cleanup/files/status").json()
        assert status["active"] and status["error"] is None
        assert api.filter_cleanup is None
    job.join()
    assert job.snapshot()["error"] is None
    assert job.snapshot()["checked_at"]


@pytest.mark.parametrize("endpoint,runner,response", [
    ("/sync/filters", "_run_filter_sync_with_logs", api.SyncFiltersResponse(success=True)),
    ("/sync/manifest", "_run_manifest_update_with_logs", api.ManifestUpdateResponse(success=True)),
    ("/sync/reload", "_run_filter_reload_with_logs", api.ReloadFiltersResponse(success=True)),
    ("/sync/apply", "_run_sync_apply_with_logs", api.SyncApplyResponse(success=True)),
])
def test_operations_wait_for_scan_then_rescan(monkeypatch, tmp_path, endpoint, runner, response):
    job = InventoryJob()
    entered, release, ran, rescanned = (threading.Event() for _ in range(4))
    monkeypatch.setattr(api, "cleanup_inventory_job", job)
    monkeypatch.setattr(api, "filter_manager", type("Manager", (), {"filters": []})())
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "history.json"))
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    def initial_scan():
        with api.operation_lock:
            entered.set()
            assert release.wait(5)
        return {"filter_count": 1}
    def operation():
        assert api.operation_lock.locked()
        ran.set()
        return response
    def scan():
        with api.operation_lock:
            assert ran.is_set()
            rescanned.set()
        return {"filter_count": 2}
    monkeypatch.setattr(api, runner, operation)
    monkeypatch.setattr(api, "_scan_cleanup_inventory", scan)
    client = TestClient(api.app)
    try:
        job.start(initial_scan)
        assert entered.wait(1)
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(client.post, endpoint)
            try:
                assert not ran.wait(0.1)
                assert client.get("/health").status_code == 200
                assert not pending.done()
            finally:
                release.set()
            assert pending.result(timeout=5).status_code in (200, 202)
        assert rescanned.wait(5)
    finally:
        release.set()
        job.join()
    assert job.snapshot()["inventory"]["filter_count"] == 2


def test_startup_health_does_not_wait_for_cleanup_initialization(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Manager:
        filters = []
        def __init__(self, **kwargs):
            pass
        def close(self):
            pass
    def initialize():
        entered.set()
        assert release.wait(5)
        return {"filter_count": 0, "total_bytes": 0}
    monkeypatch.setattr(api, "FilterManager", Manager)
    monkeypatch.setattr(api, "_initialize_filter_cleanup", initialize)
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "auto.json"))
    monkeypatch.setenv("HUSHCLIENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AUTO_UPDATE_FILTERS", "0")
    monkeypatch.delenv("HUSHFILTER_TEST_MODE", raising=False)
    with TestClient(api.app) as client:
        try:
            assert entered.wait(1)
            assert client.get("/health").status_code == 200
            assert client.get("/sync/cleanup/files/status").json()["active"]
        finally:
            release.set()
