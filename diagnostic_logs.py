"""Persistent, bounded application diagnostics and runtime logging settings."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import tempfile

from pydantic import BaseModel


class LogSettings(BaseModel):
    everything: bool = False
    critical: bool = False
    errors: bool = True
    warnings: bool = True
    sync: bool = True
    application: bool = False
    requests: bool = False


class DiagnosticLogs(RotatingFileHandler):
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.settings_path = directory / "settings.json"
        self.settings = LogSettings()
        settings_error = None
        try:
            self.settings = LogSettings.model_validate_json(self.settings_path.read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            settings_error = exc
        super().__init__(directory / "hushclient.log", maxBytes=10 * 1024 * 1024,
                         backupCount=3, encoding="utf-8")
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        if settings_error:
            self.handle(logging.LogRecord("hushclient", logging.ERROR, "", 0,
                        "Could not load log settings; using defaults: %s", (settings_error,), None))

    def emit(self, record):
        # Uvicorn access records contain raw credential query strings. The API
        # emits a separate request summary containing only the route template.
        if record.name == "uvicorn.access":
            return
        s = self.settings
        sync = record.name.startswith(("filter_sync", "helpers.generate_manifest", "hushclient.sync"))
        requests = record.name == "hushclient.requests"
        if (s.everything or (s.critical and record.levelno >= logging.CRITICAL)
                or (s.errors and record.levelno >= logging.ERROR)
                or (s.warnings and record.levelno == logging.WARNING)
                or (s.sync and sync) or (s.requests and requests)
                or (s.application and not sync and not requests)):
            super().emit(record)

    def format(self, record):
        message = super().format(record)
        for key in ("NWEBBED_API_KEY", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            secret = os.getenv(key)
            if secret:
                message = message.replace(secret, "[redacted]")
        message = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", message)
        message = re.sub(r"(?i)(authorization[\s:=]+|HFKey\s+)[^\s,]+", r"\1[redacted]", message)
        return message

    def files(self):
        return [Path(f"{self.baseFilename}.{i}") for i in range(self.backupCount, 0, -1)] + [Path(self.baseFilename)]

    def snapshot(self):
        self.acquire()
        try:
            self.flush()
            paths = [path for path in self.files() if path.exists()]
            size = sum(path.stat().st_size for path in paths)
            # Bound both the disk read and the response even with verbose logging.
            remaining = 256 * 1024
            chunks = []
            for path in reversed(paths):
                with path.open("rb") as handle:
                    length = path.stat().st_size
                    handle.seek(max(0, length - remaining))
                    chunk = handle.read(remaining)
                chunks.insert(0, chunk)
                remaining -= len(chunk)
                if not remaining:
                    break
            return {"size_bytes": size, "max_size_bytes": self.maxBytes * (self.backupCount + 1),
                    "settings": self.settings.model_dump(), "text": b"".join(chunks).decode("utf-8", errors="replace"),
                    "truncated": size > 256 * 1024}
        finally:
            self.release()

    def configure(self, settings: LogSettings):
        self.acquire()
        try:
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(self.settings_path)
            self.settings = settings
        finally:
            self.release()

    def clear(self):
        self.acquire()
        try:
            self.flush()
            for path in self.files()[:-1]:
                path.unlink(missing_ok=True)
            with open(self.baseFilename, "w", encoding="utf-8"):
                pass
        finally:
            self.release()

    def export(self):
        """Snapshot retained files before streaming, without blocking ongoing writes."""
        result = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        self.acquire()
        try:
            self.flush()
            for path in self.files():
                if path.exists():
                    with path.open("rb") as source:
                        shutil.copyfileobj(source, result)
            result.seek(0)
            return result
        except BaseException:
            result.close()
            raise
        finally:
            self.release()


def install_logging():
    handler = DiagnosticLogs(Path(os.getenv("HUSHCLIENT_LOG_DIR", "logs")))
    attached = []
    # Uvicorn normally stops propagation at its parent logger.
    for name in ("", "uvicorn"):
        target = logging.getLogger(name)
        if name and target.propagate:
            continue
        attached.append((target, target.level))
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
    return handler, attached


def uninstall_logging(handler, attached):
    for target, level in attached:
        target.removeHandler(handler)
        target.setLevel(level)
    handler.close()
