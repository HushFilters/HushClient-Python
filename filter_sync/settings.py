"""Persist the opt-in policy for reusing locally verified filter checksums."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class SyncSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    reuse_verified_md5: bool = False


def settings_path(filters_root: Path = Path("filters")) -> Path:
    return Path(os.getenv("HUSHCLIENT_SYNC_SETTINGS_PATH", str(filters_root / ".sync_settings.json")))


def load_settings(filters_root: Path = Path("filters")) -> SyncSettings:
    try:
        return SyncSettings.model_validate_json(settings_path(filters_root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SyncSettings()
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("Could not read sync settings; rehashing filters", exc_info=True)
        return SyncSettings()


def save_settings(settings: SyncSettings) -> SyncSettings:
    from .activation import atomic_json

    atomic_json(settings_path(), settings.model_dump())
    return settings
