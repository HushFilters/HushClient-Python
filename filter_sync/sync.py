from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
from zipfile import ZipFile

import requests

from .r2_client import R2Client, R2ClientError, R2Config

DEFAULT_BUCKET = "hushfilters"
REMOTE_FILTERS_PREFIX = "filters"
REMOTE_MANIFEST_NAME = "manifest_current.json"
REMOTE_UPLOAD_MANIFEST_NAME = "upload_manifest.json"

logger = logging.getLogger(__name__)


class SyncError(RuntimeError):
    """Raised when manifest or local filter sync validation fails."""


@dataclass(frozen=True)
class ManifestZip:
    path: PurePosixPath
    md5: str

    @classmethod
    def from_dict(cls, values: dict[str, str]) -> "ManifestZip":
        raw_path = values.get("path", "").strip()
        md5 = values.get("md5", "").strip().lower()
        if not raw_path:
            raise SyncError("Manifest zip entry is missing a path")
        if not md5:
            raise SyncError(f"Manifest zip entry {raw_path!r} is missing an md5")

        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts:
            raise SyncError(f"Manifest zip path is unsafe: {raw_path!r}")
        if path.suffix.lower() != ".zip":
            raise SyncError(f"Manifest zip path must end in .zip: {raw_path!r}")

        return cls(path=path, md5=md5)


@dataclass(frozen=True)
class ManifestFilterFile:
    path: PurePosixPath
    md5: str

    @classmethod
    def from_dict(cls, values: dict[str, str]) -> "ManifestFilterFile":
        raw_path = values.get("path", "").strip()
        md5 = values.get("md5", "").strip().lower()
        if not raw_path:
            raise SyncError("Manifest filter entry is missing a path")
        if not md5:
            raise SyncError(f"Manifest filter entry {raw_path!r} is missing an md5")

        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts:
            raise SyncError(f"Manifest filter path is unsafe: {raw_path!r}")
        if path.suffix.lower() != ".hf":
            raise SyncError(f"Manifest filter path must end in .hf: {raw_path!r}")

        return cls(path=path, md5=md5)


@dataclass(frozen=True)
class FilterManifest:
    current_filter_locations: tuple[str, ...]
    current_filter_zips: tuple[ManifestZip, ...]
    current_filter_files: tuple[ManifestFilterFile, ...]

    @classmethod
    def from_json(cls, payload: str) -> "FilterManifest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SyncError("Downloaded manifest is not valid JSON") from exc

        locations = tuple(data.get("current_filter_locations", []))
        zip_entries = tuple(
            ManifestZip.from_dict(entry)
            for entry in data.get("current_filter_zips", [])
        )
        filter_entries = tuple(
            ManifestFilterFile.from_dict(entry)
            for entry in data.get("current_filter_files", [])
        )
        if not zip_entries:
            raise SyncError("Manifest does not contain any current_filter_zips entries")

        return cls(
            current_filter_locations=locations,
            current_filter_zips=zip_entries,
            current_filter_files=filter_entries,
        )

    def filter_files_for_location(
        self,
        location_name: str,
    ) -> tuple[ManifestFilterFile, ...]:
        return tuple(
            entry
            for entry in self.current_filter_files
            if entry.path.parts and entry.path.parts[0] == location_name
        )


@dataclass(frozen=True)
class UploadManifest:
    filter_files: tuple[ManifestFilterFile, ...]

    @classmethod
    def from_json(cls, payload: str, *, zip_filename: str | None = None) -> "UploadManifest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SyncError("Downloaded upload manifest is not valid JSON") from exc

        entries = tuple(_parse_upload_manifest_entries(data, zip_filename=zip_filename))
        if not entries:
            raise SyncError("Upload manifest does not contain any filter file entries")
        return cls(filter_files=entries)


@dataclass(frozen=True)
class SyncResult:
    manifest_path: Path
    filters_dir: Path
    downloaded: tuple[Path, ...]
    redownloaded: tuple[Path, ...]
    verified_existing: tuple[Path, ...]


class ObjectDownloader(Protocol):
    def download_text(self, object_key: str) -> str: ...

    def download_file(self, object_key: str, destination: Path) -> None: ...


