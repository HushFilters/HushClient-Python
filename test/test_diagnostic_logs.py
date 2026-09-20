import asyncio
from datetime import datetime, timedelta, timezone
import logging

import pytest
from fastapi.testclient import TestClient

import api
from diagnostic_logs import DiagnosticLogs, LogSettings


def settings(**enabled):
    return LogSettings(**{name: enabled.get(name, False) for name in LogSettings.model_fields})


def record(handler, message, level=logging.INFO, name="hushclient"):
    handler.handle(logging.LogRecord(name, level, "", 0, message, (), None))


def test_category_union_off_persistence_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("NWEBBED_API_KEY", "secret-api-key")
    handler = DiagnosticLogs(tmp_path)
    try:
        handler.configure(settings(critical=True, sync=True))
        record(handler, "ignored ordinary error", logging.ERROR)
        record(handler, "critical example", logging.CRITICAL)
        record(handler, "download example", name="filter_sync.r2_client")
        record(handler, "secret-api-key https://example.com/check?password=secret", name="filter_sync.sync")
        text = handler.snapshot()["text"]
        assert "ignored ordinary error" not in text
        assert "critical example" in text and "download example" in text
        assert "secret-api-key" not in text and "password=secret" not in text
        handler.configure(settings())
        size = handler.snapshot()["size_bytes"]
        record(handler, "ignored critical", logging.CRITICAL)
        assert handler.snapshot()["size_bytes"] == size
    finally:
        handler.close()
    reopened = DiagnosticLogs(tmp_path)
    try:
        assert reopened.settings == settings()
    finally:
        reopened.close()


def test_rotation_export_and_clear(tmp_path):
    handler = DiagnosticLogs(tmp_path)
    handler.maxBytes = 400
    handler.configure(settings(everything=True))
    try:
        for i in range(30):
            record(handler, f"Entry {i}: " + "x" * 70)
        files = [path for path in handler.files() if path.exists()]
        assert len(files) == 4
        snapshot = handler.snapshot()
        assert snapshot["size_bytes"] == sum(path.stat().st_size for path in files)
        with handler.export() as export:
            assert export.read().decode() == snapshot["text"]
        handler.clear()
        assert handler.snapshot()["size_bytes"] == 0
        assert handler.settings_path.exists()
        record(handler, "After clear")
        assert "After clear" in handler.snapshot()["text"]
    finally:
        handler.close()


