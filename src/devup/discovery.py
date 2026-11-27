"""Polyglot project script and service discovery."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

from devup.models import Task, WorkspaceConfig

_COMPOSE_FILES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
_JUSTFILES = ("justfile", "Justfile", ".justfile")
_MAKEFILES = ("GNUmakefile", "Makefile", "makefile")

_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)(?:\s*[^#\n]*)(?:#+\s*(.*))?$")
_JUST_RECIPE_RE = re.compile(
    r"^@?([A-Za-z][A-Za-z0-9_-]*)(?:\s+[^:=\n]+?)?\s*:\s*(?:#\s*(.*))?$"
)


def _register_task(
    tasks: dict[str, Task],
    task: Task,
    qualifier: str,
    allow_override: bool = False,
) -> None:
    """Register a task, qualifying its name if another source already claimed it."""
    if allow_override or task.name not in tasks:
        tasks[task.name] = task
        return

    qualified_name = f"{qualifier}:{task.name}"
    if qualified_name not in tasks:
        task.name = qualified_name
        tasks[qualified_name] = task


def _detect_node_runner(directory: Path, root: Path) -> str:
    """Detect Node package manager from lockfiles in the directory or repo root."""
    search_dirs = [directory] if directory == root else [directory, root]
    for base in search_dirs:
        if (base / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (base / "yarn.lock").exists():
            return "yarn"
        if (base / "bun.lockb").exists() or (base / "bun.lock").exists():
            return "bun"
        if (base / "package-lock.json").exists():
            return "npm"
    return "npm"


def _discover_package_json(
    directory: Path,
    root: Path,
    prefix: str = "",
) -> tuple[list[Task], list[str], list[str]]:
    """Discover npm/pnpm/yarn/bun scripts and optional npm workspaces."""
    pkg_path = directory / "package.json"
    if not pkg_path.is_file():
        return [], [], []

    tasks: list[Task] = []
    workspace_globs: list[str] = []
    warnings: list[str] = []

    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [], [f"Failed to parse {pkg_path}: {exc}"]

    if not isinstance(data, dict):
        return [], [], []

    runner = _detect_node_runner(directory, root)
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        for name, cmd in scripts.items():
            if not isinstance(name, str) or not isinstance(cmd, str):
                continue
            if name.startswith("//") or not cmd.strip():
                continue
            run_cmd = f"{runner} {name}" if runner == "yarn" else f"{runner} run {name}"
            task_name = f"{prefix}{name}"
            tasks.append(
                Task(
                    name=task_name,
                    command=run_cmd,
                    source="package.json",
                    runner=runner,
                    description=cmd.strip(),
                    cwd=directory,
                    workspace=prefix.rstrip(":") or None,
                )
            )

    raw_workspaces = data.get("workspaces")
    if isinstance(raw_workspaces, list):
        workspace_globs = [w for w in raw_workspaces if isinstance(w, str)]
    elif isinstance(raw_workspaces, dict) and isinstance(raw_workspaces.get("packages"), list):
        workspace_globs = [w for w in raw_workspaces["packages"] if isinstance(w, str)]

    return tasks, workspace_globs, warnings


def _format_poe_or_pdm_desc(entry: Any) -> str:
    """Extract a readable description from a structured Poe/PDM script entry."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        if isinstance(entry.get("help"), str) and entry["help"].strip():
            return entry["help"].strip()
        for key in ("cmd", "shell", "script", "composite"):
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list):
                return " && ".join(str(v) for v in val)
    return ""


