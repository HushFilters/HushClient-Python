"""Read-only certificate expiry monitoring; no private keys are required."""
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

from cryptography import x509


CERTIFICATE_FILES = {
    "public/fullchain.pem": "Customer-facing certificate",
    "internal/ca.crt": "Internal CA",
    "internal/hushfilter-api.crt": "Internal API certificate",
    "internal/nginx-client.crt": "Nginx mTLS client certificate",
    "internal/healthcheck.crt": "API health-check client certificate",
}


def valid_before(cert):
    if hasattr(cert, "not_valid_before_utc"):
        return cert.not_valid_before_utc
    return cert.not_valid_before.replace(tzinfo=timezone.utc)


def valid_until(cert):
    if hasattr(cert, "not_valid_after_utc"):
        return cert.not_valid_after_utc
    return cert.not_valid_after.replace(tzinfo=timezone.utc)


def inspect_certificates(directory: Path, warning_days: int = 30, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    results = []
    for relative_path, label in CERTIFICATE_FILES.items():
        try:
            # Inspect the entire supplied chain, including intermediates.
            certificates = x509.load_pem_x509_certificates((directory / relative_path).read_bytes())
            if not certificates:
                raise ValueError("Empty certificate file")
        except (OSError, ValueError):
            results.append({"name": label, "file": relative_path, "status": "unreadable",
                            "expires_at": None, "days_remaining": None})
            continue
        for index, cert in enumerate(certificates):
            expiry = valid_until(cert)
            if valid_before(cert) > now:
                status = "not_yet_valid"
            elif expiry <= now:
                status = "expired"
            elif expiry <= now + timedelta(days=warning_days):
                status = "expiring"
            else:
                status = "valid"
            results.append({"name": label if index == 0 else f"{label} (chain certificate {index + 1})",
                            "file": relative_path, "status": status,
                            "expires_at": expiry.isoformat(timespec="seconds"),
                            "days_remaining": math.floor((expiry - now).total_seconds() / 86400)})
    return {"checked_at": now.isoformat(timespec="seconds"), "warning_days": warning_days,
            "certificates": results}


def alert_certificate_issues(manager, directory: Path) -> dict:
    """Combine all expiry/validity problems in one throttled email notification."""
    result = inspect_certificates(directory, manager.snapshot()["settings"]["certificate_warning_days"])
    issues = [cert for cert in result["certificates"] if cert["status"] != "valid"]
    if issues:
        lines = [f"{cert['name']}: {cert['status'].replace('_', ' ')}"
                 + (f"; expires {cert['expires_at']}" if cert["expires_at"] else "; file missing or invalid")
                 for cert in issues]
        manager.notify("certificate_expiry", "TLS certificates need attention:\n" + "\n".join(lines)
                       + "\n\nReplace or renew affected certificates and recreate the affected containers."
                       + " See the README certificate refresh procedure.")
    return result
