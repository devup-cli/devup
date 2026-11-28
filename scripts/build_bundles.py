"""Build platform-specific release archive bundles (Windows .zip and POSIX .tar.gz)."""

from __future__ import annotations

import argparse
import hashlib
import io
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from build_zipapp import build_zipapp


def _write_sha256(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sha_path = path.with_suffix(path.suffix + ".sha256")
    sha_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return sha_path


def build_platform_bundles(output_dir: Path, tag: str = "0.3.0") -> list[Path]:
    """Create ready-to-unpack Windows (.zip) and POSIX (.tar.gz) distribution bundles."""
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_tag = tag.lstrip("v") or "0.3.0"
    created: list[Path] = []

    with tempfile.TemporaryDirectory() as td:
        pyz_path = Path(td) / "devup.pyz"
        build_zipapp(pyz_path)
        pyz_bytes = pyz_path.read_bytes()

        readme_bytes = (ROOT / "README.md").read_bytes()
        license_bytes = (ROOT / "LICENSE").read_bytes()

        win_zip = output_dir / f"devup-v{clean_tag}-windows.zip"
        with zipfile.ZipFile(win_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("devup.pyz", pyz_bytes)
            zf.writestr("devup.cmd", '@echo off\r\npython "%~dp0devup.pyz" %*\r\n')
            zf.writestr("devup.ps1", '& python "$PSScriptRoot\\devup.pyz" @args\n')
            zf.writestr("README.md", readme_bytes)
            zf.writestr("LICENSE", license_bytes)
        created.extend([win_zip, _write_sha256(win_zip)])

        posix_tgz = output_dir / f"devup-v{clean_tag}-posix.tar.gz"
        with tarfile.open(posix_tgz, "w:gz") as tf:
            for name, data, mode in (
                ("devup", pyz_bytes, 0o755),
                ("README.md", readme_bytes, 0o644),
                ("LICENSE", license_bytes, 0o644),
            ):
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mode = stat.S_IFREG | mode
                tf.addfile(info, io.BytesIO(data))
        created.extend([posix_tgz, _write_sha256(posix_tgz)])

    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Build platform-specific devup release bundles.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist-bundles")
    parser.add_argument("--tag", default="0.3.0")
    args = parser.parse_args()
    for p in build_platform_bundles(args.output_dir, tag=args.tag):
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