def sync_filters(
    *,
    base_dir: Path | None = None,
    env_path: Path | None = None,
    bucket: str = DEFAULT_BUCKET,
    downloader: ObjectDownloader | None = None,
) -> SyncResult:
    resolved_base_dir = (
        base_dir if base_dir is not None else Path(__file__).resolve().parent
    )
    project_root = resolved_base_dir.parent
    filters_dir = project_root / "filters"
    filters_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = filters_dir / REMOTE_MANIFEST_NAME

    active_downloader = downloader or _build_r2_downloader(
        env_path=env_path,
        bucket=bucket,
        project_root=project_root,
    )

    remote_manifest_key = f"{REMOTE_FILTERS_PREFIX}/{REMOTE_MANIFEST_NAME}"
    manifest_payload = active_downloader.download_text(remote_manifest_key)
    manifest = FilterManifest.from_json(manifest_payload)
    _write_text_atomic(manifest_path, manifest_payload)

    downloaded: list[Path] = []
    redownloaded: list[Path] = []
    verified_existing: list[Path] = []
    downloaded_zip_paths: list[Path] = []

    for entry in manifest.current_filter_zips:
        local_zip_path = _safe_local_path(filters_dir, entry.path)
        remote_object_key = f"{REMOTE_FILTERS_PREFIX}/{entry.path.as_posix()}"

        if _location_filters_match_remote_manifest(
            downloader=active_downloader,
            local_zip_path=local_zip_path,
            zip_manifest_path=entry.path,
        ):
            logger.info(
                "All filter md5s matched for %s; skipping zip re-download",
                local_zip_path,
            )
            verified_existing.append(local_zip_path)
            continue

        existed_before_download = local_zip_path.exists()
        _download_verified_file(
            downloader=active_downloader,
            remote_object_key=remote_object_key,
            destination=local_zip_path,
            expected_md5=entry.md5,
        )
        if existed_before_download:
            redownloaded.append(local_zip_path)
        else:
            downloaded.append(local_zip_path)
        downloaded_zip_paths.append(local_zip_path)

    logger.info("starting filter md5 verification")
    for zip_path in downloaded_zip_paths:
        _extract_zip_archive(zip_path, zip_path.parent)
        _verify_filters_for_downloaded_zip(manifest=manifest, zip_path=zip_path)
        zip_path.unlink()
    logger.info("finished filter md5 verification")

    return SyncResult(
        manifest_path=manifest_path,
        filters_dir=filters_dir,
        downloaded=tuple(downloaded),
        redownloaded=tuple(redownloaded),
        verified_existing=tuple(verified_existing),
    )


def calculate_md5(file_path: Path) -> str:
    digest = hashlib.md5()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _log_md5_check(
    *,
    path: Path,
    expected_md5: str,
    actual_md5: str,
    source: str,
) -> None:
    logger.info(
        "MD5 check source=%s path=%s expected=%s actual=%s match=%s",
        source,
        path,
        expected_md5,
        actual_md5,
        actual_md5 == expected_md5,
    )


def _log_filter_md5_failure(
    *,
    path: Path,
    expected_md5: str,
    actual_md5: str | None,
    reason: str,
) -> None:
    logger.error(
        "Filter MD5 verification failed path=%s expected=%s actual=%s reason=%s",
        path,
        expected_md5,
        actual_md5,
        reason,
    )


def _log_upload_manifest_check_failure(
    *,
    zip_path: Path,
    upload_manifest_key: str,
    target_path: Path | None = None,
    manifest_filter_path: PurePosixPath | None = None,
    expected_md5: str | None = None,
    actual_md5: str | None = None,
    reason: str,
) -> None:
    logger.warning(
        "Local filter reuse check failed zip=%s upload_manifest=%s manifest_filter=%s local_filter=%s expected=%s actual=%s reason=%s",
        zip_path,
        upload_manifest_key,
        manifest_filter_path,
        target_path,
        expected_md5,
        actual_md5,
        reason,
    )


