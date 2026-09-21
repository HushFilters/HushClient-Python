import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from unittest.mock import MagicMock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from fastapi.testclient import TestClient
import pytest

import api
from alerts import AlertManager, AlertSettings
from certificates import alert_certificate_issues, inspect_certificates, valid_until
from scripts import ensure_tls_certs as generator
from test.test_api_ui_endpoints import DummyFilterManager


@pytest.fixture(scope="module")
def generated_tls(tmp_path_factory):
    directory = tmp_path_factory.mktemp("certificate-fixture")
    (directory / "public").mkdir()
    (directory / "internal").mkdir()
    generator._ensure_public_cert(cert_path=directory / "public/fullchain.pem", key_path=directory / "public/privkey.pem",
                                  names=generator.DEFAULT_PUBLIC_NAMES, valid_days=825)
    generator._ensure_internal_mtls_certs(internal_dir=directory / "internal",
                                          server_names=generator.DEFAULT_INTERNAL_SERVER_NAMES, valid_days=825)
    return directory


@pytest.fixture
def tls_dir(generated_tls, tmp_path, monkeypatch):
    monkeypatch.delenv("HUSHFILTER_PUBLIC_HOSTNAMES", raising=False)
    monkeypatch.delenv("HUSHFILTER_INTERNAL_SERVER_NAMES", raising=False)
    directory = tmp_path / "tls"
    shutil.copytree(generated_tls, directory)
    return directory


def reissue(directory, relative_path, *, days=2, future=False):
    cert_path = directory / relative_path
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    internal = relative_path.startswith("internal/")
    issuer_key_path = directory / "internal/ca.key" if internal else directory / "public/privkey.pem"
    key = serialization.load_pem_private_key(issuer_key_path.read_bytes(), password=None)
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(cert.subject).issuer_name(cert.issuer)
               .public_key(cert.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now + timedelta(days=1) if future else now - timedelta(days=10))
               .not_valid_after(now + timedelta(days=days)))
    for extension in cert.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    renewed = builder.sign(key, hashes.SHA256())
    generator._write_certificate(cert_path, renewed)
    return renewed


def snapshot_files(directory):
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def test_bootstrap_reuses_valid_pairs_and_creates_separate_health_identity(tls_dir):
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir)]) == 0
    assert snapshot_files(tls_dir) == before
    ca = x509.load_pem_x509_certificate((tls_dir / "internal/ca.crt").read_bytes())
    server = x509.load_pem_x509_certificate((tls_dir / "internal/hushfilter-api.crt").read_bytes())
    health = x509.load_pem_x509_certificate((tls_dir / "internal/healthcheck.crt").read_bytes())
    assert valid_until(ca) > valid_until(server) + timedelta(days=2000)
    health.verify_directly_issued_by(ca)
    assert ExtendedKeyUsageOID.CLIENT_AUTH in health.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert before["internal/healthcheck.key"] != before["internal/nginx-client.key"]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tls_dir.rglob("*.key"))


def test_upgrade_adds_healthcheck_without_changing_existing_certificates(tls_dir):
    (tls_dir / "internal/healthcheck.crt").unlink()
    (tls_dir / "internal/healthcheck.key").unlink()
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir)]) == 0
    assert all((tls_dir / path).read_bytes() == data for path, data in before.items())
    assert (tls_dir / "internal/healthcheck.crt").is_file()


@pytest.mark.parametrize("placeholders", [("crt",), ("key",), ("crt", "key")])
def test_upgrade_recovers_empty_docker_healthcheck_directories(tls_dir, placeholders):
    for extension in ("crt", "key"):
        path = tls_dir / f"internal/healthcheck.{extension}"
        path.unlink()
        if extension in placeholders:
            path.mkdir()
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir)]) == 0
    assert all((tls_dir / path).read_bytes() == data for path, data in before.items())
    assert (tls_dir / "internal/healthcheck.crt").is_file()
    assert (tls_dir / "internal/healthcheck.key").is_file()
    assert generator.main(["--tls-dir", str(tls_dir), "--check-only"]) == 0


def test_bootstrap_recovers_empty_file_mount_directories(tls_dir):
    for relative in snapshot_files(tls_dir):
        path = tls_dir / relative
        path.unlink()
        path.mkdir()
    assert generator.main(["--tls-dir", str(tls_dir)]) == 0
    assert generator.main(["--tls-dir", str(tls_dir), "--check-only"]) == 0


def test_directory_check_is_read_only_and_explains_recovery(tls_dir, capsys):
    path = tls_dir / "internal/healthcheck.crt"
    path.unlink()
    path.mkdir()
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir), "--check-only"]) == 1
    assert path.is_dir()
    assert snapshot_files(tls_dir) == before
    assert "Run the initializer without --check-only" in capsys.readouterr().err


