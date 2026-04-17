from __future__ import annotations

import json
import logging
from pathlib import Path

from helpers.generate_manifest import generate_manifest


def test_generate_manifest_uses_filters_manifest_current_for_nested_paths(
    tmp_path: Path,
) -> None:
    filters_dir = tmp_path / "filters"
    nested_dir = filters_dir / "202604" / "20260401_20260408"
    nested_dir.mkdir(parents=True)

    first_filter = nested_dir / "00_20260401_20260408.hf"
    second_filter = nested_dir / "01_20260401_20260408.hf"
    first_filter.write_bytes(b"first")
    second_filter.write_bytes(b"second")

    manifest_current = {
        "current_filter_locations": ["202604/20260401_20260408"],
        "current_filter_files": [
            {"path": "20260401_20260408/01_20260401_20260408.hf", "md5": "unused"},
            {"path": "20260401_20260408/00_20260401_20260408.hf", "md5": "unused"},
        ],
    }
    (filters_dir / "manifest_current.json").write_text(
        json.dumps(manifest_current),
        encoding="utf-8",
    )

    output_path = tmp_path / "manifest.json"
    result = generate_manifest(str(filters_dir), str(output_path))

    assert result == 0
    manifest = json.loads(output_path.read_text(encoding="utf-8"))
    assert manifest == {
        "version": "1.0",
        "filters": [
            "filters/202604/20260401_20260408/00_20260401_20260408.hf",
            "filters/202604/20260401_20260408/01_20260401_20260408.hf",
        ],
    }


def test_generate_manifest_warns_and_recurses_without_manifest_current(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING)
    filters_dir = tmp_path / "filters"
    nested_dir = filters_dir / "202604" / "20260401_20260408"
    nested_dir.mkdir(parents=True)

    root_filter = filters_dir / "root_filter.hf"
    nested_filter = nested_dir / "00_20260401_20260408.hf"
    root_filter.write_bytes(b"root")
    nested_filter.write_bytes(b"nested")

    output_path = tmp_path / "manifest.json"
    result = generate_manifest(str(filters_dir), str(output_path))

    assert result == 0
    manifest = json.loads(output_path.read_text(encoding="utf-8"))
    assert manifest == {
        "version": "1.0",
        "filters": [
            "filters/202604/20260401_20260408/00_20260401_20260408.hf",
            "filters/root_filter.hf",
        ],
    }
    assert "No manifest_current.json found under" in caplog.text
