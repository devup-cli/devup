"""Configuration validation and redacted environment diagnostics for devup."""

from __future__ import annotations

import getpass
import os
import platform
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devup import __version__
from devup.discovery import discover_workspace, resolve_targets
from devup.dotenv import load_env_files
from devup.ports import extract_ports_from_command
from devup.readiness import normalize_tcp_target, validate_ready_url
from devup.runner import _expand_dependencies
from devup.updates import check_for_update

_KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "default",
        "fail_fast",
        "shutdown_timeout",
        "auto_env",
        "env_file",
        "strict_ports",
        "workspaces",
        "env",
        "groups",
        "tasks",
    }
)

_KNOWN_TASK_KEYS = frozenset(
    {
        "command",
        "description",
        "cwd",
        "env",
        "env_file",
        "depends_on",
        "ready_pattern",
        "ready_url",
        "ready_tcp",
        "ready_port",
        "ready_timeout",
        "port",
        "ports",
        "watch",
    }
)

_TOOL_BINARIES = (
    "git",
    "node",
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "poetry",
    "pdm",
    "hatch",
    "poe",
    "make",
    "just",
    "docker",
    "java",
    "mvn",
    "gradle",
)

_SENSITIVE_KEY_RE = re.compile(
    r"(SECRET|TOKEN|KEY|PASS|PWD|AUTH|COOKIE|CREDENTIAL|PRIVATE|CERT|DSN|DATABASE_URL|CONN_STR)",
    re.IGNORECASE,
)

# Match scheme://user[:pass]@host (excluding whitespace and quotes)
_URL_USERINFO_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://)([^/\s:@\"']+(?::[^/\s@\"']*)?)@"
)

# Match KEY=VALUE or KEY: VALUE where KEY contains a sensitive word
_KV_SECRET_RE = re.compile(
    r"""(\b[A-Za-z0-9_]*(?:SECRET|TOKEN|KEY|PASS|PASSWORD|PWD|AUTH|COOKIE|CREDENTIAL|PRIVATE|DSN|DATABASE_URL)[A-Za-z0-9_]*\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
    re.IGNORECASE,
)

# Match Bearer / Basic authorization tokens
_BEARER_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)

# Match sensitive CLI flags like --token xyz, --password=abc, --api-key "xyz"
_CLI_SECRET_FLAG_RE = re.compile(
    r"""(--(?:token|password|passwd|secret|api[-_]?key|auth[-_]?token)\s*(?:=|\s)\s*)(?:"[^"]*"|'[^']*'|[^\s]+)""",
    re.IGNORECASE,
)

# Match home directories across Windows/macOS/Linux even for arbitrary usernames
_WIN_USER_DIR_RE = re.compile(r'([A-Za-z]:[\\/]+Users[\\/]+)([^\\/:*?"<>|\s\'"]+)', re.IGNORECASE)
_POSIX_USER_DIR_RE = re.compile(r"(/(?:Users|home)/)([^/\s'\"\\]+)")


@dataclass(slots=True)
class ValidationReport:
    """Result of validating a project's devup.toml and discovered tasks."""

    valid: bool
    root: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    task_count: int = 0
    group_count: int = 0

    def to_dict(self, redact_paths: bool = False) -> dict[str, Any]:
        root_str = redact_path(self.root) if redact_paths else str(self.root)
        return {
            "valid": self.valid,
            "root": root_str,
            "task_count": self.task_count,
            "group_count": self.group_count,
            "errors": [redact_text(e) if redact_paths else e for e in self.errors],
            "warnings": [redact_text(w) if redact_paths else w for w in self.warnings],
        }


def _current_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def redact_path(path: Path | str) -> str:
    """Replace user home directory, OS username segments, and secrets with safe placeholders."""
    text = str(path)
    home = str(Path.home())
    if home:
        for variant in (home, home.replace("\\", "/")):
            if text.lower().startswith(variant.lower()):
                text = "~" + text[len(variant) :]
                break
    return redact_text(text)


