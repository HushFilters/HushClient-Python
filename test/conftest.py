import pytest


@pytest.fixture(autouse=True)
def isolate_alert_settings(monkeypatch, tmp_path):
    # Test clients must never load a developer's real SMTP credentials.
    monkeypatch.setenv("HUSHCLIENT_ALERT_SETTINGS_PATH", str(tmp_path / "alerts.json"))