def _discover_pyproject(
    directory: Path,
    prefix: str = "",
) -> tuple[list[Task], list[str]]:
    """Discover tasks from pyproject.toml (Poe, PDM, Hatch, Poetry, project.scripts)."""
    pyproject_path = directory / "pyproject.toml"
    if not pyproject_path.is_file():
        return [], []

    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [f"Failed to parse {pyproject_path}: {exc}"]

    tasks: list[Task] = []
    seen: set[str] = set()
    tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}

    def add_task(name: str, command: str, runner: str, description: str) -> None:
        full_name = f"{prefix}{name}"
        if full_name in seen:
            return
        seen.add(full_name)
        tasks.append(
            Task(
                name=full_name,
                command=command,
                source="pyproject.toml",
                runner=runner,
                description=description,
                cwd=directory,
                workspace=prefix.rstrip(":") or None,
            )
        )

    # 1. Poe the Poet ([tool.poe.tasks])
    poe_tasks = tool.get("poe", {}).get("tasks") if isinstance(tool.get("poe"), dict) else None
    if isinstance(poe_tasks, dict):
        for name, entry in poe_tasks.items():
            if not isinstance(name, str) or name.startswith("_"):
                continue
            add_task(name, f"poe {name}", "poe", _format_poe_or_pdm_desc(entry))

    # 2. PDM scripts ([tool.pdm.scripts])
    pdm_scripts = tool.get("pdm", {}).get("scripts") if isinstance(tool.get("pdm"), dict) else None
    if isinstance(pdm_scripts, dict):
        for name, entry in pdm_scripts.items():
            if not isinstance(name, str) or name.startswith("_") or name == "_":
                continue
            add_task(name, f"pdm run {name}", "pdm", _format_poe_or_pdm_desc(entry))

    # 3. Hatch scripts ([tool.hatch.envs.<env>.scripts])
    hatch_envs = tool.get("hatch", {}).get("envs") if isinstance(tool.get("hatch"), dict) else None
    if isinstance(hatch_envs, dict):
        for env_name, env_cfg in hatch_envs.items():
            if not isinstance(env_cfg, dict):
                continue
            scripts = env_cfg.get("scripts")
            if not isinstance(scripts, dict):
                continue
            for name, entry in scripts.items():
                if not isinstance(name, str):
                    continue
                target_name = name if env_name == "default" else f"{env_name}:{name}"
                hatch_target = name if env_name == "default" else f"{env_name}:{name}"
                desc = entry if isinstance(entry, str) else " | ".join(map(str, entry)) if isinstance(entry, list) else ""
                add_task(target_name, f"hatch run {hatch_target}", "hatch", desc)

    # 4. Poetry scripts ([tool.poetry.scripts])
    poetry_scripts = tool.get("poetry", {}).get("scripts") if isinstance(tool.get("poetry"), dict) else None
    if isinstance(poetry_scripts, dict):
        for name, entry in poetry_scripts.items():
            if isinstance(name, str):
                desc = entry if isinstance(entry, str) else str(entry)
                add_task(name, f"poetry run {name}", "poetry", desc)

    # 5. Standard [project.scripts]
    project_scripts = (
        data.get("project", {}).get("scripts")
        if isinstance(data.get("project"), dict)
        else None
    )
    if isinstance(project_scripts, dict):
        for name, entry in project_scripts.items():
            if isinstance(name, str) and isinstance(entry, str):
                add_task(name, name, "python", entry)

    return tasks, []


def _discover_makefile(directory: Path, prefix: str = "") -> tuple[list[Task], list[str]]:
    """Discover public targets and doc comments from a Makefile."""
    makefile_path = next((directory / name for name in _MAKEFILES if (directory / name).is_file()), None)
    if makefile_path is None:
        return [], []

    try:
        lines = makefile_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [], [f"Failed to read {makefile_path}: {exc}"]

    tasks: list[Task] = []
    seen: set[str] = set()
    prev_comment = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            prev_comment = ""
            continue
        if stripped.startswith("#"):
            prev_comment = stripped.lstrip("#").strip()
            continue
        if line.startswith(("\t", " ")):
            prev_comment = ""
            continue

        match = _MAKE_TARGET_RE.match(line)
        if match:
            target = match.group(1)
            inline_doc = (match.group(2) or "").strip()
            if not target.startswith(".") and target not in seen:
                seen.add(target)
                tasks.append(
                    Task(
                        name=f"{prefix}{target}",
                        command=f"make {target}",
                        source=makefile_path.name,
                        runner="make",
                        description=inline_doc or prev_comment,
                        cwd=directory,
                        workspace=prefix.rstrip(":") or None,
                    )
                )
        prev_comment = ""

    return tasks, []


def _discover_justfile(directory: Path, prefix: str = "") -> tuple[list[Task], list[str]]:
    """Discover public recipes from a Justfile."""
    justfile_path = next((directory / name for name in _JUSTFILES if (directory / name).is_file()), None)
    if justfile_path is None:
        return [], []

    try:
        lines = justfile_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [], [f"Failed to read {justfile_path}: {exc}"]

    tasks: list[Task] = []
    seen: set[str] = set()
    prev_comment = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            prev_comment = ""
            continue
        if stripped.startswith("#"):
            prev_comment = stripped.lstrip("#").strip()
            continue
        if line.startswith(("\t", " ")):
            prev_comment = ""
            continue
        # Skip variable assignments or directives like `set dotenv-load` / `alias b := build`
        if stripped.startswith(("set ", "alias ", "export ", "import ", "mod ")):
            prev_comment = ""
            continue

        match = _JUST_RECIPE_RE.match(line)
        if match:
            recipe = match.group(1)
            inline_doc = (match.group(2) or "").strip()
            if not recipe.startswith("_") and recipe not in seen:
                seen.add(recipe)
                tasks.append(
                    Task(
                        name=f"{prefix}{recipe}",
                        command=f"just {recipe}",
                        source=justfile_path.name,
                        runner="just",
                        description=inline_doc or prev_comment,
                        cwd=directory,
                        workspace=prefix.rstrip(":") or None,
                    )
                )
        prev_comment = ""

    return tasks, []


