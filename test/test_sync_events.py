"""Exercise live snapshots without downloading or reloading real filters."""
import asyncio
import json

from fastapi.testclient import TestClient

import api
from sync_events import SyncEvents


class ConnectedRequest:
    async def is_disconnected(self):
        return False


def event_payload(message):
    return json.loads(message.split("data: ", 1)[1])


def test_stream_initial_snapshots_worker_changes_reconnect_and_cleanup(monkeypatch):
    broker = SyncEvents()
    state = api._SyncOperationLogState()
    monkeypatch.setattr(api, "sync_events", broker)
    monkeypatch.setattr(api, "sync_operation_logs", state)
    monkeypatch.setattr(api, "SYNC_EVENT_BATCH_SECONDS", 0)
    monkeypatch.setattr(api, "_machine_id_from_mac", lambda: "test-machine")
    monkeypatch.setattr(api, "_auto_update_enabled", False)
    monkeypatch.setattr(api, "_auto_update_hour", None)
    monkeypatch.setattr(api, "_auto_update_history", [])

    async def exercise():
        stream = api._sync_event_stream(ConnectedRequest())
        assert await anext(stream) == "retry: 3000\n\n"
        schedule = event_payload(await anext(stream))
        assert schedule["enabled"] is False
        assert schedule["machine_id"] == "test-machine"
        initial = event_payload(await anext(stream))
        assert initial == api.SyncStatusResponse(**state.snapshot()).model_dump()

        # Notifications from worker threads coalesce into the latest snapshot.
        def work():
            state.start("sync_apply")
            for index in range(250):
                state.append(f"line {index}")
            state.update_progress("download", completed=1, total=2)

        await asyncio.to_thread(work)
        assert event_payload(await anext(stream))["active"] is False  # Manual run, not scheduled.
        running = event_payload(await anext(stream))
        assert running["active"] is True
        assert len(running["logs"]) == 250
        assert next(p for p in running["progress"] if p["phase"] == "download")["completed"] == 1

        state.set_result({"success": True, "filter_count": 42})
        state.finish()
        await anext(stream)  # Schedule snapshot after operation completion.
        completed = event_payload(await anext(stream))
        assert completed["active"] is False
        assert completed["success"] is True
        assert completed["filter_count"] == 42
        await stream.aclose()
        assert not broker._subscribers

        # Reconnection returns full current state, without depending on missed events.
        reconnected = api._sync_event_stream(ConnectedRequest())
        await anext(reconnected)
        await anext(reconnected)
        assert event_payload(await anext(reconnected)) == completed
        await reconnected.aclose()
        assert not broker._subscribers

    asyncio.run(exercise())


def test_idle_stream_heartbeats_and_cancellation_release_subscribers(monkeypatch):
    broker = SyncEvents()
    monkeypatch.setattr(api, "sync_events", broker)
    monkeypatch.setattr(api, "SYNC_EVENT_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(api, "_machine_id_from_mac", lambda: "test-machine")

    async def exercise():
        stream = api._sync_event_stream(ConnectedRequest())
        for _ in range(3):
            await anext(stream)
        assert await anext(stream) == ": keepalive\n\n"
        # Cancel while waiting for a change, as an ASGI disconnect would do.
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        assert not broker._subscribers

    asyncio.run(exercise())


def test_schedule_and_history_changes_wake_all_connections(monkeypatch, tmp_path):
    broker = SyncEvents()
    monkeypatch.setattr(api, "sync_events", broker)
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "schedule.json"))
    monkeypatch.setattr(api, "_auto_update_enabled", False)
    monkeypatch.setattr(api, "_auto_update_hour", None)
    monkeypatch.setattr(api, "_auto_update_history", [])

    async def exercise():
        with broker.subscribe() as first, broker.subscribe() as second:
            await asyncio.to_thread(api._set_auto_update_config, True, 23)
            await asyncio.wait_for(first.wait(), 1)
            await asyncio.wait_for(second.wait(), 1)
            assert broker.take(first) == {"auto-update"}
            assert broker.take(second) == {"auto-update"}
            api._append_auto_update_history({"status": "success"})
            await asyncio.wait_for(first.wait(), 1)
            assert broker.take(first) == {"auto-update"}
        assert not broker._subscribers

    asyncio.run(exercise())


def test_stream_http_headers_and_rest_endpoints_remain_available(monkeypatch):
    async def finite_stream(request):
        yield ": keepalive\n\n"

    monkeypatch.setattr(api, "_sync_event_stream", finite_stream)
    monkeypatch.setattr(api, "_machine_id_from_mac", lambda: "test-machine")
    # No lifespan: these reads need no filter loading or background scheduler.
    client = TestClient(api.app)
    response = client.get("/sync/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.text == ": keepalive\n\n"
    assert client.get("/sync/status").status_code == 200
    assert client.get("/sync/auto-update").status_code == 200


def test_asgi_stream_flushes_events_and_disconnects_through_http_middleware(monkeypatch):
    broker = SyncEvents()
    state = api._SyncOperationLogState()
    monkeypatch.setattr(api, "sync_events", broker)
    monkeypatch.setattr(api, "sync_operation_logs", state)
    monkeypatch.setattr(api, "SYNC_EVENT_BATCH_SECONDS", 0)
    monkeypatch.setattr(api, "_machine_id_from_mac", lambda: "test-machine")

    async def exercise():
        incoming = asyncio.Queue()
        outgoing = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/sync/events", "raw_path": b"/sync/events", "query_string": b"",
            "headers": [], "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
        }
        task = asyncio.create_task(api.app(scope, incoming.get, outgoing.put))
        try:
            start = await asyncio.wait_for(outgoing.get(), 2)
            assert start["type"] == "http.response.start" and start["status"] == 200
            frames = []
            while len(frames) < 3:
                message = await asyncio.wait_for(outgoing.get(), 2)
                if message.get("body"):
                    assert message["more_body"] is True
                    frames.append(message["body"].decode())
            assert frames[0] == "retry: 3000\n\n"
            assert frames[1].startswith("event: auto-update\n")
            assert frames[2].startswith("event: sync-status\n")
            state.append("An update after the initial response")
            changed = await asyncio.wait_for(outgoing.get(), 2)
            assert "An update after the initial response" in changed["body"].decode()
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 2)
            assert not broker._subscribers
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(exercise())
