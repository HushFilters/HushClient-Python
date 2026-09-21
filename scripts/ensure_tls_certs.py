from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


# Direct script invocation puts scripts/ rather than the project root on sys.path.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from certificates import inspect_certificates, valid_before, valid_until

DEFAULT_PUBLIC_NAMES = ("localhost", "127.0.0.1")
DEFAULT_INTERNAL_SERVER_NAMES = ("hushfilter-api", "localhost", "127.0.0.1")
DEFAULT_VALID_DAYS = 825
DEFAULT_CA_VALID_DAYS = 3650
DEFAULT_RENEW_BEFORE_DAYS = 30


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    public_dir = args.tls_dir / "public"
    internal_dir = args.tls_dir / "internal"
    public_names = _split_names(os.getenv("HUSHFILTER_PUBLIC_HOSTNAMES"), default=DEFAULT_PUBLIC_NAMES)
    server_names = _split_names(os.getenv("HUSHFILTER_INTERNAL_SERVER_NAMES"), default=DEFAULT_INTERNAL_SERVER_NAMES)
    try:
        _prepare_certificate_paths(args.tls_dir, check_only=args.check_only)
        if not args.check_only:
            public_dir.mkdir(parents=True, exist_ok=True)
            internal_dir.mkdir(parents=True, exist_ok=True)
            _ensure_public_cert(cert_path=public_dir / "fullchain.pem", key_path=public_dir / "privkey.pem",
                                names=public_names, valid_days=args.valid_days, renew=args.renew,
                                renew_before_days=args.renew_before_days)
            _ensure_internal_mtls_certs(internal_dir=internal_dir, server_names=server_names,
                                       valid_days=args.valid_days, renew=args.renew,
                                       renew_before_days=args.renew_before_days, ca_valid_days=args.ca_valid_days)
        _validate_certificate_set(args.tls_dir, server_names)
        result = inspect_certificates(args.tls_dir, args.renew_before_days)
        for cert in result["certificates"]:
            print(f"{cert['file']}: {cert['status']}; expires {cert['expires_at'] or 'unknown'}")
        failures = {"expired", "not_yet_valid", "unreadable"}
        if args.check_only:
            failures.add("expiring")
        if any(cert["status"] in failures for cert in result["certificates"]):
            print("Certificate replacement/renewal is required; see the README refresh procedure.", file=sys.stderr)
            return 1
    except (OSError, ValueError, RuntimeError, x509.ExtensionNotFound, InvalidSignature) as exc:
        print(f"TLS certificate check failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create, check, or renew HushFilter TLS certificates without rotating the CA.")
    parser.add_argument("--tls-dir", type=Path, default=Path("tls"))
    parser.add_argument("--valid-days", type=int, default=DEFAULT_VALID_DAYS,
                        help="Lifetime of generated leaf certificates (default: 825 days).")
    parser.add_argument("--ca-valid-days", type=int, default=DEFAULT_CA_VALID_DAYS,
                        help="Lifetime of a NEW internal CA (default: 3650 days); existing CAs are never replaced.")
    parser.add_argument("--renew-before-days", type=int, default=DEFAULT_RENEW_BEFORE_DAYS,
                        help="Expiry warning and renewal threshold (default: 30 days).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--renew", action="store_true",
                      help="Renew expiring self-signed public and locally signable internal leaf certificates.")
    mode.add_argument("--check-only", action="store_true",
                      help="Read-only check; exit 1 for invalid, missing, expired, or soon-expiring certificates.")
    args = parser.parse_args(argv)
    if args.renew_before_days < 1 or args.valid_days <= args.renew_before_days or args.ca_valid_days <= args.valid_days:
        parser.error("Require 0 < renew-before-days < valid-days < ca-valid-days")
    return args


def _prepare_certificate_paths(tls_dir: Path, *, check_only: bool) -> None:
    """Recover empty directories left by file bind mounts before provisioning."""
    paths = [tls_dir / "public/fullchain.pem", tls_dir / "public/privkey.pem"]
    paths.extend(tls_dir / "internal" / f"{stem}.{extension}"
                 for stem in ("ca", "hushfilter-api", "nginx-client", "healthcheck")
                 for extension in ("crt", "key"))
    placeholders = []
    # Check every path first so a populated directory never causes partial cleanup.
    for path in paths:
        if not path.is_dir():
            continue
        if path.is_symlink() or any(path.iterdir()):
            raise RuntimeError(f"Expected a certificate/key file at {path}, but found a non-empty directory "
                               "or directory symlink. Leaving it untouched; inspect and restore the correct file.")
        if check_only:
            raise RuntimeError(f"Expected a certificate/key file at {path}, but found an empty directory, "
                               "possibly created by Docker. Run the initializer without --check-only before "
                               "recreating the application containers (see README).")
        placeholders.append(path)
    for path in placeholders:
        # rmdir only removes an empty directory, including if another process
        # writes to it after the preflight. Never recursively delete TLS paths.
        path.rmdir()
        print(f"Removed empty certificate-file placeholder: {path}")