def redact_text(text: str) -> str:
    """Redact home directories, usernames, URL credentials, and secrets from text."""
    home = str(Path.home())
    if home:
        for variant in (home, home.replace("\\", "/")):
            text = re.sub(re.escape(variant), "~", text, flags=re.IGNORECASE)

    text = _WIN_USER_DIR_RE.sub(r"\1<user>", text)
    text = _POSIX_USER_DIR_RE.sub(r"\1<user>", text)

    username = _current_username()
    if username and len(username) >= 3:
        text = re.sub(
            rf"(?<![A-Za-z0-9_-]){re.escape(username)}(?![A-Za-z0-9_-])",
            "<user>",
            text,
            flags=re.IGNORECASE if os.name == "nt" else 0,
        )

    text = _URL_USERINFO_RE.sub(r"\1<redacted>@", text)
    text = _KV_SECRET_RE.sub(r"\1<redacted>", text)
    text = _BEARER_RE.sub(r"\1 <redacted>", text)
    text = _CLI_SECRET_FLAG_RE.sub(r"\1<redacted>", text)

    # Scrub literal values of sensitive environment variables if present in errors or commands.
    for key, val in os.environ.items():
        if val and len(val) >= 6 and _SENSITIVE_KEY_RE.search(key):
            if val in text:
                text = text.replace(val, "<redacted>")

    return text


def _validate_raw_toml(
    root: Path,
    errors: list[str],
    warnings: list[str],
) -> None:
    toml_path = root / "devup.toml"
    if not toml_path.is_file():
        return

    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"Invalid TOML in devup.toml: {exc}")
        return

    if not isinstance(data, dict):
        errors.append("devup.toml root must be a TOML table.")
        return

    for key in data:
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            warnings.append(f"Unknown top-level key '{key}' in devup.toml.")

    raw_tasks = data.get("tasks")
    if raw_tasks is not None and not isinstance(raw_tasks, dict):
        errors.append("devup.toml `[tasks]` must be a table.")
        return

    if isinstance(raw_tasks, dict):
        for tname, entry in raw_tasks.items():
            if isinstance(entry, str):
                continue
            if not isinstance(entry, dict):
                errors.append(f"Task '{tname}' must be a command string or table.")
                continue
            for key in entry:
                if key not in _KNOWN_TASK_KEYS:
                    warnings.append(f"Task '{tname}' has unknown key '{key}'.")
            if not isinstance(entry.get("command"), str) or not entry["command"].strip():
                errors.append(f"Task '{tname}' is missing a non-empty 'command' string.")
            if "ready_url" in entry:
                if not isinstance(entry["ready_url"], str):
                    errors.append(f"Task '{tname}': ready_url must be a string.")
                else:
                    try:
                        validate_ready_url(entry["ready_url"])
                    except ValueError as exc:
                        errors.append(f"Task '{tname}': {exc}")
            raw_tcp = entry.get("ready_tcp") if "ready_tcp" in entry else entry.get("ready_port")
            if raw_tcp is not None:
                if not isinstance(raw_tcp, (str, int)) or isinstance(raw_tcp, bool):
                    errors.append(f"Task '{tname}': ready_tcp/ready_port must be a port or host:port.")
                else:
                    try:
                        normalize_tcp_target(raw_tcp)
                    except ValueError as exc:
                        errors.append(f"Task '{tname}': {exc}")
            if "port" in entry:
                p = entry["port"]
                if not isinstance(p, int) or isinstance(p, bool) or not (1 <= p <= 65535):
                    errors.append(f"Task '{tname}': port must be an integer between 1 and 65535.")
            if "ports" in entry:
                ps = entry["ports"]
                if not isinstance(ps, list) or any(
                    not isinstance(p, int) or isinstance(p, bool) or not (1 <= p <= 65535)
                    for p in ps
                ):
                    errors.append(f"Task '{tname}': ports must be a list of integers (1-65535).")


