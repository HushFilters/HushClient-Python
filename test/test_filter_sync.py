from __future__ import annotations

import hashlib
import io
import json
import logging
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import requests

from filter_sync.r2_client import R2ClientError
from filter_sync.sync import (
    SyncError,
    _build_r2_downloader,
    _fetch_r2_config_from_nwebbed,
    sync_filters,
)


class FakeDownloader:
    def __init__(
        self,
        manifest_payload: str,
        objects: dict[str, bytes],
        *,
        text_objects: dict[str, str] | None = None,
    ) -> None:
        self._manifest_payload = manifest_payload
        self._objects = objects
        self._text_objects = text_objects or {}
        self.text_requests: list[str] = []
        self.file_requests: list[str] = []

    def download_text(self, object_key: str) -> str:
        self.text_requests.append(object_key)
        if object_key in self._text_objects:
            return self._text_objects[object_key]
        return self._manifest_payload

    def download_file(self, object_key: str, destination: Path) -> None:
        self.file_requests.append(object_key)
        destination.write_bytes(self._objects[object_key])


class FakeCredentialResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload


class FakeCredentialSession:
    def __init__(self, response: FakeCredentialResponse | dict[str, FakeCredentialResponse]) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, json: dict[str, str], timeout: int) -> FakeCredentialResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(self._response, dict):
            return self._response[url]
        return self._response


def _make_zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for archive_path, content in files.items():
            archive.writestr(archive_path, content)
    return buffer.getvalue()


def _make_upload_manifest_payload(
    zip_filename: str,
    zip_md5: str,
    contents: list[dict[str, str]],
) -> str:
    return json.dumps(
        {
            "generated_at": "2026-04-13T03:50:41.1609142Z",
            "files": [
                {
                    "filename": zip_filename,
                    "md5": zip_md5,
                    "contents": contents,
                },
                {
                    "filename": f"filter_insert_counts_{zip_filename.removesuffix('.zip')}.csv",
                    "md5": hashlib.md5(b"csv").hexdigest(),
                },
            ],
        }
    )


