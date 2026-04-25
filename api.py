"""
FastAPI application for HushFilter bloom filter checking.
Provides REST API endpoints for credential and hash membership checking.
"""
import asyncio
import json
import logging
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from contextlib import suppress
from contextlib import redirect_stderr, redirect_stdout
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from typing import List, Optional
from io import StringIO

from core.filter_core import FilterManager
from filter_sync.r2_client import R2ClientError
from filter_sync.sync import SyncError, sync_filters
from helpers.generate_manifest import generate_manifest


# Global filter manager
filter_manager: Optional[FilterManager] = None
operation_lock = threading.Lock()
logger = logging.getLogger(__name__)


class _SyncOperationLogState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._operation: str | None = None
        self._logs: list[str] = []

    def start(self, operation: str) -> None:
        with self._lock:
            self._active = True
            self._operation = operation
            self._logs = []

    def append(self, line: str) -> None:
        with self._lock:
            self._logs.append(line)

    def finish(self) -> None:
        with self._lock:
            self._active = False

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "active": self._active,
                "operation": self._operation,
                "logs": list(self._logs),
            }


class _LiveSyncLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            sync_operation_logs.append(self.format(record))
        except Exception:
            self.handleError(record)


sync_operation_logs = _SyncOperationLogState()


def _is_test_mode_enabled() -> bool:
    return os.getenv("HUSHFILTER_TEST_MODE", "").strip() == "1"


def _with_test_mode(payload: dict) -> dict:
    return {**payload, "test_mode": _is_test_mode_enabled()}


