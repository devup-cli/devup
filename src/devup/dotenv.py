"""Zero-dependency .env parser and loader."""

from __future__ import annotations

import re
from pathlib import Path

_KEY_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _unescape_double_quoted(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _parse_quoted_value(raw: str, quote: str) -> str:
    """Extract value inside matching quotes, handling escapes for double quotes."""
    chars: list[str] = []
    escaped = False
    for ch in raw[1:]:
        if quote == '"' and escaped:
            chars.append("\\" + ch)
            escaped = False
            continue
        if quote == '"' and ch == "\\":
            escaped = True
            continue
        if ch == quote:
            joined = "".join(chars)
            return _unescape_double_quoted(joined) if quote == '"' else joined
        chars.append(ch)
    # Unclosed quote fallback: return stripped content after opening quote.
    joined = "".join(chars)
    return _unescape_double_quoted(joined) if quote == '"' else joined


def _strip_unquoted_comment(raw: str) -> str:
    """Strip inline `# comment` only when preceded by whitespace."""
    for idx, ch in enumerate(raw):
        if ch == "#" and idx > 0 and raw[idx - 1].isspace():
            return raw[:idx].strip()
    return raw.strip()


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse key=value pairs from .env file text."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _KEY_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        raw_val = match.group(2).lstrip()
        if not raw_val:
            result[key] = ""
        elif raw_val[0] in ('"', "'"):
            result[key] = _parse_quoted_value(raw_val, raw_val[0])
        else:
            result[key] = _strip_unquoted_comment(raw_val)
    return result


def load_env_files(
    file_refs: list[str | Path],
    base_dir: Path,
    warn_missing: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """Load and merge one or more .env files relative to base_dir."""
    merged: dict[str, str] = {}
    warnings: list[str] = []

    for ref in file_refs:
        path = Path(ref)
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if not path.is_file():
            if warn_missing:
                warnings.append(f"Env file not found: {path}")
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"Failed to read env file {path}: {exc}")
            continue
        merged.update(parse_dotenv(content))

    return merged, warnings
