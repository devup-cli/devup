"""Build a single-file portable devup.pyz zipapp executable using stdlib zipapp."""

from __future__ import annotations

import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = ROOT / "src" / "devup"
OUT_PATH = ROOT / "dist" / "devup.pyz"


def build_zipapp(output: Path = OUT_PATH) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "stage"
        shutil.copytree(
            SRC_PKG,
            stage / "devup",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        zipapp.create_archive(
            stage,
            target=output,
            interpreter="/usr/bin/env python3",
            main="devup.cli:main",
            compressed=True,
        )
    return output


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build standalone devup.pyz zipapp.")
    parser.add_argument("-o", "--output", type=Path, default=OUT_PATH)
    args = parser.parse_args()
    artifact = build_zipapp(args.output)
    print(f"Built {artifact} ({artifact.stat().st_size} bytes)")