def _load_pair(cert_path: Path, key_path: Path):
    certificates = x509.load_pem_x509_certificates(cert_path.read_bytes())
    if not certificates:
        raise RuntimeError(f"No certificates found in {cert_path}")
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    encoding = serialization.Encoding.DER
    fmt = serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(encoding, fmt) != certificates[0].public_key().public_bytes(encoding, fmt):
        raise RuntimeError(f"Certificate and private key do not match: {cert_path}")
    return certificates, key


def _expires_soon(cert, days):
    return valid_until(cert) <= datetime.now(UTC) + timedelta(days=days)


def _validate_chain(chain, trusted):
    current = chain[0]
    seen = set()
    while current.fingerprint(hashes.SHA256()) not in {cert.fingerprint(hashes.SHA256()) for cert in trusted}:
        fingerprint = current.fingerprint(hashes.SHA256())
        if fingerprint in seen:
            raise RuntimeError("Cycle in internal certificate chain")
        seen.add(fingerprint)
        issuer = None
        for candidate in [*chain[1:], *trusted]:
            if candidate.subject != current.issuer:
                continue
            try:
                current.verify_directly_issued_by(candidate)
            except (ValueError, TypeError, InvalidSignature):
                continue
            if not candidate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise RuntimeError("Internal certificate issuer is not a CA")
            issuer = candidate
            break
        if issuer is None:
            raise RuntimeError("Internal certificate is not signed by the configured CA/chain")
        current = issuer


def _validate_certificate_set(tls_dir, server_names):
    _load_pair(tls_dir / "public/fullchain.pem", tls_dir / "public/privkey.pem")
    internal = tls_dir / "internal"
    trusted = x509.load_pem_x509_certificates((internal / "ca.crt").read_bytes())
    if not trusted or any(not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca for cert in trusted):
        raise RuntimeError("Internal trust file must contain CA certificates")
    for stem, eku in (("hushfilter-api", ExtendedKeyUsageOID.SERVER_AUTH),
                      ("nginx-client", ExtendedKeyUsageOID.CLIENT_AUTH),
                      ("healthcheck", ExtendedKeyUsageOID.CLIENT_AUTH)):
        chain, _ = _load_pair(internal / f"{stem}.crt", internal / f"{stem}.key")
        _validate_chain(chain, trusted)
        if eku not in chain[0].extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
            raise RuntimeError(f"Incorrect TLS usage on {stem}.crt")
        if stem == "hushfilter-api":
            names = chain[0].extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            for name in set(server_names) | {"hushfilter-api", "localhost"}:
                expected = _subject_alt_names((name,))[0]
                if expected not in names:
                    raise RuntimeError(f"Internal API certificate is missing SAN {name!r}")