def validate_workspace(root: Path, scan_workspaces: bool = True) -> ValidationReport:
    """Validate devup.toml, groups, dependencies, working directories, env files, and ports."""
    resolved_root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []

    _validate_raw_toml(resolved_root, errors, warnings)
    config = discover_workspace(resolved_root, scan_workspaces=scan_workspaces)
    warnings.extend(config.warnings)

    for env_ref in config.env_files:
        env_path = Path(env_ref)
        if not env_path.is_absolute():
            env_path = (resolved_root / env_path).resolve()
        if not env_path.is_file():
            warnings.append(f"Workspace env_file does not exist: {env_path}")

    for group_name, members in config.groups.items():
        if not members:
            warnings.append(f"Group '{group_name}' is empty.")
            continue
        try:
            expanded = resolve_targets(config, [group_name])
            _expand_dependencies(config, expanded)
        except (KeyError, ValueError) as exc:
            errors.append(f"Group '{group_name}': {exc}")

    if config.default_targets:
        try:
            expanded_default = resolve_targets(config, config.default_targets)
            _expand_dependencies(config, expanded_default)
        except (KeyError, ValueError) as exc:
            errors.append(f"Default target error: {exc}")

    claimed_ports: dict[int, list[str]] = {}
    for name, task in config.tasks.items():
        if task.cwd is not None and not task.cwd.is_dir():
            if task.source == "devup.toml":
                errors.append(f"Task '{name}' cwd does not exist: {task.cwd}")
            else:
                warnings.append(f"Task '{name}' cwd does not exist: {task.cwd}")

        for env_ref in task.env_files:
            env_path = Path(env_ref)
            base_dir = task.cwd or resolved_root
            if not env_path.is_absolute():
                env_path = (base_dir / env_path).resolve()
            if not env_path.is_file():
                warnings.append(f"Task '{name}' env_file does not exist: {env_path}")

        if task.depends_on:
            try:
                _expand_dependencies(config, [name])
            except (KeyError, ValueError) as exc:
                errors.append(f"Task '{name}': {exc}")

        ports = list(task.ports) if task.ports else extract_ports_from_command(task.command)
        for port in ports:
            claimed_ports.setdefault(port, []).append(name)

    for port, owners in claimed_ports.items():
        if len(owners) > 1:
            warnings.append(
                f"Port {port} is declared by multiple tasks ({', '.join(owners)})."
            )

    return ValidationReport(
        valid=not errors,
        root=resolved_root,
        errors=errors,
        warnings=warnings,
        task_count=len(config.tasks),
        group_count=len(config.groups),
    )


def run_doctor(
    root: Path,
    online: bool = False,
    scan_workspaces: bool = True,
) -> dict[str, Any]:
    """Collect redacted environment, toolchain, discovery, and validation diagnostics."""
    resolved_root = root.resolve()
    validation = validate_workspace(resolved_root, scan_workspaces=scan_workspaces)
    config = discover_workspace(resolved_root, scan_workspaces=scan_workspaces)

    runners: dict[str, dict[str, Any]] = {}
    for tool in _TOOL_BINARIES:
        found = shutil.which(tool)
        runners[tool] = {
            "available": found is not None,
            "path": redact_path(found) if found else None,
        }

    dotenv_keys: list[str] = []
    if (resolved_root / ".env").is_file():
        vals, _ = load_env_files([resolved_root / ".env"], resolved_root, warn_missing=False)
        dotenv_keys = sorted(vals.keys())

    sensitive_env_count = sum(1 for k in os.environ if _SENSITIVE_KEY_RE.search(k))
    update_info: dict[str, Any] | None = None
    if online:
        status = check_for_update(__version__, force=True)
        if status is not None:
            update_info = {
                "current": status.current,
                "latest": status.latest,
                "update_available": status.update_available,
            }

    return {
        "devup_version": __version__,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "os": os.name,
        "cwd": redact_path(resolved_root),
        "devup_toml_present": (resolved_root / "devup.toml").is_file(),
        "dotenv_present": (resolved_root / ".env").is_file(),
        "dotenv_keys": dotenv_keys,
        "redacted_env_vars_count": sensitive_env_count,
        "sources": sorted({t.source for t in config.tasks.values()}),
        "runners": runners,
        "validation": validation.to_dict(redact_paths=True),
        "update_check": update_info,
    }