def _render_ui_page(page_name: str) -> HTMLResponse:
    page_path = Path("webui") / page_name / "index.html"
    html = page_path.read_text(encoding="utf-8")
    banner_html = ""
    if _is_test_mode_enabled():
        banner_html = (
            '<div class="test-mode-banner" role="status" aria-label="Test mode active">'
            "TEST MODE"
            "</div>"
        )
    return HTMLResponse(content=html.replace("{{TEST_MODE_BANNER}}", banner_html))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for the application."""
    global filter_manager
    auto_update_task: asyncio.Task[None] | None = None
    
    # Startup: Load filters
    manifest_path = _current_filter_configuration()
    
    try:
        filter_manager = FilterManager(manifest_path=manifest_path)
        print(f"Loaded {len(filter_manager.filters)} filters from {manifest_path}")
        if _is_test_mode_enabled():
            print("Running in TEST mode (using test_manifest.json)")
    except Exception as e:
        print(f"Error loading filters: {e}")
        raise

    if _is_auto_update_enabled():
        auto_update_hour = _configured_auto_update_hour()
        if auto_update_hour is None:
            logger.warning(
                "AUTO_UPDATE_FILTERS=1 but AUTO_UPDATE_TIME=%r is invalid; auto updater is disabled",
                os.getenv("AUTO_UPDATE_TIME"),
            )
        else:
            logger.info("Starting daily auto-update scheduler for hour %02d:00", auto_update_hour)
            auto_update_task = asyncio.create_task(_auto_update_scheduler_loop())

    yield

    if auto_update_task is not None:
        auto_update_task.cancel()
        with suppress(asyncio.CancelledError):
            await auto_update_task

    # Shutdown: Close filters
    if filter_manager:
        filter_manager.close()
        print("Closed all filters")


app = FastAPI(
    title="HushFilter API",
    description="Bloom filter based credential membership checking API",
    version="1.0.0",
    lifespan=lifespan
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_with_test_mode({"detail": exc.detail}),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_with_test_mode({"detail": exc.errors()}),
    )


@app.exception_handler(Exception)
async def unexpected_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_with_test_mode({"detail": f"Internal server error: {exc}"}),
    )

# Request/Response Models
class CheckRequest(BaseModel):
    """Request model for single credential check."""
    username: str = Field(..., description="Username to check")
    password: str = Field(default="", description="Password to check (optional)")


class BatchCheckRequest(BaseModel):
    """Request model for batch credential check."""
    credentials: List[CheckRequest] = Field(..., description="List of credentials to check")


class SingleHashCheckRequest(BaseModel):
    """Request model for single precomputed SHA-256 hash check."""
    hash: str = Field(..., description="Single SHA-256 hash (64-character hex string)")


class BatchHashCheckRequest(BaseModel):
    """Request model for batch SHA-256 hash checks."""
    hashes: List[str] = Field(..., description="List of SHA-256 hashes (64-character hex strings)")


class ApiResponse(BaseModel):
    """Base response model with test mode metadata."""
    test_mode: bool = Field(default_factory=_is_test_mode_enabled)


class CheckResponse(ApiResponse):
    """Response model for credential check."""
    # username: str
    # password: str
    found: bool
    # match_count: int
    matching_filters: List[str]


class BatchCheckResponse(ApiResponse):
    """Response model for batch credential check."""
    total: int
    found_usernames: List[str]


class BatchHashCheckResponse(ApiResponse):
    """Response model for batch SHA-256 hash checks."""
    total: int
    found_hashes: List[str]


class StatsResponse(ApiResponse):
    """Response model for stats endpoint."""
    filter_count: int
    filters: List[str]
    max_nk: int


class SyncFiltersResponse(ApiResponse):
    """Response model for filter sync operations."""
    success: bool
    manifest_path: Optional[str] = None
    downloaded: List[str] = Field(default_factory=list)
    redownloaded: List[str] = Field(default_factory=list)
    verified_existing: List[str] = Field(default_factory=list)
    logs: List[str] = Field(default_factory=list)
    detail: Optional[str] = None


class ManifestUpdateResponse(ApiResponse):
    """Response model for manifest regeneration."""
    success: bool
    output_file: Optional[str] = None
    filter_count: int = 0
    logs: List[str] = Field(default_factory=list)
    detail: Optional[str] = None


class ReloadFiltersResponse(ApiResponse):
    """Response model for reloading filters into memory."""
    success: bool
    filter_count: int = 0
    filters: List[str] = Field(default_factory=list)
    max_nk: int = 0
    logs: List[str] = Field(default_factory=list)
    detail: Optional[str] = None


class SyncApplyResponse(ApiResponse):
    """Response model for sync + manifest update + reload sequence."""
    success: bool
    manifest_path: Optional[str] = None
    output_file: Optional[str] = None
    downloaded: List[str] = Field(default_factory=list)
    redownloaded: List[str] = Field(default_factory=list)
    verified_existing: List[str] = Field(default_factory=list)
    filter_count: int = 0
    filters: List[str] = Field(default_factory=list)
    max_nk: int = 0
    logs: List[str] = Field(default_factory=list)
    detail: Optional[str] = None


class SyncStatusResponse(ApiResponse):
    """Response model for live sync status polling."""
    active: bool
    operation: Optional[str] = None
    logs: List[str] = Field(default_factory=list)


# API Endpoints
@app.get("/", tags=["General"])
async def root():
    """Root endpoint with API information."""
    return _with_test_mode({
        "name": "HushFilter API",
        "version": "1.0.0",
        "endpoints": {
            "ui_check": "/ui-check",
            "ui_sync": "/ui-sync",
            "health": "/health",
            "stats": "/stats",
            "check": "/check",
            "batch_check": "/check/batch",
            "single_hash_check": "/checkhash",
            "batch_hash_check": "/checkhash/batch",
            "sync_filters": "/sync/filters",
            "sync_apply": "/sync/apply",
            "sync_status": "/sync/status",
            "update_manifest": "/sync/manifest",
            "reload_filters": "/sync/reload",
        }
    })


@app.get("/ui-check", tags=["General"])
async def ui_check_redirect():
    """Redirect /ui-check to the static check UI root."""
    return RedirectResponse(url="/ui-check/")


@app.get("/ui-check/", tags=["General"])
async def ui_check_page():
    """Render the check UI root page."""
    return _render_ui_page("ui-check")


@app.get("/ui-sync", tags=["General"])
async def ui_sync_redirect():
    """Redirect /ui-sync to the static sync UI root."""
    return RedirectResponse(url="/ui-sync/")


@app.get("/ui-sync/", tags=["General"])
async def ui_sync_page():
    """Render the sync UI root page."""
    return _render_ui_page("ui-sync")


# Static UI
app.mount("/ui-check", StaticFiles(directory="webui/ui-check", html=True), name="ui-check")
app.mount("/ui-sync", StaticFiles(directory="webui/ui-sync", html=True), name="ui-sync")


@app.get("/health", tags=["General"])
async def health():
    """Health check endpoint."""
    if filter_manager is None:
        raise HTTPException(status_code=503, detail="Filter manager not initialized")
    
    return _with_test_mode({
        "status": "healthy",
        "filters_loaded": len(filter_manager.filters)
    })


@app.get("/stats", response_model=StatsResponse, tags=["General"])
async def get_stats():
    """Get statistics about loaded filters."""
    if filter_manager is None:
        raise HTTPException(status_code=503, detail="Filter manager not initialized")
    
    return StatsResponse(**filter_manager.get_stats())


@app.post("/check", response_model=CheckResponse, tags=["Check"])
async def check_credential(request: CheckRequest):
    """
    Check if a single credential exists in the bloom filter(s).
    
    Returns information about whether the credential was found and in which filters.
    """
    if filter_manager is None:
        raise HTTPException(status_code=503, detail="Filter manager not initialized")
    
    result = filter_manager.check(request.username, request.password)
    
    return CheckResponse(
        # username=result.username,
        # password=result.password,
        found=result.found,
        # match_count=result.match_count,
        matching_filters=result.matching_filters
    )


@app.get("/check", response_model=CheckResponse, tags=["Check"])
async def check_credential_get(
    username: str = Query(..., description="Username to check"),
    password: str = Query("", description="Password to check (optional)")
):
    """
    Check if a single credential exists in the bloom filter(s) via GET request.
    
    Useful for simple URL-based queries.
    """
    if filter_manager is None:
        raise HTTPException(status_code=503, detail="Filter manager not initialized")
    
    result = filter_manager.check(username, password)
    
    return CheckResponse(
        # username=result.username,
        # password=result.password,
        found=result.found,
        # match_count=result.match_count,
        matching_filters=result.matching_filters
    )


@app.post("/check/batch", response_model=BatchCheckResponse, tags=["Check"])
async def check_credentials_batch(request: BatchCheckRequest):
    """
    Check multiple credentials in a single request.
    
    Efficiently checks multiple username/password combinations.
    """
    if filter_manager is None:
        raise HTTPException(status_code=503, detail="Filter manager not initialized")
    
    credentials = [(cred.username, cred.password) for cred in request.credentials]
    results = filter_manager.check_batch(credentials)
    
    found_usernames = []
    seen = set()
    for (username, _), result in zip(credentials, results):
        if result.found and username not in seen:
            seen.add(username)
            found_usernames.append(username)

    return BatchCheckResponse(
        total=len(results),
        found_usernames=found_usernames
    )


@app.post("/checkhash", response_model=CheckResponse, tags=["Check"])
async def check_precomputed_sha256(request: SingleHashCheckRequest):
    """
    Check a single precomputed SHA-256 hash for membership in the loaded filter(s).

    The provided hash is converted into Bloom probe positions using Murmur3 double-hashing.
    """
    if filter_manager is None:
        raise HTTPException(status_code=503, detail="Filter manager not initialized")

    try:
        result = filter_manager.check_sha256_hash(request.hash)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return CheckResponse(
        found=result.found,
        matching_filters=result.matching_filters
    )


@app.post("/checkhash/batch", response_model=BatchHashCheckResponse, tags=["Check"])
async def check_precomputed_sha256_batch(request: BatchHashCheckRequest):
    """
    Check multiple precomputed SHA-256 hashes in a single request.

    Returns only the hashes that were found in at least one loaded filter.
    """
    if filter_manager is None:
        raise HTTPException(status_code=503, detail="Filter manager not initialized")

    try:
        results = filter_manager.check_sha256_batch(request.hashes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    found_hashes = []
    seen = set()
    for hash_value, result in zip(request.hashes, results):
        if result.found and hash_value not in seen:
            seen.add(hash_value)
            found_hashes.append(hash_value)

    return BatchHashCheckResponse(
        total=len(request.hashes),
        found_hashes=found_hashes
    )


@app.post("/sync/filters", response_model=SyncFiltersResponse, tags=["Sync"])
async def sync_filters_endpoint():
    """
    Download, verify, and extract filters from nWebbed storage.
    """
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another filter operation is already in progress")

    try:
        sync_operation_logs.start("sync_filters")
        response = await run_in_threadpool(_run_filter_sync_with_logs)
        if response.success:
            return response
        return JSONResponse(status_code=500, content=response.model_dump())
    finally:
        sync_operation_logs.finish()
        operation_lock.release()


@app.post("/sync/apply", response_model=SyncApplyResponse, tags=["Sync"])
async def sync_apply_endpoint():
    """
    Run filter sync, manifest update, and filter reload in sequence.
    """
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another filter operation is already in progress")

    try:
        sync_operation_logs.start("sync_apply")
        response = await run_in_threadpool(_run_sync_apply_with_logs)
        if response.success:
            return response
        return JSONResponse(status_code=500, content=response.model_dump())
    finally:
        sync_operation_logs.finish()
        operation_lock.release()


@app.post("/sync/manifest", response_model=ManifestUpdateResponse, tags=["Sync"])
async def update_manifest_endpoint():
    """
    Regenerate manifest.json from the local filters directory.
    """
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another filter operation is already in progress")

    try:
        sync_operation_logs.start("sync_manifest")
        response = await run_in_threadpool(_run_manifest_update_with_logs)
        if response.success:
            return response
        return JSONResponse(status_code=500, content=response.model_dump())
    finally:
        sync_operation_logs.finish()
        operation_lock.release()


@app.post("/sync/reload", response_model=ReloadFiltersResponse, tags=["Sync"])
async def reload_filters_endpoint():
    """
    Reload manifest/filter files into memory without restarting the API process.
    """
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another filter operation is already in progress")

    try:
        sync_operation_logs.start("sync_reload")
        response = await run_in_threadpool(_run_filter_reload_with_logs)
        if response.success:
            return response
        return JSONResponse(status_code=500, content=response.model_dump())
    finally:
        sync_operation_logs.finish()
        operation_lock.release()


@app.get("/sync/status", response_model=SyncStatusResponse, tags=["Sync"])
async def sync_status_endpoint():
    """Return live logs for the current or most recent sync operation."""
    return SyncStatusResponse(**sync_operation_logs.snapshot())


def _run_filter_sync_with_logs() -> SyncFiltersResponse:
    log_stream = StringIO()
    formatter = logging.Formatter("%(levelname)s %(message)s")
    capture_handler = logging.StreamHandler(log_stream)
    live_handler = _LiveSyncLogHandler()
    stdout_handler = logging.StreamHandler(sys.stdout)
    for handler in (capture_handler, live_handler, stdout_handler):
        handler.setLevel(logging.INFO)
        handler.setFormatter(formatter)

    sync_logger = logging.getLogger("filter_sync")
    previous_level = sync_logger.level
    previous_propagate = sync_logger.propagate
    sync_logger.setLevel(logging.INFO)
    sync_logger.propagate = False
    for handler in (capture_handler, live_handler, stdout_handler):
        sync_logger.addHandler(handler)

    try:
        result = sync_filters()
    except (R2ClientError, SyncError) as exc:
        capture_handler.flush()
        logs = _split_log_lines(log_stream.getvalue())
        return SyncFiltersResponse(
            success=False,
            logs=logs,
            detail=str(exc),
        )
    except Exception as exc:
        capture_handler.flush()
        logs = _split_log_lines(log_stream.getvalue())
        return SyncFiltersResponse(
            success=False,
            logs=logs,
            detail=f"Unexpected sync error: {exc}",
        )
    finally:
        for handler in (capture_handler, live_handler, stdout_handler):
            sync_logger.removeHandler(handler)
        sync_logger.setLevel(previous_level)
        sync_logger.propagate = previous_propagate
        capture_handler.close()
        live_handler.close()
        stdout_handler.close()

    capture_handler.flush()
    logs = _split_log_lines(log_stream.getvalue())
    return SyncFiltersResponse(
        success=True,
        manifest_path=str(result.manifest_path),
        downloaded=[str(path) for path in result.downloaded],
        redownloaded=[str(path) for path in result.redownloaded],
        verified_existing=[str(path) for path in result.verified_existing],
        logs=logs,
    )


def _run_manifest_update_with_logs() -> ManifestUpdateResponse:
    log_stream = StringIO()
    stdout_stream = StringIO()
    stderr_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    manifest_logger = logging.getLogger("helpers.generate_manifest")
    previous_level = manifest_logger.level
    previous_propagate = manifest_logger.propagate
    manifest_logger.setLevel(logging.INFO)
    manifest_logger.propagate = False
    manifest_logger.addHandler(handler)

    try:
        with redirect_stdout(stdout_stream), redirect_stderr(stderr_stream):
            exit_code = generate_manifest("filters", "manifest.json")
    except Exception as exc:
        handler.flush()
        logs = _merge_output_logs(log_stream, stdout_stream, stderr_stream)
        return ManifestUpdateResponse(
            success=False,
            logs=logs,
            detail=f"Unexpected manifest update error: {exc}",
        )
    finally:
        manifest_logger.removeHandler(handler)
        manifest_logger.setLevel(previous_level)
        manifest_logger.propagate = previous_propagate
        handler.close()

    logs = _merge_output_logs(log_stream, stdout_stream, stderr_stream)
    if exit_code != 0:
        return ManifestUpdateResponse(
            success=False,
            output_file="manifest.json",
            logs=logs,
            detail="generate_manifest failed",
        )

    try:
        with open("manifest.json", "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        filter_count = len(manifest.get("filters", []))
    except Exception:
        filter_count = 0

    return ManifestUpdateResponse(
        success=True,
        output_file="manifest.json",
        filter_count=filter_count,
        logs=logs,
    )


def _run_sync_apply_with_logs() -> SyncApplyResponse:
    logs = [
        "INFO starting filter sync, manifest update, and reload sequence",
        "INFO step 1/3: filter sync",
    ]
    for line in logs:
        _append_live_sync_log(line)

    sync_response = _run_filter_sync_with_logs()
    logs.extend(sync_response.logs)
    if not sync_response.success:
        logs.append("ERROR sequence stopped during filter sync")
        _append_live_sync_log("ERROR sequence stopped during filter sync")
        return SyncApplyResponse(
            success=False,
            manifest_path=sync_response.manifest_path,
            downloaded=sync_response.downloaded,
            redownloaded=sync_response.redownloaded,
            verified_existing=sync_response.verified_existing,
            logs=logs,
            detail=sync_response.detail,
        )

    logs.append("INFO step 2/3: manifest update")
    _append_live_sync_log("INFO step 2/3: manifest update")
    manifest_response = _run_manifest_update_with_logs()
    logs.extend(manifest_response.logs)
    if not manifest_response.success:
        logs.append("ERROR sequence stopped during manifest update")
        _append_live_sync_log("ERROR sequence stopped during manifest update")
        return SyncApplyResponse(
            success=False,
            manifest_path=sync_response.manifest_path,
            output_file=manifest_response.output_file,
            downloaded=sync_response.downloaded,
            redownloaded=sync_response.redownloaded,
            verified_existing=sync_response.verified_existing,
            filter_count=manifest_response.filter_count,
            logs=logs,
            detail=manifest_response.detail,
        )

    logs.append("INFO step 3/3: reload filters")
    _append_live_sync_log("INFO step 3/3: reload filters")
    reload_response = _run_filter_reload_with_logs()
    logs.extend(reload_response.logs)
    if not reload_response.success:
        logs.append("ERROR sequence stopped during filter reload")
        _append_live_sync_log("ERROR sequence stopped during filter reload")
        return SyncApplyResponse(
            success=False,
            manifest_path=sync_response.manifest_path,
            output_file=manifest_response.output_file,
            downloaded=sync_response.downloaded,
            redownloaded=sync_response.redownloaded,
            verified_existing=sync_response.verified_existing,
            filter_count=reload_response.filter_count,
            filters=reload_response.filters,
            max_nk=reload_response.max_nk,
            logs=logs,
            detail=reload_response.detail,
        )

    logs.append("INFO completed filter sync, manifest update, and reload sequence")
    _append_live_sync_log("INFO completed filter sync, manifest update, and reload sequence")
    return SyncApplyResponse(
        success=True,
        manifest_path=sync_response.manifest_path,
        output_file=manifest_response.output_file,
        downloaded=sync_response.downloaded,
        redownloaded=sync_response.redownloaded,
        verified_existing=sync_response.verified_existing,
        filter_count=reload_response.filter_count,
        filters=reload_response.filters,
        max_nk=reload_response.max_nk,
        logs=logs,
    )


def _run_filter_reload_with_logs() -> ReloadFiltersResponse:
    global filter_manager

    logs: list[str] = []
    try:
        manifest_path = _current_filter_configuration()
        new_manager = FilterManager(manifest_path=manifest_path)
        logs.append(f"INFO loaded {len(new_manager.filters)} filters from {manifest_path}")

        old_manager = filter_manager
        filter_manager = new_manager
        if old_manager is not None:
            old_manager.close()
            logs.append("INFO closed previous filter mappings")

        stats = new_manager.get_stats()
        return ReloadFiltersResponse(
            success=True,
            filter_count=stats["filter_count"],
            filters=stats["filters"],
            max_nk=stats["max_nk"],
            logs=logs,
        )
    except Exception as exc:
        return ReloadFiltersResponse(
            success=False,
            logs=logs,
            detail=f"Failed to reload filters: {exc}",
        )


def _current_filter_configuration() -> str:
    is_test_mode = _is_test_mode_enabled()
    default_manifest = "test_manifest.json" if is_test_mode else "manifest.json"
    return os.getenv("MANIFEST_PATH", default_manifest)


def _is_auto_update_enabled() -> bool:
    return os.getenv("AUTO_UPDATE_FILTERS", "").strip() == "1"


def _configured_auto_update_hour() -> int | None:
    raw_hour = os.getenv("AUTO_UPDATE_TIME", "").strip()
    if not raw_hour:
        return None

    try:
        hour = int(raw_hour)
    except ValueError:
        return None

    if hour < 0 or hour > 23:
        return None

    return hour


def _seconds_until_next_auto_update(now: datetime, update_hour: int) -> float:
    next_run = now.replace(hour=update_hour, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


async def _auto_update_scheduler_loop() -> None:
    while True:
        update_hour = _configured_auto_update_hour()
        if update_hour is None:
            logger.warning("Stopping auto-update scheduler because AUTO_UPDATE_TIME is invalid")
            return

        now = datetime.now().astimezone()
        sleep_seconds = _seconds_until_next_auto_update(now, update_hour)
        next_run = now + timedelta(seconds=sleep_seconds)
        logger.info("Next auto update scheduled for %s", next_run.isoformat(timespec="seconds"))

        await asyncio.sleep(sleep_seconds)
        await asyncio.to_thread(_run_scheduled_auto_update)


def _run_scheduled_auto_update() -> None:
    if not operation_lock.acquire(blocking=False):
        logger.info("Skipping scheduled auto update because another filter operation is already in progress")
        return

    try:
        sync_operation_logs.start("auto_sync_apply")
        logger.info("Starting scheduled filter sync, manifest update, and reload sequence")
        response = _run_sync_apply_with_logs()
        if response.success:
            logger.info("Scheduled filter sync, manifest update, and reload completed successfully")
            return

        logger.error(
            "Scheduled filter sync, manifest update, and reload failed: %s",
            response.detail or "unknown error",
        )
    finally:
        sync_operation_logs.finish()
        operation_lock.release()


def _merge_output_logs(
    log_stream: StringIO,
    stdout_stream: StringIO,
    stderr_stream: StringIO,
) -> List[str]:
    lines = _split_log_lines(log_stream.getvalue())
    lines.extend(line for line in stdout_stream.getvalue().splitlines() if line.strip())
    lines.extend(line for line in stderr_stream.getvalue().splitlines() if line.strip())
    return lines


def _split_log_lines(raw_logs: str) -> List[str]:
    return [line for line in raw_logs.splitlines() if line.strip()]


def _append_live_sync_log(line: str) -> None:
    sync_operation_logs.append(line)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