def _split_names(raw_value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw_value is None or not raw_value.strip():
        return default
    names = tuple(name.strip() for name in raw_value.split(",") if name.strip())
    return names or default


def _ensure_public_cert(*, cert_path: Path, key_path: Path, names: tuple[str, ...],
                        valid_days: int, renew: bool = False,
                        renew_before_days: int = DEFAULT_RENEW_BEFORE_DAYS) -> None:
    if cert_path.exists() != key_path.exists():
        raise RuntimeError(f"Public TLS certificate is incomplete. Expected both {cert_path} and {key_path}.")
    if cert_path.exists():
        chain, key = _load_pair(cert_path, key_path)
        cert = chain[0]
        if not renew or not _expires_soon(cert, renew_before_days):
            return
        if cert.issuer != cert.subject:
            print("Externally issued public certificate must be renewed by its CA/ACME provider; leaving it unchanged.")
            return
        cert.verify_directly_issued_by(cert)
        _write_certificate(cert_path, _renew_certificate(cert, key, valid_days))
        print(f"Renewed self-signed public TLS certificate: {cert_path}")
        return
    else:
        key = _new_private_key()
        _write_private_key(key_path, key)
    subject = _name(f"HushFilter public {names[0]}")
    cert = (
        _certificate_builder(subject=subject, issuer=subject, public_key=key.public_key(), valid_days=valid_days)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(_subject_alt_names(names), critical=False)
        .sign(private_key=key, algorithm=_signing_algorithm(key))
    )
    _write_certificate(cert_path, cert)
    print(f"Generated/renewed self-signed public TLS certificate: {cert_path}")


def _ensure_internal_mtls_certs(*, internal_dir: Path, server_names: tuple[str, ...],
                                valid_days: int, renew: bool = False,
                                renew_before_days: int = DEFAULT_RENEW_BEFORE_DAYS,
                                ca_valid_days: int = DEFAULT_CA_VALID_DAYS) -> None:
    ca_cert_path, ca_key_path = internal_dir / "ca.crt", internal_dir / "ca.key"
    if not ca_cert_path.exists():
        if any(internal_dir.iterdir()):
            raise RuntimeError("Internal CA is missing from an existing certificate set; restore it instead of silently replacing trust.")
        ca_key = _new_private_key()
        ca_subject = _name("HushFilter internal CA")
        ca_cert = (
            _certificate_builder(subject=ca_subject, issuer=ca_subject, public_key=ca_key.public_key(), valid_days=ca_valid_days)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                        data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                        encipher_only=False, decipher_only=False), critical=True)
            .sign(private_key=ca_key, algorithm=hashes.SHA256())
        )
        _write_private_key(ca_key_path, ca_key)
        _write_certificate(ca_cert_path, ca_cert)
    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    now = datetime.now(UTC)
    if valid_before(ca_cert) > now or valid_until(ca_cert) <= now:
        raise RuntimeError("Internal CA is expired or not yet valid; arrange an explicit CA rotation (see README).")
    if not ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise RuntimeError("Internal signing certificate is not a CA")
    for stem, names, eku in (
        ("hushfilter-api", server_names, ExtendedKeyUsageOID.SERVER_AUTH),
        ("nginx-client", ("nginx-client",), ExtendedKeyUsageOID.CLIENT_AUTH),
        ("healthcheck", ("healthcheck",), ExtendedKeyUsageOID.CLIENT_AUTH),
    ):
        cert_path, key_path = internal_dir / f"{stem}.crt", internal_dir / f"{stem}.key"
        if cert_path.exists() != key_path.exists():
            raise RuntimeError(f"Incomplete internal pair: {cert_path} and {key_path} are both required.")
        if cert_path.exists():
            chain, key = _load_pair(cert_path, key_path)
            if not renew or not _expires_soon(chain[0], renew_before_days):
                continue
        else:
            key = None
        if not ca_key_path.exists():
            raise RuntimeError(f"Cannot issue/renew {stem}.crt without the CA key. Obtain this certificate from your PKI; the CA key need not be copied here.")
        _, ca_key = _load_pair(ca_cert_path, ca_key_path)
        if _expires_soon(ca_cert, renew_before_days):
            raise RuntimeError("Internal CA expires inside the renewal window; rotate the CA explicitly before issuing more leaves.")
        if key is None:
            key = _new_private_key()
            cert = _sign_leaf_cert(subject=_name(stem), issuer_cert=ca_cert, issuer_key=ca_key,
                                  public_key=key.public_key(), valid_days=valid_days, names=names, eku=eku)
        else:
            # Renewal preserves SANs, identity, and extensions. A chain issued by
            # an external intermediate must go back to its original issuer.
            chain[0].verify_directly_issued_by(ca_cert)
            cert = _renew_certificate(chain[0], ca_key, valid_days, expires_at=valid_until(ca_cert))
        if not key_path.exists():
            _write_private_key(key_path, key)
        _write_certificate(cert_path, cert)
        print(f"Generated/renewed internal certificate, retaining the CA: {cert_path}")


def _signing_algorithm(key):
    from cryptography.hazmat.primitives.asymmetric import ed25519, ed448
    return None if isinstance(key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)) else hashes.SHA256()


def _renew_certificate(cert, issuer_key, valid_days, *, expires_at=None):
    builder = _certificate_builder(subject=cert.subject, issuer=cert.issuer, public_key=cert.public_key(),
                                   valid_days=valid_days, expires_at=expires_at)
    for extension in cert.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    return builder.sign(private_key=issuer_key, algorithm=_signing_algorithm(issuer_key))


def _new_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HushFilter"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _certificate_builder(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key,
    valid_days: int,
    expires_at: datetime | None = None,
) -> x509.CertificateBuilder:
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(min(now + timedelta(days=max(valid_days, 1)), expires_at) if expires_at else now + timedelta(days=max(valid_days, 1)))
    )


def _sign_leaf_cert(
    *,
    subject: x509.Name,
    issuer_cert: x509.Certificate,
    issuer_key: rsa.RSAPrivateKey,
    public_key,
    valid_days: int,
    names: tuple[str, ...],
    eku: x509.ObjectIdentifier,
) -> x509.Certificate:
    return (
        _certificate_builder(
            subject=subject,
            issuer=issuer_cert.subject,
            public_key=public_key,
            valid_days=valid_days,
            expires_at=valid_until(issuer_cert),
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .add_extension(_subject_alt_names(names), critical=False)
        .sign(private_key=issuer_key, algorithm=_signing_algorithm(issuer_key))
    )


def _subject_alt_names(names: tuple[str, ...]) -> x509.SubjectAlternativeName:
    general_names: list[x509.GeneralName] = []
    for name in names:
        try:
            general_names.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            general_names.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(general_names)


def _write_atomic(path: Path, content: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.chmod(temporary, mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_private_key(path: Path, key) -> None:
    _write_atomic(path, key.private_bytes(encoding=serialization.Encoding.PEM,
                                        format=serialization.PrivateFormat.PKCS8,
                                        encryption_algorithm=serialization.NoEncryption()), 0o600)


def _write_certificate(path: Path, cert: x509.Certificate) -> None:
    _write_atomic(path, cert.public_bytes(serialization.Encoding.PEM), 0o644)


if __name__ == "__main__":
    raise SystemExit(main())