def _parse_compose_services(text: str) -> list[str]:
    """Extract top-level service names from a Docker Compose YAML file without PyYAML."""
    services: list[str] = []
    in_services = False
    service_indent: int | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        if indent == 0:
            if stripped.startswith("services:"):
                in_services = True
                service_indent = None
            else:
                in_services = False
            continue

        if not in_services:
            continue

        if service_indent is None:
            service_indent = indent

        if indent == service_indent and ":" in stripped and not stripped.startswith("-"):
            key = stripped.split(":", 1)[0].strip().strip("\"'")
            if key:
                services.append(key)

    return services


def _detect_compose_binary() -> str:
    """Prefer `docker compose`, falling back to `docker-compose` if present."""
    if shutil.which("docker") is not None:
        return "docker compose"
    if shutil.which("docker-compose") is not None:
        return "docker-compose"
    return "docker compose"


def _discover_compose(directory: Path, prefix: str = "") -> tuple[list[Task], list[str]]:
    """Discover Docker Compose up/down and per-service tasks."""
    compose_path = next((directory / name for name in _COMPOSE_FILES if (directory / name).is_file()), None)
    if compose_path is None:
        return [], []

    try:
        content = compose_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"Failed to read {compose_path}: {exc}"]

    services = _parse_compose_services(content)
    if not services:
        return [], []

    compose_cmd = _detect_compose_binary()
    tasks = [
        Task(
            name=f"{prefix}compose:up",
            command=f"{compose_cmd} up",
            source=compose_path.name,
            runner="docker-compose",
            description=f"Start all services ({', '.join(services)})",
            cwd=directory,
            workspace=prefix.rstrip(":") or None,
        ),
        Task(
            name=f"{prefix}compose:down",
            command=f"{compose_cmd} down",
            source=compose_path.name,
            runner="docker-compose",
            description="Stop and remove compose containers",
            cwd=directory,
            workspace=prefix.rstrip(":") or None,
        ),
    ]

    for svc in services:
        tasks.append(
            Task(
                name=f"{prefix}compose:{svc}",
                command=f"{compose_cmd} up {svc}",
                source=compose_path.name,
                runner="docker-compose",
                description=f"Start Docker Compose service '{svc}'",
                cwd=directory,
                workspace=prefix.rstrip(":") or None,
            )
        )

    return tasks, []


def _resolve_workspace_dirs(root: Path, patterns: list[str]) -> list[Path]:
    """Expand workspace glob patterns into sorted unique subdirectories."""
    dirs: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        clean = pattern.strip().rstrip("/")
        if not clean or clean.startswith("!"):
            continue
        for candidate in sorted(root.glob(clean)):
            if candidate.is_dir() and candidate.resolve() != root.resolve():
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    dirs.append(candidate)
    return dirs


def _discover_directory(
    directory: Path,
    root: Path,
    tasks: dict[str, Task],
    warnings: list[str],
    prefix: str = "",
) -> list[str]:
    """Run all manifest scanners in priority order on a single directory."""
    pkg_tasks, pkg_workspaces, pkg_warnings = _discover_package_json(directory, root, prefix=prefix)
    warnings.extend(pkg_warnings)
    for task in pkg_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}node")

    py_tasks, py_warnings = _discover_pyproject(directory, prefix=prefix)
    warnings.extend(py_warnings)
    for task in py_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}py")

    just_tasks, just_warnings = _discover_justfile(directory, prefix=prefix)
    warnings.extend(just_warnings)
    for task in just_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}just")

    make_tasks, make_warnings = _discover_makefile(directory, prefix=prefix)
    warnings.extend(make_warnings)
    for task in make_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}make")

    compose_tasks, compose_warnings = _discover_compose(directory, prefix=prefix)
    warnings.extend(compose_warnings)
    for task in compose_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}compose")

    return pkg_workspaces


