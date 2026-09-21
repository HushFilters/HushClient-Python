"""Persisted SMTP configuration and bounded, non-blocking operational alerts."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import json
import logging
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import smtplib
import socket
import ssl
import tempfile
import threading
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

Event = Literal["sync_failure", "filter_failure", "no_filters", "service_error", "certificate_expiry"]
EVENT_LABELS = {
    "sync_failure": "Filter sync failed",
    "filter_failure": "Filter loading or manifest update failed",
    "no_filters": "No filters loaded",
    "service_error": "Critical service error",
    "certificate_expiry": "TLS certificate expiry or validity issue",
}
logger = logging.getLogger("hushclient.alerts")


class AlertSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str = Field(default="", max_length=320)
    password: SecretStr | None = Field(default=None, max_length=4096)
    sender: str = Field(default="", max_length=254)
    recipients: list[str] = Field(default_factory=list, max_length=50)
    events: list[Event] = Field(default_factory=lambda: list(EVENT_LABELS))
    cooldown_minutes: int = Field(default=15, ge=1, le=1440)
    certificate_warning_days: int = Field(default=30, ge=1, le=365)

    @field_validator("smtp_host", "username", "sender")
    @classmethod
    def single_line(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use a single line without control characters")
        return value.strip()

    @field_validator("smtp_host")
    @classmethod
    def host_only(cls, value: str) -> str:
        if value and (any(char.isspace() for char in value) or any(char in value for char in "/@?#")):
            raise ValueError("Enter an SMTP hostname or IP address without a URL or port")
        return value

    @field_validator("sender")
    @classmethod
    def sender_address(cls, value: str) -> str:
        if value:
            validate_address(value)
        return value

    @field_validator("recipients")
    @classmethod
    def recipient_addresses(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            value = value.strip()
            validate_address(value)
            if value not in result:
                result.append(value)
        return result

    def require_delivery_settings(self) -> None:
        if not self.smtp_host or not self.sender or not self.recipients:
            raise ValueError("SMTP host, sender, and at least one recipient are required")
        password = self.password.get_secret_value() if self.password else ""
        if bool(self.username) != bool(password):
            raise ValueError("Provide both an SMTP username and password, or leave both empty for a relay")
        if self.security == "none" and self.username:
            raise ValueError("SMTP authentication requires STARTTLS or TLS")


def validate_address(value: str) -> None:
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
        raise ValueError("Enter an email address without a display name")


def send_email(settings: AlertSettings, subject: str, body: str) -> None:
    """Use Python's SMTP sendmail; never downgrade a requested TLS connection."""
    settings.require_delivery_settings()
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = ", ".join(settings.recipients)
    message["Subject"] = f"[HushFilter] {subject}"
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    context = ssl.create_default_context()
    if settings.security == "ssl":
        connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10, context=context)
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    with connection as smtp:
        smtp.ehlo()
        if settings.security == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if settings.username:
            smtp.login(settings.username, settings.password.get_secret_value())
        refused = smtp.sendmail(settings.sender, settings.recipients, message.as_bytes())
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def delivery_error(exc: Exception) -> str:
    # SMTP errors may echo credentials or server responses; expose fixed summaries only.
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP authentication failed. Check the saved credentials."
    if isinstance(exc, ssl.SSLError):
        return "SMTP TLS verification failed. Check the server certificate and TLS mode."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "SMTP rejected one or more recipients; some recipients may have received the email."
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "The SMTP server does not support the selected TLS or authentication mode."
    if isinstance(exc, (OSError, smtplib.SMTPException)):
        return "SMTP delivery failed. Check the server, port, network access, and sender permissions."
    return "Email delivery failed. Check the saved SMTP settings."


class AlertManager:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._delivery_lock = threading.Lock()
        self.settings = AlertSettings()
        self.settings_error: str | None = None
        self.history: deque[dict] = deque(maxlen=20)
        self._last_queued: dict[str, float] = {}
        self._queue: Queue = Queue(maxsize=100)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        try:
            settings = AlertSettings.model_validate_json(path.read_text(encoding="utf-8"))
            if settings.enabled:
                settings.require_delivery_settings()
            self.settings = settings
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.settings_error = "Could not load saved alert settings. Alerts are disabled; save valid settings to restore them."
            logger.error(self.settings_error)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "settings": self.settings.model_dump(mode="json", exclude={"password"}),
                "password_configured": bool(self.settings.password and self.settings.password.get_secret_value()),
                "settings_error": self.settings_error,
                "history": list(self.history),
            }

    def configure(self, settings: AlertSettings) -> dict:
        with self._lock:
            settings = settings.model_copy(deep=True)
            if settings.password is None:
                settings.password = self.settings.password
            if settings.enabled:
                settings.require_delivery_settings()
                if not settings.events:
                    raise ValueError("Select at least one alert category")
            payload = settings.model_dump(mode="json", exclude={"password"})
            payload["password"] = settings.password.get_secret_value() if settings.password else None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".alerts-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
            self.settings = settings
            self.settings_error = None
            self._last_queued.clear()
            return self.snapshot()

    def start(self) -> None:
        self._worker = threading.Thread(target=self._run, name="smtp-alerts", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=12)

    def notify(self, event: Event, summary: str) -> bool:
        """Only callers' fixed operational summaries belong here, never request data."""
        with self._lock:
            if self._stop.is_set() or not self.settings.enabled or event not in self.settings.events:
                return False
            now = time.monotonic()
            previous = self._last_queued.get(event)
            if previous is not None and now - previous < self.settings.cooldown_minutes * 60:
                return False
            try:
                self._queue.put_nowait((event, summary, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            except Full:
                logger.warning("Alert queue is full; notification skipped")
                return False
            self._last_queued[event] = now
            return True

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                event, summary, occurred_at = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                with self._lock:
                    settings = self.settings.model_copy(deep=True)
                if settings.enabled and event in settings.events:
                    self._deliver(settings, event, summary, occurred_at)
            finally:
                self._queue.task_done()

    def _deliver(self, settings: AlertSettings, event: str, summary: str, occurred_at: str) -> dict:
        body = (f"{summary}\n\nClient: {socket.gethostname()}\nTime (UTC): {occurred_at}\n"
                "\nOpen the HushFilter Logs and Filter Sync pages for diagnostics.\n"
                "Credential checks, secrets, and raw request data are excluded from alert emails.\n")
        with self._delivery_lock:
            try:
                send_email(settings, EVENT_LABELS.get(event, "Test alert"), body)
                result = {"event": event, "success": True, "detail": "Email accepted by the SMTP server."}
            except Exception as exc:
                result = {"event": event, "success": False, "detail": delivery_error(exc)}
                logger.error("Alert delivery failed (%s): %s", event, result["detail"])
            result["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self._lock:
                self.history.appendleft(result)
            return result

    def test_email(self) -> dict:
        with self._lock:
            settings = self.settings.model_copy(deep=True)
        settings.require_delivery_settings()
        return self._deliver(settings, "test", "This is a test of HushFilter email alerts.",
                             datetime.now(timezone.utc).isoformat(timespec="seconds"))
