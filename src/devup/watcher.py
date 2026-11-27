"""Zero-dependency polling file watcher for --watch auto-restart."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".turbo",
        "target",
        ".eggs",
    }
)


def _matches_any_pattern(rel_posix: str, patterns: list[str]) -> bool:
    """Match relative POSIX path against glob patterns, treating ** as 0+ segments."""
    filename = rel_posix.rsplit("/", 1)[-1]
    for raw in patterns:
        pat = raw.replace("\\", "/").lstrip("./")
        if not pat:
            continue
        if "/" not in pat and fnmatch.fnmatch(filename, pat):
            return True
        if fnmatch.fnmatch(rel_posix, pat):
            return True
        if "**/" in pat and fnmatch.fnmatch(rel_posix, pat.replace("**/", "", 1)):
            return True
    return False


def snapshot_tree(
    base_dir: Path,
    patterns: list[str] | None = None,
    ignore_dirs: frozenset[str] = DEFAULT_IGNORE_DIRS,
) -> dict[Path, int]:
    """Capture mtime_ns for matching non-ignored files beneath base_dir."""
    snapshots: dict[Path, int] = {}
    if not base_dir.exists():
        return snapshots

    resolved_base = base_dir.resolve()
    for dirpath, dirnames, filenames in os.walk(resolved_base):
        dirnames[:] = [
            d for d in dirnames if d not in ignore_dirs and not d.endswith(".egg-info")
        ]
        current_dir = Path(dirpath)
        for fname in filenames:
            full_path = current_dir / fname
            if patterns:
                try:
                    rel_posix = full_path.relative_to(resolved_base).as_posix()
                except ValueError:
                    continue
                if not _matches_any_pattern(rel_posix, patterns):
                    continue
            try:
                snapshots[full_path] = full_path.stat().st_mtime_ns
            except OSError:
                continue
    return snapshots


def diff_snapshots(prev: dict[Path, int], curr: dict[Path, int]) -> Path | None:
    """Return the first created, modified, or deleted file path between snapshots."""
    for path, mtime in curr.items():
        old_mtime = prev.get(path)
        if old_mtime is None or old_mtime != mtime:
            return path
    for path in prev:
        if path not in curr:
            return path
    return None