def _load_devup_toml(root: Path, config: WorkspaceConfig) -> list[str]:
    """Load explicit devup.toml configuration, returning any configured workspace globs."""
    toml_path = root / "devup.toml"
    if not toml_path.is_file():
        return []

    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        config.warnings.append(f"Failed to parse {toml_path}: {exc}")
        return []

    if not isinstance(data, dict):
        return []

    if isinstance(data.get("fail_fast"), bool):
        config.fail_fast = data["fail_fast"]

    if isinstance(data.get("shutdown_timeout"), (int, float)):
        config.shutdown_timeout = max(0.5, float(data["shutdown_timeout"]))

    if isinstance(data.get("default"), list):
        config.default_targets = [str(item) for item in data["default"] if isinstance(item, str)]
    elif isinstance(data.get("default"), str):
        config.default_targets = [data["default"]]

    raw_env = data.get("env")
    if isinstance(raw_env, dict):
        config.env = {str(k): str(v) for k, v in raw_env.items()}

    raw_groups = data.get("groups")
    if isinstance(raw_groups, dict):
        for group_name, members in raw_groups.items():
            if isinstance(group_name, str) and isinstance(members, list):
                config.groups[group_name] = [str(m) for m in members if isinstance(m, str)]

    raw_tasks = data.get("tasks")
    if isinstance(raw_tasks, dict):
        for task_name, entry in raw_tasks.items():
            if not isinstance(task_name, str):
                continue
            if isinstance(entry, str):
                _register_task(
                    config.tasks,
                    Task(
                        name=task_name,
                        command=entry,
                        source="devup.toml",
                        runner="devup",
                        cwd=root,
                    ),
                    qualifier="devup",
                    allow_override=True,
                )
            elif isinstance(entry, dict) and isinstance(entry.get("command"), str):
                raw_cwd = entry.get("cwd")
                task_cwd = (root / raw_cwd).resolve() if isinstance(raw_cwd, str) else root
                task_env = (
                    {str(k): str(v) for k, v in entry["env"].items()}
                    if isinstance(entry.get("env"), dict)
                    else {}
                )
                deps = (
                    [str(d) for d in entry["depends_on"] if isinstance(d, str)]
                    if isinstance(entry.get("depends_on"), list)
                    else []
                )
                ready = entry.get("ready_pattern") if isinstance(entry.get("ready_pattern"), str) else None
                desc = entry.get("description") if isinstance(entry.get("description"), str) else ""
                _register_task(
                    config.tasks,
                    Task(
                        name=task_name,
                        command=entry["command"],
                        source="devup.toml",
                        runner="devup",
                        description=desc,
                        cwd=task_cwd,
                        env=task_env,
                        depends_on=deps,
                        ready_pattern=ready,
                    ),
                    qualifier="devup",
                    allow_override=True,
                )

    raw_workspaces = data.get("workspaces")
    if isinstance(raw_workspaces, list):
        return [str(w) for w in raw_workspaces if isinstance(w, str)]
    return []


def discover_workspace(root: Path, scan_workspaces: bool = True) -> WorkspaceConfig:
    """Discover tasks and groups for a project root and its configured workspaces."""
    resolved_root = root.resolve()
    config = WorkspaceConfig(root=resolved_root)

    # First discover manifests at root, then apply devup.toml overrides on top.
    pkg_workspaces = _discover_directory(
        resolved_root,
        resolved_root,
        config.tasks,
        config.warnings,
    )
    toml_workspaces = _load_devup_toml(resolved_root, config)

    if scan_workspaces:
        workspace_globs = toml_workspaces or pkg_workspaces
        for ws_dir in _resolve_workspace_dirs(resolved_root, workspace_globs):
            ws_name = ws_dir.name
            _discover_directory(
                ws_dir,
                resolved_root,
                config.tasks,
                config.warnings,
                prefix=f"{ws_name}:",
            )
            # Re-apply devup.toml so root explicit tasks always win over workspace collisions.
        if toml_workspaces or pkg_workspaces:
            _load_devup_toml(resolved_root, config)

    return config


def resolve_targets(config: WorkspaceConfig, targets: list[str]) -> list[str]:
    """Expand group names and validate that all requested targets exist."""
    expanded: list[str] = []
    seen: set[str] = set()

    def expand_one(name: str, stack: set[str]) -> None:
        if name in stack:
            raise ValueError(f"Circular group reference detected: {name}")
        if name in config.groups:
            stack.add(name)
            for member in config.groups[name]:
                expand_one(member, stack)
            stack.remove(name)
            return
        if name not in config.tasks:
            available = ", ".join(sorted(list(config.tasks.keys()) + list(config.groups.keys())))
            raise KeyError(f"Unknown task or group '{name}'. Available: {available or 'none'}")
        if name not in seen:
            seen.add(name)
            expanded.append(name)

    for target in targets:
        expand_one(target, set())

    return expanded
