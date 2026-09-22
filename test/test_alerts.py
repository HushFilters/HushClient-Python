import asyncio
import json
import smtplib
import ssl
import threading
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest

import alerts
import api
from alerts import AlertManager, AlertSettings
from test.test_api_ui_endpoints import DummyFilterManager


def settings(**overrides):
    return AlertSettings(**{
        "enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 587,
        "security": "starttls", "username": "smtp-user", "password": "private-password",
        "sender": "hush@example.com", "recipients": ["ops@example.com"], **overrides,
    })


def test_settings_persist_preserve_clear_and_hide_password(tmp_path):
    path = tmp_path / "settings.json"
    manager = AlertManager(path)
    snapshot = manager.configure(settings())
    assert "private-password" not in json.dumps(snapshot)
    assert "password" not in snapshot["settings"]
    assert snapshot["password_configured"] is True
    assert path.stat().st_mode & 0o777 == 0o600
    reopened = AlertManager(path)
    assert reopened.settings.password.get_secret_value() == "private-password"
    reopened.configure(settings(password=None, smtp_port=465, security="ssl"))
    assert reopened.settings.password.get_secret_value() == "private-password"
    reopened.configure(settings(password="", username=""))
    assert reopened.snapshot()["password_configured"] is False
    assert not json.loads(path.read_text())["password"]


