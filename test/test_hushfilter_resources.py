from __future__ import annotations

import builtins
import mmap
import os
from pathlib import Path

import pytest

from core.main import HushFilter


def test_hushfilter_releases_file_descriptor_after_mapping_on_posix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    filter_path = tmp_path / "sample.hf"
    filter_path.write_bytes(b"HUSH" + b"\x00" * 128)

    monkeypatch.setattr(HushFilter, "parse_header", lambda self: {"sauth": b"sauth-bytes"})
    monkeypatch.setattr(HushFilter, "decode_public_key", lambda self, sauth: object())
    monkeypatch.setattr(HushFilter, "load_filter", lambda self: None)

    hush_filter = HushFilter(str(filter_path))
    try:
        assert hush_filter.mm is not None
        if os.name != "nt":
            assert hush_filter.f is None
        else:
            assert hush_filter.f is not None
    finally:
        hush_filter.close()

    assert hush_filter.mm is None
    assert hush_filter.f is None


def test_hushfilter_cleans_up_partial_resources_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFile:
        def __init__(self) -> None:
            self.closed = False

        def fileno(self) -> int:
            return 123

        def close(self) -> None:
            self.closed = True

    class FakeMmap:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    opened_files: list[FakeFile] = []
    created_mmaps: list[FakeMmap] = []

    def fake_open(path: str, mode: str):
        del path, mode
        handle = FakeFile()
        opened_files.append(handle)
        return handle

    def fake_mmap(fileno: int, length: int, access: int):
        del fileno, length, access
        mapped_file = FakeMmap()
        created_mmaps.append(mapped_file)
        return mapped_file

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(mmap, "mmap", fake_mmap)
    monkeypatch.setattr(HushFilter, "parse_header", lambda self: (_ for _ in ()).throw(ValueError("bad header")))

    with pytest.raises(ValueError, match="bad header"):
        HushFilter("filters/bad.hf")

    assert len(opened_files) == 1
    assert len(created_mmaps) == 1
    assert opened_files[0].closed is True
    assert created_mmaps[0].closed is True