@pytest.fixture
def client(monkeypatch, tmp_path):
    class Manager:
        filters = []
        def __init__(self, **kwargs): pass
        def close(self): pass
    monkeypatch.setattr(api, "FilterManager", Manager)
    monkeypatch.setenv("HUSHCLIENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "schedule.json"))
    monkeypatch.delenv("AUTO_UPDATE_FILTERS", raising=False)
    with TestClient(api.app, raise_server_exceptions=False) as client:
        yield client


def test_logs_api_sync_failure_and_machine_id(client, monkeypatch):
    monkeypatch.setattr(api, "_machine_id_from_mac", lambda: "machine-123")
    assert client.get("/sync/auto-update").json()["machine_id"] == "machine-123"
    assert client.get("/ui-logs/").status_code == 200
    response = client.put("/logs/settings", json=settings(sync=True).model_dump())
    assert response.status_code == 200

    def fail():
        raise api.R2ClientError("Download connection lost")
    monkeypatch.setattr(api, "sync_filters", fail)
    assert client.post("/sync/filters").status_code == 500
    payload = client.get("/logs").json()
    assert "Download connection lost" in payload["text"]
    assert "Traceback" in payload["text"]
    assert payload["size_bytes"] > 0
    download = client.get("/logs/download")
    assert "Download connection lost" in download.text
    assert "attachment" in download.headers["content-disposition"]
    assert client.delete("/logs").status_code == 200
    assert client.get("/logs").json()["size_bytes"] == 0


def test_request_errors_logged_without_credentials(client):
    client.put("/logs/settings", json=settings(everything=True).model_dump())
    client.post("/check", json={"username": {"secret-user": "secret-password"}})
    client.get("/check?username=private-user&password=private-password")
    text = client.get("/logs").json()["text"]
    assert "Request validation failed" in text
    assert "Unhandled request error" in text
    assert "secret-user" not in text and "secret-password" not in text
    assert "private-user" not in text and "private-password" not in text


@pytest.mark.parametrize("category", ["everything", "requests"])
def test_successful_reads_do_not_fill_logs_but_failures_remain(client, monkeypatch, category):
    client.put("/logs/settings", json=settings(**{category: True}).model_dump())
    # TestClient's HTTP client has its own logger, absent from server-side polling.
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", True)
    monkeypatch.setattr(logging.getLogger("httpcore"), "disabled", True)
    client.delete("/logs")
    for _ in range(10):
        for path in ("/sync/status", "/sync/auto-update", "/health", "/ui-logs/"):
            assert client.get(path).status_code == 200
    assert client.get("/logs").json()["size_bytes"] == 0

    monkeypatch.setattr(api, "filter_manager", None)
    assert client.get("/health").status_code == 503
    assert "hushclient.requests GET /health -> 503" in client.get("/logs").json()["text"]

    monkeypatch.setattr(api, "_run_filter_sync_with_logs", lambda: api.SyncFiltersResponse(success=True))
    assert client.post("/sync/filters").status_code == 200
    assert "hushclient.requests POST /sync/filters -> 200" in client.get("/logs").json()["text"]


def test_scheduled_retry_recovers_and_clears_status(monkeypatch):
    attempts = []
    def run():
        attempts.append(1)
        return api.SyncApplyResponse(success=len(attempts) == 2, retryable=True)
    monkeypatch.setattr(api, "_run_scheduled_auto_update", run)
    monkeypatch.setattr(api, "AUTO_UPDATE_RETRY_SECONDS", 0.001)
    asyncio.run(api._run_scheduled_retry_window(datetime.now().astimezone(), asyncio.Event()))
    assert len(attempts) == 2
    assert api._auto_update_retry_at is None and api._auto_update_retry_until is None


def test_retry_window_is_anchored_to_schedule(monkeypatch):
    attempts = []
    def run():
        attempts.append(1)
        return api.SyncApplyResponse(success=False, retryable=True)
    monkeypatch.setattr(api, "_run_scheduled_auto_update", run)
    scheduled = datetime.now(timezone.utc) - timedelta(minutes=59)
    asyncio.run(api._run_scheduled_retry_window(scheduled, asyncio.Event()))
    assert len(attempts) == 1


@pytest.mark.parametrize("result", [api.SyncApplyResponse(success=True), api.SyncApplyResponse(success=False)])
def test_no_retry_after_success_or_manifest_reload_failure(monkeypatch, result):
    attempts = []
    def run():
        attempts.append(1)
        return result
    monkeypatch.setattr(api, "_run_scheduled_auto_update", run)
    asyncio.run(api._run_scheduled_retry_window(datetime.now().astimezone(), asyncio.Event()))
    assert len(attempts) == 1


def test_schedule_change_cancels_pending_retry(monkeypatch):
    attempts = []
    def run():
        attempts.append(1)
        return api.SyncApplyResponse(success=False, retryable=True)
    monkeypatch.setattr(api, "_run_scheduled_auto_update", run)

    async def scenario():
        event = asyncio.Event()
        task = asyncio.create_task(api._run_scheduled_retry_window(datetime.now().astimezone(), event))
        while api._auto_update_retry_at is None:
            await asyncio.sleep(0.001)
        event.set()
        await asyncio.wait_for(task, timeout=1)
    asyncio.run(scenario())
    assert len(attempts) == 1
    assert api._auto_update_retry_at is None