def _download_verified_file(
    *,
    downloader: ObjectDownloader,
    remote_object_key: str,
    destination: Path,
    expected_md5: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.parent / f".{destination.name}.part"
    if temp_path.exists():
        temp_path.unlink()

    try:
        downloader.download_file(remote_object_key, temp_path)
        actual_md5 = calculate_md5(temp_path)
        _log_md5_check(
            path=destination,
            expected_md5=expected_md5,
            actual_md5=actual_md5,
            source="downloaded",
        )
        if actual_md5 != expected_md5:
            logger.error(
                "ZIP MD5 mismatch path=%s expected=%s actual=%s",
                destination,
                expected_md5,
                actual_md5,
            )
            raise SyncError(
                f"MD5 mismatch for {destination}: expected {expected_md5}, got {actual_md5}"
            )
        temp_path.replace(destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _extract_zip_archive(zip_path: Path, destination_dir: Path) -> None:
    with ZipFile(zip_path) as archive:
        for member in archive.infolist():
            member_path = PurePosixPath(member.filename)
            if not member.filename:
                continue
            if member_path.is_absolute() or ".." in member_path.parts:
                raise SyncError(f"Unsafe zip member path in {zip_path}: {member.filename!r}")

            target_path = _safe_local_path(destination_dir, member_path)
            if member.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target_path.open("wb") as destination:
                shutil.copyfileobj(source, destination)


def _location_filters_match_remote_manifest(
    *,
    downloader: ObjectDownloader,
    local_zip_path: Path,
    zip_manifest_path: PurePosixPath,
) -> bool:
    upload_manifest_key = (
        f"{REMOTE_FILTERS_PREFIX}/"
        f"{zip_manifest_path.parent.joinpath(REMOTE_UPLOAD_MANIFEST_NAME).as_posix()}"
    )
    logger.info(
        "Checking local extracted filters against %s for %s",
        upload_manifest_key,
        local_zip_path,
    )
    try:
        upload_manifest_payload = downloader.download_text(upload_manifest_key)
    except Exception as exc:
        _log_upload_manifest_check_failure(
            zip_path=local_zip_path,
            upload_manifest_key=upload_manifest_key,
            reason=f"upload_manifest_download_failed: {exc}",
        )
        return False

    try:
        upload_manifest = UploadManifest.from_json(
            upload_manifest_payload,
            zip_filename=zip_manifest_path.name,
        )
    except SyncError as exc:
        _log_upload_manifest_check_failure(
            zip_path=local_zip_path,
            upload_manifest_key=upload_manifest_key,
            reason=f"upload_manifest_parse_failed: {exc}",
        )
        return False

    location_dir = local_zip_path.parent
    for entry in upload_manifest.filter_files:
        target_path = _resolve_filter_output_path(location_dir, entry.path)
        if not target_path.exists():
            _log_upload_manifest_check_failure(
                zip_path=local_zip_path,
                upload_manifest_key=upload_manifest_key,
                target_path=target_path,
                manifest_filter_path=entry.path,
                expected_md5=entry.md5,
                actual_md5=None,
                reason="local_filter_missing",
            )
            return False
        actual_md5 = calculate_md5(target_path)
        if actual_md5 != entry.md5:
            _log_upload_manifest_check_failure(
                zip_path=local_zip_path,
                upload_manifest_key=upload_manifest_key,
                target_path=target_path,
                manifest_filter_path=entry.path,
                expected_md5=entry.md5,
                actual_md5=actual_md5,
                reason="local_filter_md5_mismatch",
            )
            return False
    return True


def _verify_filters_for_downloaded_zip(
    *,
    manifest: FilterManifest,
    zip_path: Path,
) -> None:
    location_dir = zip_path.parent
    location_name = location_dir.name
    expected_files = manifest.filter_files_for_location(location_name)
    if not expected_files:
        logger.error("No filter manifest entries found for downloaded zip %s", zip_path)
        raise SyncError(f"No filter manifest entries found for downloaded zip {zip_path}")

    failures: list[str] = []
    for entry in expected_files:
        target_path = _resolve_filter_output_path(location_dir, entry.path)
        if not target_path.exists():
            _log_filter_md5_failure(
                path=target_path,
                expected_md5=entry.md5,
                actual_md5=None,
                reason="missing",
            )
            failures.append(f"Missing extracted filter: {target_path}")
            continue

        actual_md5 = calculate_md5(target_path)
        if actual_md5 != entry.md5:
            _log_filter_md5_failure(
                path=target_path,
                expected_md5=entry.md5,
                actual_md5=actual_md5,
                reason="mismatch",
            )
            failures.append(
                f"Filter MD5 mismatch for {target_path}: expected {entry.md5}, got {actual_md5}"
            )

    if failures:
        raise SyncError("; ".join(failures))


def _resolve_filter_output_path(
    location_dir: Path,
    manifest_path: PurePosixPath,
) -> Path:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add_candidate(candidate_parts: tuple[str, ...]) -> None:
        candidate = _safe_local_path(location_dir, PurePosixPath(*candidate_parts))
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    if manifest_path.parts:
        add_candidate((manifest_path.name,))
        add_candidate(tuple(manifest_path.parts))

        if len(manifest_path.parts) >= 2:
            add_candidate(tuple(manifest_path.parts[1:]))

        location_name = location_dir.name
        if location_name in manifest_path.parts:
            location_index = manifest_path.parts.index(location_name)
            trailing_parts = manifest_path.parts[location_index + 1 :]
            if trailing_parts:
                add_candidate(tuple(trailing_parts))

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else location_dir / manifest_path.name


def _parse_upload_manifest_entries(payload: object, *, zip_filename: str | None = None):
    if isinstance(payload, list):
        for entry in payload:
            parsed_entry = _manifest_filter_file_from_upload_entry(entry)
            if parsed_entry is not None:
                yield parsed_entry
        return

    if not isinstance(payload, dict):
        return

    for key in ("contents", "current_filter_files", "files", "filters"):
        entries = payload.get(key)
        if isinstance(entries, list):
            if key == "files":
                yield from _parse_upload_manifest_file_entries(entries, zip_filename=zip_filename)
                return
            for entry in entries:
                parsed_entry = _manifest_filter_file_from_upload_entry(entry)
                if parsed_entry is not None:
                    yield parsed_entry
            return


def _parse_upload_manifest_file_entries(entries: list[object], *, zip_filename: str | None = None):
    matched_contents_found = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        contents = entry.get("contents")
        if not isinstance(contents, list):
            continue

        filename = entry.get("filename")
        if zip_filename is not None and isinstance(filename, str) and filename != zip_filename:
            continue

        matched_contents_found = True
        for content_entry in contents:
            parsed_entry = _manifest_filter_file_from_upload_entry(content_entry)
            if parsed_entry is not None:
                yield parsed_entry

    if matched_contents_found:
        return

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        parsed_entry = _manifest_filter_file_from_upload_entry(entry)
        if parsed_entry is not None:
            yield parsed_entry


def _manifest_filter_file_from_upload_entry(entry: object) -> ManifestFilterFile | None:
    if not isinstance(entry, dict):
        return None

    raw_path = ""
    for key in ("path", "file_path", "filename", "file_name", "name", "file"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            raw_path = value.strip()
            break

    md5 = entry.get("md5")
    if not isinstance(md5, str) or not md5.strip():
        raise SyncError(f"Upload manifest entry is missing an md5: {entry!r}")
    if not raw_path:
        raise SyncError(f"Upload manifest entry is missing a path-like field: {entry!r}")
    if not raw_path.lower().endswith(".hf"):
        return None

    return ManifestFilterFile.from_dict({"path": raw_path, "md5": md5})


def _build_r2_downloader(
    *,
    env_path: Path | None,
    bucket: str,
    project_root: Path,
) -> R2Client:
    settings = dict(_load_dotenv(project_root / ".env"))
    settings.update({key: value for key, value in os.environ.items() if value})

    if env_path is not None:
        settings.update(_load_dotenv(env_path))

    if _has_direct_r2_settings(settings):
        config = R2Config.from_mapping(settings, bucket=bucket)
    else:
        config = _fetch_r2_config_from_nwebbed(settings, bucket=bucket)
    return R2Client(config)


def _has_direct_r2_settings(settings: dict[str, str]) -> bool:
    required_keys = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    return all(settings.get(key, "").strip() for key in required_keys)


def _fetch_r2_config_from_nwebbed(
    settings: dict[str, str],
    *,
    bucket: str,
    session: requests.Session | None = None,
) -> R2Config:
    try:
        api_key = settings["NWEBBED_API_KEY"].strip()
        api_url = settings["NWEBBED_API_URL"].strip()
    except KeyError as exc:
        missing_key = exc.args[0]
        raise R2ClientError(f"Missing required nWebbed setting: {missing_key}") from exc

    if not api_key or not api_url:
        raise R2ClientError("NWEBBED_API_KEY and NWEBBED_API_URL must not be empty")

    active_session = session or requests.Session()
    candidate_urls = _credential_api_urls(api_url)
    last_error: requests.RequestException | None = None
    response = None
    for candidate_url in candidate_urls:
        try:
            response = active_session.post(
                candidate_url,
                json={"api_key": api_key},
                timeout=60,
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
    if response is None:
        assert last_error is not None
        raise R2ClientError(
            f"Failed to fetch R2 credentials from nWebbed: {last_error}"
        ) from last_error

    try:
        payload = response.json()
    except ValueError as exc:
        raise R2ClientError("nWebbed credential response was not valid JSON") from exc

    return R2Config(
        endpoint=_extract_payload_value(
            payload,
            "R2 endpoint",
            "R2_ENDPOINT",
        ).rstrip("/"),
        access_key_id=_extract_payload_value(
            payload,
            "R2 access key ID",
            "R2_ACCESS_KEY_ID",
        ),
        secret_access_key=_extract_payload_value(
            payload,
            "R2 secret access key",
            "R2_SECRET_ACCESS_KEY",
        ),
        bucket=bucket,
    )


def _credential_api_urls(api_url: str) -> tuple[str, ...]:
    normalized = api_url.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if not parsed.scheme or not parsed.netloc:
        return (normalized,)

    if parsed.path.endswith("/r2") or parsed.path == "/r2":
        return (normalized,)

    if parsed.path in {"", "/"}:
        r2_url = urlunsplit((parsed.scheme, parsed.netloc, "/r2", parsed.query, parsed.fragment))
        return (normalized, r2_url)

    r2_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{parsed.path.rstrip('/')}/r2",
            parsed.query,
            parsed.fragment,
        )
    )
    return (normalized, r2_url)


def _extract_payload_value(
    payload: object,
    label: str,
    *keys: str,
) -> str:
    if not isinstance(payload, dict):
        raise R2ClientError("nWebbed credential response must be a JSON object")

    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    joined_keys = ", ".join(keys)
    available_keys = ", ".join(sorted(payload.keys())) if payload else "(none)"
    raise R2ClientError(
        f"nWebbed credential response is missing {label} ({joined_keys}); "
        f"top-level keys: {available_keys}"
    )


def _load_dotenv(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _safe_local_path(base_dir: Path, relative_path: PurePosixPath) -> Path:
    candidate = (base_dir / Path(*relative_path.parts)).resolve()
    base = base_dir.resolve()
    if candidate != base and base not in candidate.parents:
        raise SyncError(f"Refusing to write outside filters directory: {relative_path}")
    return candidate


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.part"
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync filter zip archives from Cloudflare R2 into the repo filters directory",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Local filter_sync directory containing manifest_current.json",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional path to an env file containing R2 credentials",
    )
    parser.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
        help=f"R2 bucket name. Defaults to {DEFAULT_BUCKET}.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    try:
        result = sync_filters(
            base_dir=args.base_dir,
            env_path=args.env_file,
            bucket=args.bucket,
        )
    except (R2ClientError, SyncError) as exc:
        print(f"Sync failed: {exc}")
        return 1

    print(f"Manifest updated: {result.manifest_path}")
    print(f"Verified existing zips: {len(result.verified_existing)}")
    print(f"Downloaded zips: {len(result.downloaded)}")
    print(f"Re-downloaded zips: {len(result.redownloaded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