def test_sync_filters_downloads_unpacks_and_verifies_filters(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    base_dir = tmp_path / "filter_sync"
    base_dir.mkdir()

    existing_filters = {
        "00_20010101_20260401.hf": b"kept-first-filter",
        "01_20010101_20260401.hf": b"kept-second-filter",
    }
    downloaded_filters = {
        "00_20260401_20260408.hf": b"first-filter",
        "01_20260401_20260408.hf": b"second-filter",
    }
    downloaded_zip_bytes = _make_zip_bytes(downloaded_filters)

    manifest_payload = json.dumps(
        {
            "current_filter_locations": [
                "202604/20010101_20260401",
                "202604/20260401_20260408",
            ],
            "current_filter_zips": [
                {
                    "path": "202604/20010101_20260401/20010101_20260401.zip",
                    "md5": hashlib.md5(b"unused-existing-zip").hexdigest(),
                },
                {
                    "path": "202604/20260401_20260408/20260401_20260408.zip",
                    "md5": hashlib.md5(downloaded_zip_bytes).hexdigest(),
                },
            ],
            "current_filter_files": [
                {
                    "path": "20260401_20260408/00_20260401_20260408.hf",
                    "md5": hashlib.md5(downloaded_filters["00_20260401_20260408.hf"]).hexdigest(),
                },
                {
                    "path": "20260401_20260408/01_20260401_20260408.hf",
                    "md5": hashlib.md5(downloaded_filters["01_20260401_20260408.hf"]).hexdigest(),
                },
            ],
        }
    )

    filters_dir = tmp_path / "filters"
    keep_dir = filters_dir / "202604" / "20010101_20260401"
    keep_dir.mkdir(parents=True, exist_ok=True)
    keep_zip = keep_dir / "20010101_20260401.zip"
    (keep_dir / "00_20010101_20260401.hf").write_bytes(existing_filters["00_20010101_20260401.hf"])
    (keep_dir / "01_20010101_20260401.hf").write_bytes(existing_filters["01_20010101_20260401.hf"])

    downloader = FakeDownloader(
        manifest_payload=manifest_payload,
        objects={
            "filters/202604/20260401_20260408/20260401_20260408.zip": downloaded_zip_bytes,
        },
        text_objects={
            "filters/202604/20010101_20260401/upload_manifest.json": _make_upload_manifest_payload(
                "20010101_20260401.zip",
                hashlib.md5(b"unused-existing-zip").hexdigest(),
                [
                    {
                        "filename": "00_20010101_20260401.hf",
                        "md5": hashlib.md5(existing_filters["00_20010101_20260401.hf"]).hexdigest(),
                    },
                    {
                        "filename": "01_20010101_20260401.hf",
                        "md5": hashlib.md5(existing_filters["01_20010101_20260401.hf"]).hexdigest(),
                    },
                ],
            ),
        },
    )

    result = sync_filters(base_dir=base_dir, downloader=downloader)

    downloaded_zip = (
        filters_dir / "202604" / "20260401_20260408" / "20260401_20260408.zip"
    ).resolve()
    extracted_file_one = (
        filters_dir / "202604" / "20260401_20260408" / "00_20260401_20260408.hf"
    ).resolve()
    extracted_file_two = (
        filters_dir / "202604" / "20260401_20260408" / "01_20260401_20260408.hf"
    ).resolve()

    assert downloader.text_requests == [
        "filters/manifest_current.json",
        "filters/202604/20010101_20260401/upload_manifest.json",
        "filters/202604/20260401_20260408/upload_manifest.json",
    ]
    assert downloader.file_requests == [
        "filters/202604/20260401_20260408/20260401_20260408.zip",
    ]
    assert result.verified_existing == (keep_zip.resolve(),)
    assert result.redownloaded == ()
    assert result.downloaded == (downloaded_zip,)
    assert not downloaded_zip.exists()
    assert extracted_file_one.read_bytes() == downloaded_filters["00_20260401_20260408.hf"]
    assert extracted_file_two.read_bytes() == downloaded_filters["01_20260401_20260408.hf"]
    assert (filters_dir / "manifest_current.json").read_text(encoding="utf-8") == manifest_payload
    assert "starting filter md5 verification" in caplog.text
    assert "finished filter md5 verification" in caplog.text
    assert "source=filter" not in caplog.text


def test_sync_filters_logs_and_stops_on_zip_md5_mismatch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    base_dir = tmp_path / "filter_sync"
    base_dir.mkdir()

    zip_bytes = _make_zip_bytes({"00_20260401_20260408.hf": b"payload"})
    manifest_payload = json.dumps(
        {
            "current_filter_zips": [
                {
                    "path": "202604/20260401_20260408/20260401_20260408.zip",
                    "md5": hashlib.md5(b"not-the-zip").hexdigest(),
                }
            ],
            "current_filter_files": [
                {
                    "path": "20260401_20260408/00_20260401_20260408.hf",
                    "md5": hashlib.md5(b"payload").hexdigest(),
                }
            ],
        }
    )
    downloader = FakeDownloader(
        manifest_payload=manifest_payload,
        objects={
            "filters/202604/20260401_20260408/20260401_20260408.zip": zip_bytes,
        },
        text_objects={
            "filters/202604/20260401_20260408/upload_manifest.json": _make_upload_manifest_payload(
                "20260401_20260408.zip",
                hashlib.md5(zip_bytes).hexdigest(),
                [
                    {
                        "filename": "00_20260401_20260408.hf",
                        "md5": hashlib.md5(b"payload").hexdigest(),
                    }
                ],
            ),
        },
    )

    with pytest.raises(SyncError, match="MD5 mismatch"):
        sync_filters(base_dir=base_dir, downloader=downloader)

    assert downloader.text_requests == [
        "filters/manifest_current.json",
        "filters/202604/20260401_20260408/upload_manifest.json",
    ]
    assert "Checking local extracted filters against filters/202604/20260401_20260408/upload_manifest.json" in caplog.text
    assert "ZIP MD5 mismatch" in caplog.text
    assert "local_filter_missing" in caplog.text
    assert "starting filter md5 verification" not in caplog.text
    assert not (
        tmp_path / "filters" / "202604" / "20260401_20260408" / "00_20260401_20260408.hf"
    ).exists()


def test_sync_filters_logs_filter_verification_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    base_dir = tmp_path / "filter_sync"
    base_dir.mkdir()

    zip_bytes = _make_zip_bytes({"00_20260401_20260408.hf": b"actual-filter"})
    manifest_payload = json.dumps(
        {
            "current_filter_zips": [
                {
                    "path": "202604/20260401_20260408/20260401_20260408.zip",
                    "md5": hashlib.md5(zip_bytes).hexdigest(),
                }
            ],
            "current_filter_files": [
                {
                    "path": "20260401_20260408/00_20260401_20260408.hf",
                    "md5": hashlib.md5(b"different-filter").hexdigest(),
                }
            ],
        }
    )
    downloader = FakeDownloader(
        manifest_payload=manifest_payload,
        objects={
            "filters/202604/20260401_20260408/20260401_20260408.zip": zip_bytes,
        },
        text_objects={
            "filters/202604/20260401_20260408/upload_manifest.json": _make_upload_manifest_payload(
                "20260401_20260408.zip",
                hashlib.md5(zip_bytes).hexdigest(),
                [
                    {
                        "filename": "00_20260401_20260408.hf",
                        "md5": hashlib.md5(b"different-filter").hexdigest(),
                    }
                ],
            ),
        },
    )

    with pytest.raises(SyncError, match="Filter MD5 mismatch"):
        sync_filters(base_dir=base_dir, downloader=downloader)

    assert downloader.text_requests == [
        "filters/manifest_current.json",
        "filters/202604/20260401_20260408/upload_manifest.json",
    ]
    assert "Checking local extracted filters against filters/202604/20260401_20260408/upload_manifest.json" in caplog.text
    assert (
        tmp_path / "filters" / "202604" / "20260401_20260408" / "20260401_20260408.zip"
    ).exists()
    assert "starting filter md5 verification" in caplog.text
    assert "finished filter md5 verification" not in caplog.text
    assert "Filter MD5 verification failed" in caplog.text


def test_sync_filters_rejects_unsafe_zip_paths(tmp_path: Path) -> None:
    base_dir = tmp_path / "filter_sync"
    base_dir.mkdir()
    manifest_payload = json.dumps(
        {
            "current_filter_zips": [
                {
                    "path": "../outside.zip",
                    "md5": hashlib.md5(b"bad").hexdigest(),
                }
            ]
        }
    )
    downloader = FakeDownloader(manifest_payload=manifest_payload, objects={})

    with pytest.raises(SyncError, match="unsafe"):
        sync_filters(base_dir=base_dir, downloader=downloader)


def test_sync_filters_skips_download_when_local_filters_match_upload_manifest(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    base_dir = tmp_path / "filter_sync"
    base_dir.mkdir()

    filters_dir = tmp_path / "filters"
    local_dir = filters_dir / "202604" / "20260401_20260408"
    local_dir.mkdir(parents=True)
    first_filter = local_dir / "00_20260401_20260408.hf"
    second_filter = local_dir / "01_20260401_20260408.hf"
    first_filter.write_bytes(b"first-filter")
    second_filter.write_bytes(b"second-filter")

    manifest_payload = json.dumps(
        {
            "current_filter_zips": [
                {
                    "path": "202604/20260401_20260408/20260401_20260408.zip",
                    "md5": hashlib.md5(b"zip-bytes-not-needed").hexdigest(),
                }
            ],
            "current_filter_files": [
                {
                    "path": "20260401_20260408/00_20260401_20260408.hf",
                    "md5": hashlib.md5(b"first-filter").hexdigest(),
                },
                {
                    "path": "20260401_20260408/01_20260401_20260408.hf",
                    "md5": hashlib.md5(b"second-filter").hexdigest(),
                },
            ],
        }
    )
    upload_manifest_payload = _make_upload_manifest_payload(
        "20260401_20260408.zip",
        hashlib.md5(b"zip-bytes-not-needed").hexdigest(),
        [
            {
                "filename": "00_20260401_20260408.hf",
                "md5": hashlib.md5(b"first-filter").hexdigest(),
            },
            {
                "filename": "01_20260401_20260408.hf",
                "md5": hashlib.md5(b"second-filter").hexdigest(),
            },
        ],
    )
    downloader = FakeDownloader(
        manifest_payload=manifest_payload,
        objects={},
        text_objects={
            "filters/202604/20260401_20260408/upload_manifest.json": upload_manifest_payload,
        },
    )

    result = sync_filters(base_dir=base_dir, downloader=downloader)

    assert result.downloaded == ()
    assert result.redownloaded == ()
    assert downloader.file_requests == []
    assert downloader.text_requests == [
        "filters/manifest_current.json",
        "filters/202604/20260401_20260408/upload_manifest.json",
    ]
    assert "Checking local extracted filters against filters/202604/20260401_20260408/upload_manifest.json" in caplog.text
    assert "All filter md5s matched for" in caplog.text
    assert "skipping zip re-download" in caplog.text


def test_fetch_r2_config_from_nwebbed_uses_api_key_exchange() -> None:
    response = FakeCredentialResponse(
        {
            "R2_ENDPOINT": "https://example.r2.cloudflarestorage.com/",
            "R2_ACCESS_KEY_ID": "access-key",
            "R2_SECRET_ACCESS_KEY": "secret-key",
        }
    )
    session = FakeCredentialSession(response)

    config = _fetch_r2_config_from_nwebbed(
        {
            "NWEBBED_API_KEY": "test-api-key",
            "NWEBBED_API_URL": "https://nwebbed.example.com/r2",
        },
        bucket="hushfilters",
        session=session,
    )

    assert session.calls == [
        {
            "url": "https://nwebbed.example.com/r2",
            "json": {"api_key": "test-api-key"},
            "timeout": 60,
        }
    ]
    assert config.endpoint == "https://example.r2.cloudflarestorage.com"
    assert config.access_key_id == "access-key"
    assert config.secret_access_key == "secret-key"
    assert config.bucket == "hushfilters"


def test_build_r2_downloader_prefers_direct_r2_settings(monkeypatch, tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "R2_ENDPOINT=https://example.r2.cloudflarestorage.com/",
                "R2_ACCESS_KEY_ID=access-key",
                "R2_SECRET_ACCESS_KEY=secret-key",
                "NWEBBED_API_KEY=test-api-key",
                "NWEBBED_API_URL=https://nwebbed.example.com/r2",
            ]
        ),
        encoding="utf-8",
    )

    def _unexpected_fetch(*args, **kwargs):
        raise AssertionError("nWebbed credential exchange should not be called")

    monkeypatch.setattr("filter_sync.sync._fetch_r2_config_from_nwebbed", _unexpected_fetch)

    downloader = _build_r2_downloader(
        env_path=env_path,
        bucket="hushfilters",
        project_root=tmp_path,
    )

    assert downloader._config.endpoint == "https://example.r2.cloudflarestorage.com"
    assert downloader._config.access_key_id == "access-key"
    assert downloader._config.secret_access_key == "secret-key"
    assert downloader._config.bucket == "hushfilters"