def test_populated_placeholder_blocks_cleanup_without_deleting_data(tls_dir, capsys):
    for extension in ("crt", "key"):
        path = tls_dir / f"internal/healthcheck.{extension}"
        path.unlink()
        path.mkdir()
    (tls_dir / "internal/healthcheck.key/keep.txt").write_text("preserve me")
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir)]) == 1
    assert snapshot_files(tls_dir) == before
    assert (tls_dir / "internal/healthcheck.crt").is_dir()
    assert "Leaving it untouched" in capsys.readouterr().err


def test_directory_symlink_is_not_removed(tls_dir, tmp_path):
    target = tmp_path / "keep"
    target.mkdir()
    path = tls_dir / "internal/healthcheck.crt"
    path.unlink()
    path.symlink_to(target, target_is_directory=True)
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir)]) == 1
    assert path.is_symlink() and target.is_dir()
    assert snapshot_files(tls_dir) == before


def test_ca_placeholder_never_replaces_existing_trust(tls_dir):
    path = tls_dir / "internal/ca.crt"
    path.unlink()
    path.mkdir()
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir)]) == 1
    assert snapshot_files(tls_dir) == before
    assert not path.exists()


def test_renewal_preserves_ca_and_leaf_private_keys(tls_dir):
    for path in ("public/fullchain.pem", "internal/hushfilter-api.crt", "internal/nginx-client.crt", "internal/healthcheck.crt"):
        reissue(tls_dir, path, days=-1)
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir), "--renew"]) == 0
    after = snapshot_files(tls_dir)
    for path, data in before.items():
        if path.endswith(".key") or path in {"internal/ca.crt", "public/privkey.pem"}:
            assert after[path] == data
        else:
            assert after[path] != data
            original = x509.load_pem_x509_certificate(data)
            renewed = x509.load_pem_x509_certificate(after[path])
            assert renewed.subject == original.subject
            assert list(renewed.extensions) == list(original.extensions)
    assert all(cert["status"] == "valid" for cert in inspect_certificates(tls_dir)["certificates"])


@pytest.mark.parametrize("days,future,status", [(2, False, "expiring"), (-1, False, "expired"), (2, True, "not_yet_valid")])
def test_read_only_check_detects_expiry_and_future_validity(tls_dir, days, future, status):
    reissue(tls_dir, "public/fullchain.pem", days=days, future=future)
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir), "--check-only"]) == 1
    assert inspect_certificates(tls_dir)["certificates"][0]["status"] == status
    assert snapshot_files(tls_dir) == before


def test_default_startup_rejects_expired_cert_without_overwriting_it(tls_dir):
    reissue(tls_dir, "internal/nginx-client.crt", days=-1)
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir)]) == 1
    assert snapshot_files(tls_dir) == before


def test_external_public_certificate_and_ca_are_not_replaced(tls_dir):
    ca, ca_key = generator._load_pair(tls_dir / "internal/ca.crt", tls_dir / "internal/ca.key")
    _, public_key = generator._load_pair(tls_dir / "public/fullchain.pem", tls_dir / "public/privkey.pem")
    external = generator._sign_leaf_cert(subject=generator._name("external.example.com"), issuer_cert=ca[0],
                                         issuer_key=ca_key, public_key=public_key.public_key(), valid_days=2,
                                         names=("external.example.com",), eku=ExtendedKeyUsageOID.SERVER_AUTH)
    generator._write_certificate(tls_dir / "public/fullchain.pem", external)
    (tls_dir / "internal/ca.key").unlink()  # Existing external material needs no signing key locally.
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir), "--renew"]) == 0
    assert snapshot_files(tls_dir) == before
    assert generator.main(["--tls-dir", str(tls_dir), "--check-only"]) == 1


def test_missing_external_signer_fails_renewal_without_changing_trust(tls_dir):
    reissue(tls_dir, "internal/nginx-client.crt")
    (tls_dir / "internal/ca.key").unlink()
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir), "--renew"]) == 1
    assert snapshot_files(tls_dir) == before


def test_expired_ca_requires_explicit_rotation(tls_dir):
    reissue(tls_dir, "internal/ca.crt", days=-1)
    before = snapshot_files(tls_dir)
    assert generator.main(["--tls-dir", str(tls_dir), "--renew"]) == 1
    assert snapshot_files(tls_dir) == before


def test_renewed_leaf_cannot_outlive_stable_ca(tls_dir):
    ca = reissue(tls_dir, "internal/ca.crt", days=100)
    reissue(tls_dir, "internal/hushfilter-api.crt")
    assert generator.main(["--tls-dir", str(tls_dir), "--renew"]) == 0
    server = x509.load_pem_x509_certificate((tls_dir / "internal/hushfilter-api.crt").read_bytes())
    assert valid_until(server) == valid_until(ca)


