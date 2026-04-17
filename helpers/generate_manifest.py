#!/usr/bin/env python3
"""
Generate a manifest.json file from .hf files in the filters directory.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)
REMOTE_MANIFEST_NAME = "manifest_current.json"


def generate_manifest(filters_dir: str = "filters", output_file: str = "manifest.json") -> int:
    """
    Generate manifest.json from .hf files in the specified directory.

    If filters/manifest_current.json exists, use it to reconstruct the nested
    extracted filter paths. Otherwise, fall back to recursively discovering .hf
    files under filters_dir.
    """
    filters_path = Path(filters_dir)
    output_path = Path(output_file)

    filter_paths = _filter_paths_from_manifest_current(filters_path)
    if filter_paths is None:
        logger.warning(
            "No %s found under %s; recursively discovering .hf files instead",
            REMOTE_MANIFEST_NAME,
            filters_path,
        )
        filter_paths = _discover_filter_paths(filters_path)

    if not filter_paths:
        print(f"Error: No .hf files found in {filters_path}", file=sys.stderr)
        return 1

    manifest = {
        "version": "1.0",
        "filters": filter_paths,
    }

    output_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {output_path} with {len(filter_paths)} filters")
    return 0


def _filter_paths_from_manifest_current(filters_path: Path) -> list[str] | None:
    manifest_current_path = filters_path / REMOTE_MANIFEST_NAME
    if not manifest_current_path.exists():
        return None

    data = json.loads(manifest_current_path.read_text(encoding="utf-8"))
    location_map = _build_location_map(data.get("current_filter_locations", []))
    filter_paths: list[str] = []

    for entry in data.get("current_filter_files", []):
        relative_path = _resolve_manifest_filter_path(
            entry.get("path", ""),
            location_map=location_map,
        )
        full_path = filters_path / Path(*relative_path.parts)
        if not full_path.exists():
            logger.warning("Skipping missing manifest-listed filter file: %s", full_path)
            continue
        filter_paths.append(_to_manifest_path(full_path, filters_path))

    return sorted(filter_paths)


def _build_location_map(current_filter_locations: list[str]) -> dict[str, PurePosixPath]:
    location_map: dict[str, PurePosixPath] = {}
    for location in current_filter_locations:
        location_path = PurePosixPath(location)
        if location_path.is_absolute() or ".." in location_path.parts:
            continue
        if not location_path.parts:
            continue
        location_name = location_path.parts[-1]
        location_map[location_name] = location_path
    return location_map


def _resolve_manifest_filter_path(
    raw_filter_path: str,
    *,
    location_map: dict[str, PurePosixPath],
) -> PurePosixPath:
    filter_path = PurePosixPath(raw_filter_path)
    if filter_path.is_absolute() or ".." in filter_path.parts or not filter_path.parts:
        raise ValueError(f"Unsafe filter path in {REMOTE_MANIFEST_NAME}: {raw_filter_path!r}")

    location_name = filter_path.parts[0]
    resolved_location = location_map.get(location_name)
    if resolved_location is None:
        return filter_path

    return resolved_location / PurePosixPath(*filter_path.parts[1:])


def _discover_filter_paths(filters_path: Path) -> list[str]:
    return sorted(
        _to_manifest_path(path, filters_path)
        for path in filters_path.rglob("*.hf")
        if path.is_file()
    )


def _to_manifest_path(path: Path, filters_path: Path) -> str:
    return path.relative_to(filters_path.parent).as_posix()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate manifest.json from filter files",
    )
    parser.add_argument(
        "-d",
        "--dir",
        default="filters",
        help="Directory containing .hf files (default: filters)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="manifest.json",
        help="Output manifest file (default: manifest.json)",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(generate_manifest(args.dir, args.output))
