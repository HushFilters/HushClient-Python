from __future__ import annotations

import argparse
import ipaddress
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


DEFAULT_PUBLIC_NAMES = ("localhost", "127.0.0.1")
DEFAULT_INTERNAL_SERVER_NAMES = ("hushfilter-api", "localhost", "127.0.0.1")
DEFAULT_VALID_DAYS = 825


def main() -> int:
    args = _parse_args()
    tls_dir = args.tls_dir
    public_dir = tls_dir / "public"
    internal_dir = tls_dir / "internal"
    public_dir.mkdir(parents=True, exist_ok=True)
    internal_dir.mkdir(parents=True, exist_ok=True)

    public_names = _split_names(
        os.getenv("HUSHFILTER_PUBLIC_HOSTNAMES"),
        default=DEFAULT_PUBLIC_NAMES,
    )
    internal_server_names = _split_names(
        os.getenv("HUSHFILTER_INTERNAL_SERVER_NAMES"),
        default=DEFAULT_INTERNAL_SERVER_NAMES,
    )

    _ensure_public_cert(
        cert_path=public_dir / "fullchain.pem",
        key_path=public_dir / "privkey.pem",
        names=public_names,
        valid_days=args.valid_days,
    )
    _ensure_internal_mtls_certs(
        internal_dir=internal_dir,
        server_names=internal_server_names,
        valid_days=args.valid_days,
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create default HushFilter TLS certificates when they are missing.",
    )
    parser.add_argument(
        "--tls-dir",
        type=Path,
        default=Path("tls"),
        help="Directory containing public/ and internal/ certificate folders.",
    )
    parser.add_argument(
        "--valid-days",
        type=int,
        default=DEFAULT_VALID_DAYS,
        help=f"Validity window for generated self-signed certificates. Defaults to {DEFAULT_VALID_DAYS}.",
    )
    return parser.parse_args()


def _split_names(raw_value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw_value is None or not raw_value.strip():
        return default
    names = tuple(name.strip() for name in raw_value.split(",") if name.strip())
    return names or default


def _ensure_public_cert(
    *,
    cert_path: Path,
    key_path: Path,
    names: tuple[str, ...],
    valid_days: int,
) -> None:
    if cert_path.exists() and key_path.exists():
        print(f"Using existing public TLS certificate: {cert_path}")
        return
    if cert_path.exists() != key_path.exists():
        raise RuntimeError(
            f"Public TLS certificate is incomplete. Expected both {cert_path} and {key_path}."
        )

    key = _new_private_key()
    subject = _name(f"HushFilter public {names[0]}")
    cert = (
        _certificate_builder(subject=subject, issuer=subject, public_key=key.public_key(), valid_days=valid_days)
        .add_extension(_subject_alt_names(names), critical=False)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    _write_private_key(key_path, key)
    _write_certificate(cert_path, cert)
    print(f"Generated self-signed public TLS certificate: {cert_path}")


def _ensure_internal_mtls_certs(
    *,
    internal_dir: Path,
    server_names: tuple[str, ...],
    valid_days: int,
) -> None:
    ca_cert_path = internal_dir / "ca.crt"
    ca_key_path = internal_dir / "ca.key"
    server_cert_path = internal_dir / "hushfilter-api.crt"
    server_key_path = internal_dir / "hushfilter-api.key"
    client_cert_path = internal_dir / "nginx-client.crt"
    client_key_path = internal_dir / "nginx-client.key"

    internal_files = (
        ca_cert_path,
        ca_key_path,
        server_cert_path,
        server_key_path,
        client_cert_path,
        client_key_path,
    )
    if all(path.exists() for path in internal_files):
        print(f"Using existing internal mTLS certificates: {internal_dir}")
        return
    if any(path.exists() for path in internal_files):
        raise RuntimeError(
            "Internal mTLS certificate set is incomplete. Replace the full set or remove "
            f"{internal_dir} so defaults can be regenerated."
        )

    ca_key = _new_private_key()
    ca_subject = _name("HushFilter internal CA")
    ca_cert = (
        _certificate_builder(subject=ca_subject, issuer=ca_subject, public_key=ca_key.public_key(), valid_days=valid_days)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    server_key = _new_private_key()
    server_cert = _sign_leaf_cert(
        subject=_name("hushfilter-api"),
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        public_key=server_key.public_key(),
        valid_days=valid_days,
        names=server_names,
        eku=ExtendedKeyUsageOID.SERVER_AUTH,
    )

    client_key = _new_private_key()
    client_cert = _sign_leaf_cert(
        subject=_name("nginx-client"),
        issuer_cert=ca_cert,
        issuer_key=ca_key,
        public_key=client_key.public_key(),
        valid_days=valid_days,
        names=("nginx-client",),
        eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )

    _write_private_key(ca_key_path, ca_key)
    _write_certificate(ca_cert_path, ca_cert)
    _write_private_key(server_key_path, server_key)
    _write_certificate(server_cert_path, server_cert)
    _write_private_key(client_key_path, client_key)
    _write_certificate(client_cert_path, client_cert)
    print(f"Generated self-signed internal mTLS certificate set: {internal_dir}")


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
) -> x509.CertificateBuilder:
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=max(valid_days, 1)))
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
        .sign(private_key=issuer_key, algorithm=hashes.SHA256())
    )


def _subject_alt_names(names: tuple[str, ...]) -> x509.SubjectAlternativeName:
    general_names: list[x509.GeneralName] = []
    for name in names:
        try:
            general_names.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            general_names.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(general_names)


def _write_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def _write_certificate(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    path.chmod(0o644)


if __name__ == "__main__":
    raise SystemExit(main())