def test_invalid_settings_and_failed_save_keep_previous_configuration(tmp_path, monkeypatch):
    manager = AlertManager(tmp_path / "settings.json")
    manager.configure(settings())
    with pytest.raises(ValueError):
        manager.configure(settings(smtp_host=""))
    with pytest.raises(ValueError):
        manager.configure(settings(events=[]))
    with pytest.raises(ValueError):
        manager.configure(settings(security="none"))
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(alerts.os, "replace", fail)
    with pytest.raises(OSError):
        manager.configure(settings(password="replacement"))
    assert manager.settings.password.get_secret_value() == "private-password"
    assert AlertManager(manager.path).settings.password.get_secret_value() == "private-password"
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_saved_config_disables_alerts_without_exposing_secret(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text('{"password":"private-password", "enabled": true}')
    manager = AlertManager(path)
    assert manager.settings.enabled is False
    assert manager.snapshot()["settings_error"]
    assert "private-password" not in json.dumps(manager.snapshot())


@pytest.mark.parametrize("mode,port", [("starttls", 587), ("ssl", 465), ("none", 25)])
def test_smtp_transport_and_message(monkeypatch, mode, port):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.sendmail.return_value = {}
    factory = MagicMock(return_value=smtp)
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL" if mode == "ssl" else "SMTP", factory)
    config = settings(security=mode, smtp_port=port, username="" if mode == "none" else "smtp-user",
                      password="" if mode == "none" else "private-password")
    alerts.send_email(config, "Sync failed", "Operational summary")
    args, kwargs = factory.call_args
    assert args == ("smtp.example.com", port)
    assert kwargs["timeout"] == 10
    if mode == "starttls":
        context = smtp.starttls.call_args.kwargs["context"]
        assert smtp.ehlo.call_count == 2
    elif mode == "ssl":
        context = kwargs["context"]
        smtp.starttls.assert_not_called()
    else:
        smtp.starttls.assert_not_called()
        smtp.login.assert_not_called()
        context = None
    if context:
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    sender, recipients, message = smtp.sendmail.call_args.args
    assert sender == "hush@example.com" and recipients == ["ops@example.com"]
    assert b"Subject: [HushFilter] Sync failed" in message
    assert b"Operational summary" in message
    assert b"private-password" not in message


def test_tls_failure_never_authenticates_or_sends(monkeypatch):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("No TLS")
    monkeypatch.setattr(alerts.smtplib, "SMTP", lambda *args, **kwargs: smtp)
    with pytest.raises(smtplib.SMTPNotSupportedError):
        alerts.send_email(settings(), "test", "test")
    smtp.login.assert_not_called()
    smtp.sendmail.assert_not_called()


def test_partial_recipient_rejection_is_a_failed_delivery(monkeypatch):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.sendmail.return_value = {"ops@example.com": (550, b"rejected")}
    monkeypatch.setattr(alerts.smtplib, "SMTP", lambda *args, **kwargs: smtp)
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        alerts.send_email(settings(), "test", "test")


def test_queue_categories_cooldown_and_disabled_settings(tmp_path, monkeypatch):
    manager = AlertManager(tmp_path / "settings.json")
    manager.configure(settings(events=["sync_failure"], cooldown_minutes=1))
    now = [100.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: now[0])
    assert manager.notify("sync_failure", "First failure")
    assert not manager.notify("sync_failure", "Duplicate")
    assert not manager.notify("service_error", "Not selected")
    now[0] += 60
    assert manager.notify("sync_failure", "Later failure")
    manager.configure(settings(enabled=False))
    assert not manager.notify("sync_failure", "Disabled")
    send = MagicMock()
    monkeypatch.setattr(alerts, "send_email", send)
    manager.start()
    manager.close()
    send.assert_not_called()


def test_alert_queue_does_not_block_and_delivery_errors_are_redacted(tmp_path, monkeypatch, caplog):
    manager = AlertManager(tmp_path / "settings.json")
    manager.configure(settings(events=["sync_failure"]))
    started, release = threading.Event(), threading.Event()
    def fail(*args):
        started.set()
        assert release.wait(2)
        raise smtplib.SMTPAuthenticationError(535, b"private-password")
    monkeypatch.setattr(alerts, "send_email", fail)
    manager.start()
    try:
        assert manager.notify("sync_failure", "Sync failed")
        assert started.wait(1)
        assert manager.snapshot()["settings"]["enabled"]
        assert not manager.notify("sync_failure", "Duplicate")
    finally:
        release.set()
        manager.close()
    history = manager.snapshot()["history"]
    assert len(history) == 1 and history[0]["success"] is False
    assert "authentication" in history[0]["detail"]
    assert "private-password" not in json.dumps(history) + caplog.text


@pytest.fixture
def client(monkeypatch, tmp_path):
    class Manager(DummyFilterManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.filters = [("example.hf", object())]
    monkeypatch.setattr(api, "FilterManager", Manager)
    monkeypatch.setenv("HUSHCLIENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "schedule.json"))
    monkeypatch.delenv("AUTO_UPDATE_FILTERS", raising=False)
    with TestClient(api.app, raise_server_exceptions=False) as client:
        yield client


def test_alert_api_redaction_persistence_and_test_while_disabled(client, monkeypatch):
    send = MagicMock()
    monkeypatch.setattr(alerts, "send_email", send)
    data = settings(enabled=False).model_dump(mode="json", exclude={"password"})
    data["password"] = "private-password"
    response = client.put("/alerts/settings", json=data)
    assert response.status_code == 200
    assert "private-password" not in response.text
    assert response.json()["password_configured"] is True
    assert response.headers["cache-control"] == "no-store"
    response = client.post("/alerts/test")
    assert response.status_code == 200
    send.assert_called_once()
    assert send.call_args.args[0].password.get_secret_value() == "private-password"
    assert client.get("/alerts/settings").json()["history"][0]["event"] == "test"


@pytest.mark.parametrize("invalid", [
    {"password": {"secret": "private-password"}},
    {"username": "bad\r\nprivate-password"},
    {"recipients": ["ops@example.com\r\nBcc:private-password"]},
    {"smtp_host": "https://private-password"},
    {"unexpected": "private-password"},
])
def test_validation_does_not_echo_secrets(client, invalid):
    response = client.put("/alerts/settings", json=invalid)
    assert response.status_code == 422
    assert "private-password" not in response.text


def test_failed_test_email_returns_safe_error(client, monkeypatch):
    api.alert_manager.configure(settings())
    def fail(*args):
        raise smtplib.SMTPAuthenticationError(535, b"private-password")
    monkeypatch.setattr(alerts, "send_email", fail)
    response = client.post("/alerts/test")
    assert response.status_code == 502
    assert "private-password" not in response.text
    assert response.json()["success"] is False
    assert api.alert_manager._queue.empty()  # SMTP errors must not recursively alert.


def test_sync_manifest_reload_and_service_alert_hooks(client, monkeypatch):
    notify = MagicMock()
    monkeypatch.setattr(api.alert_manager, "notify", notify)
    monkeypatch.setattr(api, "_run_filter_sync_with_logs", lambda: api.SyncFiltersResponse(success=False, detail="private-detail"))
    assert client.post("/sync/filters").status_code == 500
    assert notify.call_args.args[0] == "sync_failure"
    assert "private-detail" not in notify.call_args.args[1]
    monkeypatch.setattr(api, "_run_manifest_update_with_logs", lambda: api.ManifestUpdateResponse(success=False))
    assert client.post("/sync/manifest").status_code == 500
    assert notify.call_args.args[0] == "filter_failure"
    monkeypatch.setattr(api, "_run_filter_reload_with_logs", lambda: api.ReloadFiltersResponse(success=False))
    assert client.post("/sync/reload").status_code == 500
    assert notify.call_args.args[0] == "filter_failure"
    monkeypatch.setattr(api, "filter_manager", None)
    assert client.get("/health").status_code == 503
    assert notify.call_args.args[0] == "service_error"


def test_scheduled_manifest_failure_alert_and_progress(client, monkeypatch):
    notify = MagicMock()
    monkeypatch.setattr(api.alert_manager, "notify", notify)
    monkeypatch.setattr(api, "_run_filter_sync_with_logs", lambda: api.SyncFiltersResponse(success=True))
    monkeypatch.setattr(api, "_run_manifest_update_with_logs", lambda: api.ManifestUpdateResponse(success=False))
    api._run_scheduled_auto_update()
    assert notify.call_args.args == ("filter_failure", "Scheduled filter operation failed during manifest.")
    status = client.get("/sync/status").json()
    assert status["success"] is False and status["active"] is False
    phases = {phase["phase"]: phase for phase in status["progress"]}
    assert phases["manifest"]["status"] == "failed"
    assert phases["reload"]["status"] == "pending"


def test_no_filters_partial_loading_and_async_error_hooks(client, monkeypatch):
    notify = MagicMock()
    monkeypatch.setattr(api.alert_manager, "notify", notify)
    api.filter_manager.filters = []
    api._check_loaded_filters()
    assert notify.call_args.args[0] == "no_filters"
    api.filter_manager.filters = [("one.hf", object())]
    api.filter_manager._requested_filter_count = 2
    api._check_loaded_filters()
    assert notify.call_args.args[0] == "filter_failure"
    async def trigger_error():
        asyncio.get_running_loop().call_exception_handler({"message": "unhandled task"})
    client.portal.call(trigger_error)
    assert notify.call_args.args[0] == "service_error"


def test_startup_filter_failure_sends_alert(monkeypatch, tmp_path):
    path = tmp_path / "alerts.json"
    AlertManager(path).configure(settings(events=["filter_failure"]))
    monkeypatch.setenv("HUSHCLIENT_ALERT_SETTINGS_PATH", str(path))
    monkeypatch.setenv("HUSHCLIENT_LOG_DIR", str(tmp_path / "logs"))
    send = MagicMock()
    monkeypatch.setattr(alerts, "send_email", send)
    def fail(**kwargs):
        raise RuntimeError("cannot load filters")
    monkeypatch.setattr(api, "FilterManager", fail)
    with pytest.raises(RuntimeError, match="cannot load filters"):
        with TestClient(api.app):
            pass
    send.assert_called_once()
    assert "startup" in send.call_args.args[2]


@pytest.mark.parametrize("path", ["/", "/ui-check/", "/ui-sync/", "/ui-logs/", "/ui-alerts/", "/docs"])
def test_navigation_footer_and_docs(client, path):
    response = client.get(path)
    assert response.status_code == 200
    for target in ("/ui-check/", "/ui-sync/", "/ui-logs/", "/ui-alerts/", "/docs"):
        assert f'href="{target}"' in response.text
    assert 'https://www.nwebbed.com/dashboard/product-configs/hushfilters' in response.text
    assert 'https://eu.nwebbed.com/dashboard/product-configs/hushfilters' in response.text
    assert "{{WORKSPACE_FOOTER}}" not in response.text
    assert "{{WORKSPACE_HEADER}}" not in response.text
    assert 'class="workspace-header"' in response.text
    assert 'aria-label="Hushfilters home"' in response.text
    # All pages use the same header; only the active tool's state changes.
    header = response.text.split('<header class="workspace-header">', 1)[1].split('</header>', 1)[0]
    if path != "/":
        assert header.count('aria-current="page"') == 1
        assert f'href="{path}" aria-current="page"' in header
    baseline = client.get("/").text.split('<header class="workspace-header">', 1)[1].split('</header>', 1)[0]
    assert header.replace('workspace-nav-link active', 'workspace-nav-link').replace(' aria-current="page"', '') == baseline
    if path == "/docs":
        assert "SwaggerUIBundle" in response.text and "/openapi.json" in response.text
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in response.text
    if path == "/ui-sync/":
        assert 'id="sync-progress"' in response.text
        assert 'downloaded-count' not in response.text


def test_alert_assets_and_openapi(client):
    assert client.get("/ui-alerts", follow_redirects=False).status_code == 307
    assert client.get("/ui-alerts/app.js").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/alerts/settings" in schema["paths"] and "/alerts/test" in schema["paths"]
    assert schema["components"]["schemas"]["AlertSettings"]["properties"]["password"]["anyOf"][0]["writeOnly"]


def test_unreadable_generated_manifest_reports_failure(client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "generate_manifest", lambda *args: 0)
    notify = MagicMock()
    monkeypatch.setattr(api.alert_manager, "notify", notify)
    response = client.post("/sync/manifest")
    assert response.status_code == 500
    assert response.json()["detail"] == "Could not read generated manifest"
    assert notify.call_args.args[0] == "filter_failure"
