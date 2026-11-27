"""Optional, cached PyPI update availability check for devup-cli."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_PYPI_URL = "https://pypi.org/pypi/devup-cli/json"
_CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass(slots=True)
class UpdateStatus:
    """Represents current vs latest PyPI release version."""

    current: str
    latest: str
    update_available: bool


def _parse_version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for raw in version.strip().lstrip("v").split("."):
        digits = ""
        for ch in raw:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
        else:
            break
    return tuple(parts) or (0,)


def _cache_file() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "devup" / "update_check.json"


def check_for_update(
    current_version: str,
    force: bool = False,
    timeout: float = 0.8,
) -> UpdateStatus | None:
    """Check PyPI for a newer version of devup-cli, caching results for 24 hours."""
    if os.environ.get("DEVUP_NO_UPDATE_CHECK") in ("1", "true", "yes"):
        return None

    cache_path = _cache_file()
    now = time.time()
    if not force and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if now - float(cached.get("checked_at", 0)) < _CACHE_TTL_SECONDS:
                latest = str(cached["latest"])
                return UpdateStatus(
                    current=current_version,
                    latest=latest,
                    update_available=_parse_version_tuple(latest)
                    > _parse_version_tuple(current_version),
                )
        except Exception:
            pass

    req = urllib.request.Request(
        _PYPI_URL,
        headers={"User-Agent": f"devup-cli/{current_version}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        latest = str(payload["info"]["version"])
    except (OSError, urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError):
        return None

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"checked_at": now, "latest": latest}),
            encoding="utf-8",
        )
    except OSError:
        pass

    return UpdateStatus(
        current=current_version,
        latest=latest,
        update_available=_parse_version_tuple(latest) > _parse_version_tuple(current_version),
    )