def test_incomplete_pairs_and_mismatched_keys_fail(tls_dir):
    (tls_dir / "public/privkey.pem").write_bytes((tls_dir / "internal/nginx-client.key").read_bytes())
    assert generator.main(["--tls-dir", str(tls_dir), "--check-only"]) == 1
    (tls_dir / "public/privkey.pem").unlink()
    assert generator.main(["--tls-dir", str(tls_dir)]) == 1
    assert not (tls_dir / "public/privkey.pem").exists()


def test_monitor_reads_chains_and_never_reads_private_keys(tls_dir, monkeypatch):
    old_ca = reissue(tls_dir, "internal/ca.crt", days=2)
    with (tls_dir / "public/fullchain.pem").open("ab") as handle:
        handle.write(old_ca.public_bytes(serialization.Encoding.PEM))
    original = Path.read_bytes
    def public_only(path):
        assert path.suffix != ".key" and path.name != "privkey.pem"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", public_only)
    result = inspect_certificates(tls_dir)
    public_chain = [cert for cert in result["certificates"] if cert["file"] == "public/fullchain.pem"]
    assert [cert["status"] for cert in public_chain] == ["valid", "expiring"]
    (tls_dir / "internal/healthcheck.crt").write_text("not a certificate")
    assert inspect_certificates(tls_dir)["certificates"][-1]["status"] == "unreadable"


def test_certificate_alert_selection_warning_window_and_cooldown(tls_dir, tmp_path):
    manager = AlertManager(tmp_path / "alerts.json")
    manager.configure(AlertSettings(enabled=True, smtp_host="smtp.example.com", sender="hush@example.com",
                                     recipients=["ops@example.com"], events=["certificate_expiry"], certificate_warning_days=5))
    reissue(tls_dir, "internal/nginx-client.crt", days=10)
    alert_certificate_issues(manager, tls_dir)
    assert manager._queue.empty()
    reissue(tls_dir, "internal/nginx-client.crt", days=2)
    alert_certificate_issues(manager, tls_dir)
    alert_certificate_issues(manager, tls_dir)
    assert manager._queue.qsize() == 1
    event, summary, _ = manager._queue.get_nowait()
    assert event == "certificate_expiry" and "Nginx mTLS client certificate" in summary
    assert "expires" in summary and "PRIVATE KEY" not in summary
    manager.configure(manager.settings.model_copy(update={"events": ["sync_failure"]}))
    alert_certificate_issues(manager, tls_dir)
    assert manager._queue.empty()


def test_certificate_api_startup_save_and_periodic_checks(tls_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HUSHCLIENT_TLS_MONITOR", "1")
    monkeypatch.setenv("HUSHCLIENT_TLS_DIR", str(tls_dir))
    monkeypatch.setenv("HUSHCLIENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AUTO_UPDATE_STATE_PATH", str(tmp_path / "schedule.json"))
    monkeypatch.setattr(api, "FilterManager", DummyFilterManager)
    monkeypatch.setattr(api, "CERTIFICATE_CHECK_INTERVAL_SECONDS", .01)
    inspect = MagicMock(wraps=api.alert_certificate_issues)
    monkeypatch.setattr(api, "alert_certificate_issues", inspect)
    with TestClient(api.app) as client:
        assert inspect.call_count >= 1
        result = client.get("/alerts/certificates")
        assert result.status_code == 200
        assert result.json()["enabled"] is True
        assert len(result.json()["certificates"]) == 5
        assert result.headers["cache-control"] == "no-store"
        before = inspect.call_count
        assert client.put("/alerts/settings", json={"certificate_warning_days": 60}).status_code == 200
        assert inspect.call_count > before
        assert client.get("/alerts/certificates").json()["warning_days"] == 60
        async def wait_for_periodic_check():
            before = inspect.call_count
            for _ in range(100):
                if inspect.call_count > before:
                    return
                await asyncio.sleep(.01)
            pytest.fail("Periodic certificate check did not run")
        client.portal.call(wait_for_periodic_check)


def test_compose_mounts_exclude_other_services_keys_and_ca_signer():
    # PyYAML is optional in the dev environment; production does not depend on it.
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(Path("docker-compose.yml").read_text())
    expected_keys = {
        "hushfilter-api": {"./tls/internal/hushfilter-api.key", "./tls/internal/healthcheck.key"},
        "nginx": {"./tls/public/privkey.pem", "./tls/internal/nginx-client.key"},
    }
    for service, expected in expected_keys.items():
        mounts = [mount for mount in config["services"][service]["volumes"] if isinstance(mount, dict)]
        assert {m["source"] for m in mounts if m["source"].endswith((".key", "privkey.pem"))} == expected
        assert all(m["read_only"] and m["bind"]["create_host_path"] is False for m in mounts)
        assert not any(m["source"].endswith("ca.key") for m in mounts)
        assert not any(isinstance(m, str) and m.startswith("./tls:") for m in config["services"][service]["volumes"])
    healthcheck = " ".join(config["services"]["hushfilter-api"]["healthcheck"]["test"])
    assert "healthcheck.key" in healthcheck and "nginx-client.key" not in healthcheck
