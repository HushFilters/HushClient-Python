import threading

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
    def fail():
        raise OSError("disk unavailable")
    job.start(fail)
    job.join()
    assert not job.snapshot()["active"]
    assert job.snapshot()["inventory"] == {"filter_count": 3}
    assert job.snapshot()["error"]
    job.start(lambda: {"filter_count": 4})
    job.join()
    assert job.snapshot()["error"] is None
    assert job.snapshot()["inventory"] == {"filter_count": 4}


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