def test_fetch_r2_config_from_nwebbed_rejects_incomplete_response() -> None:
    response = FakeCredentialResponse({"endpoint": "https://example.r2.cloudflarestorage.com"})
    session = FakeCredentialSession(response)

    with pytest.raises(R2ClientError, match="missing R2 endpoint"):
        _fetch_r2_config_from_nwebbed(
            {
                "NWEBBED_API_KEY": "test-api-key",
                "NWEBBED_API_URL": "https://nwebbed.example.com/r2",
            },
            bucket="hushfilters",
            session=session,
        )


def test_fetch_r2_config_from_nwebbed_retries_r2_suffix_for_base_url() -> None:
    session = FakeCredentialSession(
        {
            "https://nwebbed.example.com": FakeCredentialResponse({}, status_code=404),
            "https://nwebbed.example.com/r2": FakeCredentialResponse(
                {
                    "R2_ENDPOINT": "https://example.r2.cloudflarestorage.com/",
                    "R2_ACCESS_KEY_ID": "access-key",
                    "R2_SECRET_ACCESS_KEY": "secret-key",
                }
            ),
        }
    )

    config = _fetch_r2_config_from_nwebbed(
        {
            "NWEBBED_API_KEY": "test-api-key",
            "NWEBBED_API_URL": "https://nwebbed.example.com",
        },
        bucket="hushfilters",
        session=session,
    )

    assert [call["url"] for call in session.calls] == [
        "https://nwebbed.example.com",
        "https://nwebbed.example.com/r2",
    ]
    assert config.endpoint == "https://example.r2.cloudflarestorage.com"
