#!/usr/bin/env python3
"""Zip ``src/`` into ``Repos-<version>.alfredworkflow`` at the repo root.

The version string is read from the root ``version`` key in ``src/info.plist``.
Run from anywhere; paths are resolved from this script's location.

Usage:
    ./scripts/build-workflow.py
    python3 scripts/build-workflow.py
"""

from __future__ import annotations

import plistlib
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
INFO_PLIST = SRC_DIR / "info.plist"
SKIP_NAMES = frozenset({".DS_Store", ".gitkeep"})
SKIP_SUFFIXES = (".pyc",)


def _workflow_version() -> str:
    if not INFO_PLIST.is_file():
        sys.exit(f"error: missing {INFO_PLIST}")
    with INFO_PLIST.open("rb") as fp:
        data = plistlib.load(fp)
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        sys.exit(f"error: root key 'version' must be a non-empty string in {INFO_PLIST}")
    return version.strip()


def _safe_filename_fragment(version: str) -> str:
    return version.replace("/", "-").replace("\\", "-")


def _iter_files(src: Path):
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if path.name in SKIP_NAMES:
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def main() -> None:
    if not SRC_DIR.is_dir():
        sys.exit(f"error: missing directory {SRC_DIR}")

    version = _workflow_version()
    out_name = f"Repos-{_safe_filename_fragment(version)}.alfredworkflow"
    out_path = REPO_ROOT / out_name

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in _iter_files(SRC_DIR):
            arc = path.relative_to(SRC_DIR).as_posix()
            zf.write(path, arcname=arc)

    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes) version={version!r}")


if __name__ == "__main__":
    main()
