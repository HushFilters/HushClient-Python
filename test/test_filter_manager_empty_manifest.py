from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.filter_core import FilterManager


def test_filter_manager_allows_empty_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"version": "1.0", "filters": []}),
        encoding="utf-8",
    )

    manager = FilterManager(manifest_path=str(manifest_path))
    try:
        stats = manager.get_stats()
        assert stats["filter_count"] == 0
        assert stats["filters"] == []
        assert stats["max_nk"] == 0

        result = manager.check("user@example.com", "password")
        assert result.found is False
        assert result.match_count == 0
        assert result.matching_filters == []
    finally:
        manager.close()


def test_filter_manager_still_fails_when_manifest_entries_cannot_be_loaded(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"version": "1.0", "filters": ["filters/missing.hf"]}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="No filters were successfully loaded"):
        FilterManager(manifest_path=str(manifest_path))
