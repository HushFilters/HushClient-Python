from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, urlsplit

import requests


class R2ClientError(RuntimeError):
    """Raised when the R2 client cannot authenticate or download objects."""


@dataclass(frozen=True)
class R2Config:
    endpoint: str
    access_key_id: str
    secret_access_key: str
    bucket: str = "hushfilters"
    region: str = "auto"
    service: str = "s3"

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        bucket: str = "hushfilters",
    ) -> "R2Config":
        try:
            endpoint = values["R2_ENDPOINT"].strip()
            access_key_id = values["R2_ACCESS_KEY_ID"].strip()
            secret_access_key = values["R2_SECRET_ACCESS_KEY"].strip()
        except KeyError as exc:
            missing_key = exc.args[0]
            raise R2ClientError(f"Missing required R2 setting: {missing_key}") from exc

        if not endpoint or not access_key_id or not secret_access_key:
            raise R2ClientError("R2 settings must not be empty")

        return cls(
            endpoint=endpoint.rstrip("/"),
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket=bucket,
        )


class R2Client:
    """Minimal signed GET client for Cloudflare R2's S3-compatible API."""

    def __init__(
        self,
        config: R2Config,
        *,
        session: requests.Session | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._timeout_seconds = timeout_seconds
        self._endpoint_parts = urlsplit(config.endpoint)
        if self._endpoint_parts.scheme not in {"http", "https"}:
            raise R2ClientError("R2 endpoint must use http or https")

    def download_text(self, object_key: str) -> str:
        response = self._get(object_key)
        return response.content.decode("utf-8")

    def download_file(self, object_key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self._get(object_key, stream=True)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

    def _get(self, object_key: str, *, stream: bool = False) -> requests.Response:
        normalized_key = object_key.lstrip("/")
        canonical_object_path = quote(normalized_key, safe="/-_.~")
        canonical_uri = f"/{self._config.bucket}/{canonical_object_path}"
        request_url = self._build_url(canonical_uri)
        headers = self._build_headers(canonical_uri)

        response = self._session.get(
            request_url,
            headers=headers,
            stream=stream,
            timeout=self._timeout_seconds,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise R2ClientError(
                f"Failed to download s3://{self._config.bucket}/{normalized_key}: "
                f"HTTP {response.status_code}"
            ) from exc
        return response

    def _build_url(self, canonical_uri: str) -> str:
        base_path = self._endpoint_parts.path.rstrip("/")
        return (
            f"{self._endpoint_parts.scheme}://{self._endpoint_parts.netloc}"
            f"{base_path}{canonical_uri}"
        )

    def _build_headers(self, canonical_uri: str) -> dict[str, str]:
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(b"").hexdigest()

        host = self._endpoint_parts.netloc
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [
                "GET",
                canonical_uri,
                "",
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )

        credential_scope = (
            f"{date_stamp}/{self._config.region}/{self._config.service}/aws4_request"
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = _derive_signing_key(
            self._config.secret_access_key,
            date_stamp,
            self._config.region,
            self._config.service,
        )
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._config.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        return {
            "Authorization": authorization,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }


def _sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _derive_signing_key(
    secret_access_key: str,
    date_stamp: str,
    region: str,
    service: str,
) -> bytes:
    key_date = _sign(f"AWS4{secret_access_key}".encode("utf-8"), date_stamp)
    key_region = _sign(key_date, region)
    key_service = _sign(key_region, service)
    return _sign(key_service, "aws4_request")
