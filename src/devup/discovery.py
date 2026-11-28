"""Polyglot project script and service discovery."""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from devup.models import Task, WorkspaceConfig
from devup.readiness import normalize_tcp_target, validate_ready_url

_COMPOSE_FILES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
_JUSTFILES = ("justfile", "Justfile", ".justfile")
_MAKEFILES = ("GNUmakefile", "Makefile", "makefile")
_GRADLE_FILES = ("build.gradle.kts", "build.gradle")
_GRADLE_SETTINGS_FILES = ("settings.gradle.kts", "settings.gradle")
_TASKFILES = ("Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml")
_MISE_FILES = ("mise.toml", ".mise.toml", ".config/mise.toml")

_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)(?:\s*[^#\n]*)(?:#+\s*(.*))?$")
_JUST_RECIPE_RE = re.compile(
    r"^@?([A-Za-z][A-Za-z0-9_-]*)(?:\s+[^:=\n]+?)?\s*:\s*(?:#\s*(.*))?$"
)
_GRADLE_REGISTER_RE = re.compile(
    r"""tasks\.register(?:<[^>]+>)?\(\s*["']([A-Za-z0-9_-]+)["']"""
)
_GRADLE_KTS_DELEGATE_RE = re.compile(
    r"""val\s+([A-Za-z][A-Za-z0-9_-]*)\s+by\s+tasks\.(?:registering|creating)"""
)
_GRADLE_GROOVY_TASK_RE = re.compile(
    r"""^\s*task\s+([A-Za-z][A-Za-z0-9_-]*)\s*(?:\(|{|$)"""
)
_GRADLE_INCLUDE_RE = re.compile(r"""include\s*\(?\s*([^\n\)]+)""")


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
        return [], [], [f"Failed to parse {pkg_path} in repo {root}: {exc}. Check JSON syntax and run `devup doctor`."]

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
        return [], [f"Failed to parse {pyproject_path} in repo {directory}: {exc}. Check TOML syntax."]

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


def _format_wrapper_cmd(wrapper_path: Path, directory: Path) -> str:
    """Format wrapper script path relative to directory for shell invocation."""
    rel = os.path.relpath(wrapper_path, directory)
    if os.name == "nt":
        return f'"{rel}"' if " " in rel else rel
    if not rel.startswith((".", "/")):
        rel = f"./{rel}"
    return f'"{rel}"' if " " in rel else rel


def _detect_maven_runner(directory: Path, root: Path) -> str:
    """Detect Maven wrapper (mvnw.cmd / ./mvnw) or fall back to mvn."""
    wrapper_name = "mvnw.cmd" if os.name == "nt" else "mvnw"
    search_dirs = [directory] if directory == root else [directory, root]
    for base in search_dirs:
        candidate = base / wrapper_name
        if candidate.is_file():
            return _format_wrapper_cmd(candidate, directory)
    return "mvn"


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _discover_maven(
    directory: Path,
    root: Path,
    prefix: str = "",
) -> tuple[list[Task], list[str], list[str]]:
    """Discover Maven lifecycle goals, plugins, and sub-modules from pom.xml."""
    pom_path = directory / "pom.xml"
    if not pom_path.is_file():
        return [], [], []

    try:
        tree = ET.parse(pom_path)
    except (OSError, ET.ParseError) as exc:
        return [], [], [f"Failed to parse {pom_path} in repo {root}: {exc}. Check XML syntax."]

    xml_root = tree.getroot()
    mvn = _detect_maven_runner(directory, root)
    tasks: list[Task] = []
    seen: set[str] = set()
    modules: list[str] = []

    def add_task(name: str, goal: str, description: str) -> None:
        full_name = f"{prefix}{name}"
        if full_name in seen:
            return
        seen.add(full_name)
        tasks.append(
            Task(
                name=full_name,
                command=f"{mvn} {goal}",
                source="pom.xml",
                runner="maven",
                description=description,
                cwd=directory,
                workspace=prefix.rstrip(":") or None,
            )
        )

    for elem in xml_root.iter():
        local = _xml_local_name(elem.tag)
        if local == "module" and elem.text and elem.text.strip():
            modules.append(elem.text.strip())
        elif local == "artifactId" and elem.text:
            artifact = elem.text.strip()
            if artifact == "spring-boot-maven-plugin":
                add_task("dev", "spring-boot:run", "Run Spring Boot application")
            elif artifact == "quarkus-maven-plugin":
                add_task("dev", "quarkus:dev", "Run Quarkus in dev mode")
            elif artifact == "exec-maven-plugin":
                add_task("exec", "exec:java", "Run main class via exec-maven-plugin")

    for goal, desc in (
        ("compile", "Compile project sources"),
        ("test", "Run Maven tests"),
        ("package", "Package Maven artifact"),
        ("verify", "Run verification checks"),
        ("clean", "Clean Maven target directory"),
    ):
        add_task(goal, goal, desc)

    return tasks, modules, []


def _detect_gradle_runner(directory: Path, root: Path) -> str:
    """Detect Gradle wrapper (gradlew.bat / ./gradlew) or fall back to gradle."""
    wrapper_name = "gradlew.bat" if os.name == "nt" else "gradlew"
    search_dirs = [directory] if directory == root else [directory, root]
    for base in search_dirs:
        candidate = base / wrapper_name
        if candidate.is_file():
            return _format_wrapper_cmd(candidate, directory)
    return "gradle"


def _discover_gradle_subprojects(directory: Path) -> list[str]:
    """Extract subproject relative directory paths from settings.gradle(.kts)."""
    settings_path = next(
        (directory / name for name in _GRADLE_SETTINGS_FILES if (directory / name).is_file()),
        None,
    )
    if settings_path is None:
        return []
    try:
        text = settings_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    subprojects: list[str] = []
    for match in _GRADLE_INCLUDE_RE.finditer(text):
        args_blob = match.group(1)
        for quoted in re.findall(r"""["']([^"']+)["']""", args_blob):
            rel = quoted.lstrip(":").replace(":", "/").strip()
            if rel and rel not in subprojects:
                subprojects.append(rel)
    return subprojects


def _discover_gradle(
    directory: Path,
    root: Path,
    prefix: str = "",
) -> tuple[list[Task], list[str], list[str]]:
    """Discover Gradle tasks and subprojects from build.gradle(.kts) and settings.gradle(.kts)."""
    gradle_path = next(
        (directory / name for name in _GRADLE_FILES if (directory / name).is_file()),
        None,
    )
    subprojects = _discover_gradle_subprojects(directory)
    if gradle_path is None and not subprojects:
        return [], [], []

    gradle = _detect_gradle_runner(directory, root)
    tasks: list[Task] = []
    seen: set[str] = set()
    warnings: list[str] = []
    source_name = gradle_path.name if gradle_path is not None else "settings.gradle"

    def add_task(name: str, gradle_task: str, description: str) -> None:
        full_name = f"{prefix}{name}"
        if full_name in seen:
            return
        seen.add(full_name)
        tasks.append(
            Task(
                name=full_name,
                command=f"{gradle} {gradle_task}",
                source=source_name,
                runner="gradle",
                description=description,
                cwd=directory,
                workspace=prefix.rstrip(":") or None,
            )
        )

    if gradle_path is not None:
        try:
            content = gradle_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [], subprojects, [f"Failed to read {gradle_path}: {exc}"]

        if "org.springframework.boot" in content:
            add_task("bootRun", "bootRun", "Run Spring Boot application")
        if re.search(r"""\bapplication\b|\bmainClass\b""", content):
            add_task("run", "run", "Run Gradle application")

        for regex in (_GRADLE_REGISTER_RE, _GRADLE_KTS_DELEGATE_RE):
            for match in regex.finditer(content):
                tname = match.group(1)
                add_task(tname, tname, f"Custom Gradle task '{tname}'")
        for line in content.splitlines():
            match = _GRADLE_GROOVY_TASK_RE.match(line)
            if match:
                tname = match.group(1)
                add_task(tname, tname, f"Custom Gradle task '{tname}'")

    for task_name, desc in (
        ("build", "Assemble and test Gradle project"),
        ("test", "Run Gradle tests"),
        ("check", "Run Gradle verification checks"),
        ("clean", "Clean Gradle build directory"),
    ):
        add_task(task_name, task_name, desc)

    return tasks, subprojects, warnings


def _discover_cargo(
    directory: Path,
    root: Path,
    prefix: str = "",
) -> tuple[list[Task], list[str], list[str]]:
    """Discover Cargo tasks, binary targets, and [workspace] members from Cargo.toml."""
    cargo_path = directory / "Cargo.toml"
    if not cargo_path.is_file():
        return [], [], []

    try:
        data = tomllib.loads(cargo_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [], [f"Failed to parse {cargo_path} in repo {root}: {exc}"]

    if not isinstance(data, dict):
        return [], [], []

    tasks: list[Task] = []
    seen: set[str] = set()
    members: list[str] = []
    ws_name = prefix.rstrip(":") or None

    raw_ws = data.get("workspace")
    if isinstance(raw_ws, dict) and isinstance(raw_ws.get("members"), list):
        members = [str(m) for m in raw_ws["members"] if isinstance(m, str) and not str(m).startswith("!")]

    def add_task(name: str, cargo_cmd: str, description: str) -> None:
        full_name = f"{prefix}{name}"
        if full_name in seen:
            return
        seen.add(full_name)
        tasks.append(
            Task(
                name=full_name,
                command=cargo_cmd,
                source="Cargo.toml",
                runner="cargo",
                description=description,
                cwd=directory,
                workspace=ws_name,
            )
        )

    pkg = data.get("package") if isinstance(data.get("package"), dict) else None
    bins = data.get("bin") if isinstance(data.get("bin"), list) else []
    for entry in bins:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"].strip():
            bname = entry["name"].strip()
            add_task(f"run:{bname}", f"cargo run --bin {bname}", f"Run Cargo binary '{bname}'")

    if pkg is not None:
        has_main = (directory / "src" / "main.rs").is_file()
        has_lib = (directory / "src" / "lib.rs").is_file()
        if has_main or bins or not has_lib:
            add_task("run", "cargo run", f"Run Cargo package '{pkg.get('name', directory.name)}'")
        for sub, desc in (
            ("build", "Build Cargo package"),
            ("test", "Run Cargo tests"),
            ("check", "Type-check Cargo package"),
            ("clippy", "Run Clippy lints"),
        ):
            add_task(sub, f"cargo {sub}", desc)
    elif raw_ws is not None:
        for sub, desc in (
            ("build", "Build Cargo workspace"),
            ("test", "Run Cargo workspace tests"),
            ("check", "Check Cargo workspace"),
        ):
            add_task(sub, f"cargo {sub} --workspace", desc)

    return tasks, members, []


def _parse_go_work_modules(work_path: Path) -> list[str]:
    """Extract module paths from go.work `use` directives."""
    try:
        lines = work_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    modules: list[str] = []
    in_use = False
    for raw in lines:
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("use ("):
            in_use = True
            continue
        if in_use:
            if line.startswith(")"):
                in_use = False
                continue
            mod = line.strip().strip('"').lstrip("./")
            if mod and mod not in (".",) and mod not in modules:
                modules.append(mod)
        elif line.startswith("use "):
            mod = line[4:].strip().strip('"').lstrip("./")
            if mod and mod not in (".",) and mod not in modules:
                modules.append(mod)
    return modules


def _discover_go(
    directory: Path,
    root: Path,
    prefix: str = "",
) -> tuple[list[Task], list[str], list[str]]:
    """Discover Go tasks from go.mod and sub-modules from go.work."""
    gomod_path = directory / "go.mod"
    gowork_path = directory / "go.work"
    work_modules = _parse_go_work_modules(gowork_path) if gowork_path.is_file() else []
    if not gomod_path.is_file() and not work_modules:
        return [], [], []

    tasks: list[Task] = []
    seen: set[str] = set()
    ws_name = prefix.rstrip(":") or None
    source_name = "go.mod" if gomod_path.is_file() else "go.work"

    if gomod_path.is_file():
        try:
            mod_text = gomod_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [], work_modules, [f"Failed to read {gomod_path} in repo {root}: {exc}"]
        if "module " not in mod_text:
            return [], work_modules, [f"Malformed go.mod in repo {root} ({gomod_path}): missing 'module' directive."]

    def add_task(name: str, cmd: str, description: str) -> None:
        full_name = f"{prefix}{name}"
        if full_name in seen:
            return
        seen.add(full_name)
        tasks.append(
            Task(
                name=full_name,
                command=cmd,
                source=source_name,
                runner="go",
                description=description,
                cwd=directory,
                workspace=ws_name,
            )
        )

    cmd_dir = directory / "cmd"
    has_cmd_bins = False
    if cmd_dir.is_dir():
        for child in sorted(cmd_dir.iterdir()):
            if child.is_dir() and (child / "main.go").is_file():
                has_cmd_bins = True
                add_task(f"run:{child.name}", f"go run ./cmd/{child.name}", f"Run Go command './cmd/{child.name}'")

    if (directory / "main.go").is_file() or not has_cmd_bins:
        add_task("run", "go run .", "Run Go main package")

    for name, go_cmd, desc in (
        ("build", "go build ./...", "Build Go packages"),
        ("test", "go test ./...", "Run Go tests"),
        ("vet", "go vet ./...", "Run go vet static analyzer"),
    ):
        add_task(name, go_cmd, desc)

    return tasks, work_modules, []


def _parse_taskfile_yaml(text: str) -> dict[str, str]:
    """Extract public task names -> descriptions from Taskfile YAML without PyYAML."""
    tasks: dict[str, str] = {}
    in_tasks = False
    task_indent: int | None = None
    current_task: str | None = None
    prop_indent: int | None = None

    for raw_line in text.splitlines():
        stripped_comment = raw_line.split("#", 1)[0].rstrip()
        if not stripped_comment.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = stripped_comment.strip()

        if indent == 0:
            if stripped == "tasks:" or stripped.startswith("tasks:"):
                in_tasks = True
                task_indent = None
                current_task = None
                prop_indent = None
            else:
                in_tasks = False
                current_task = None
            continue

        if not in_tasks:
            continue

        if task_indent is None:
            task_indent = indent

        if indent == task_indent and ":" in stripped and not stripped.startswith("-"):
            key = stripped.split(":", 1)[0].strip().strip("\"'")
            current_task = key if key else None
            prop_indent = None
            if current_task and current_task not in tasks:
                tasks[current_task] = ""
            continue

        if current_task is not None and indent > task_indent:
            if prop_indent is None:
                prop_indent = indent
            if indent == prop_indent:
                if stripped.startswith(("desc:", "summary:")):
                    val = stripped.split(":", 1)[1].strip().strip("\"'")
                    if val and not tasks[current_task]:
                        tasks[current_task] = val
                elif stripped.startswith("internal:") and stripped.split(":", 1)[1].strip().lower() == "true":
                    tasks.pop(current_task, None)
                    current_task = None

    return tasks


def _discover_taskfile(directory: Path, root: Path, prefix: str = "") -> tuple[list[Task], list[str]]:
    """Discover tasks from Taskfile.yml / Taskfile.yaml."""
    taskfile_path = next((directory / name for name in _TASKFILES if (directory / name).is_file()), None)
    if taskfile_path is None:
        return [], []

    try:
        content = taskfile_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"Failed to read {taskfile_path} in repo {root}: {exc}"]

    parsed = _parse_taskfile_yaml(content)
    tasks: list[Task] = []
    ws_name = prefix.rstrip(":") or None
    for name, desc in parsed.items():
        tasks.append(
            Task(
                name=f"{prefix}{name}",
                command=f"task {name}",
                source=taskfile_path.name,
                runner="task",
                description=desc,
                cwd=directory,
                workspace=ws_name,
            )
        )
    return tasks, []


def _discover_mise(directory: Path, root: Path, prefix: str = "") -> tuple[list[Task], list[str]]:
    """Discover tasks from mise.toml / .mise.toml / .config/mise.toml."""
    mise_path = next((directory / name for name in _MISE_FILES if (directory / name).is_file()), None)
    if mise_path is None:
        return [], []

    try:
        data = tomllib.loads(mise_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [f"Failed to parse {mise_path} in repo {root}: {exc}"]

    if not isinstance(data, dict):
        return [], []

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, dict):
        return [], []

    tasks: list[Task] = []
    ws_name = prefix.rstrip(":") or None
    for name, entry in raw_tasks.items():
        if not isinstance(name, str) or name.startswith("_"):
            continue
        desc = ""
        if isinstance(entry, str):
            desc = entry.strip()
        elif isinstance(entry, dict):
            if entry.get("hide") is True:
                continue
            if isinstance(entry.get("description"), str) and entry["description"].strip():
                desc = entry["description"].strip()
            elif isinstance(entry.get("run"), str):
                desc = entry["run"].strip()
            elif isinstance(entry.get("run"), list):
                desc = " && ".join(str(x) for x in entry["run"])
        else:
            continue

        tasks.append(
            Task(
                name=f"{prefix}{name}",
                command=f"mise run {name}",
                source=mise_path.name if mise_path.parent == directory else ".config/mise.toml",
                runner="mise",
                description=desc,
                cwd=directory,
                workspace=ws_name,
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
    debug_events: list[str] | None = None,
) -> list[str]:
    """Run all manifest scanners in priority order on a single directory."""
    discovered_workspaces: list[str] = []

    def _log(msg: str) -> None:
        if debug_events is not None:
            debug_events.append(msg)

    pkg_tasks, pkg_workspaces, pkg_warnings = _discover_package_json(directory, root, prefix=prefix)
    warnings.extend(pkg_warnings)
    discovered_workspaces.extend(pkg_workspaces)
    if pkg_tasks:
        _log(f"discovery: {directory / 'package.json'} -> {len(pkg_tasks)} task(s) via {pkg_tasks[0].runner}")
    for task in pkg_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}node")

    py_tasks, py_warnings = _discover_pyproject(directory, prefix=prefix)
    warnings.extend(py_warnings)
    if py_tasks:
        _log(f"discovery: {directory / 'pyproject.toml'} -> {len(py_tasks)} task(s)")
    for task in py_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}py")

    cargo_tasks, cargo_members, cargo_warnings = _discover_cargo(directory, root, prefix=prefix)
    warnings.extend(cargo_warnings)
    discovered_workspaces.extend(cargo_members)
    if cargo_tasks:
        _log(f"discovery: {directory / 'Cargo.toml'} -> {len(cargo_tasks)} task(s)")
    for task in cargo_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}cargo")

    go_tasks, go_modules, go_warnings = _discover_go(directory, root, prefix=prefix)
    warnings.extend(go_warnings)
    discovered_workspaces.extend(go_modules)
    if go_tasks:
        _log(f"discovery: {directory} (Go) -> {len(go_tasks)} task(s)")
    for task in go_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}go")

    mvn_tasks, mvn_modules, mvn_warnings = _discover_maven(directory, root, prefix=prefix)
    warnings.extend(mvn_warnings)
    discovered_workspaces.extend(mvn_modules)
    if mvn_tasks:
        _log(f"discovery: {directory / 'pom.xml'} -> {len(mvn_tasks)} task(s)")
    for task in mvn_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}mvn")

    gradle_tasks, gradle_subs, gradle_warnings = _discover_gradle(directory, root, prefix=prefix)
    warnings.extend(gradle_warnings)
    discovered_workspaces.extend(gradle_subs)
    if gradle_tasks:
        _log(f"discovery: {directory} (Gradle) -> {len(gradle_tasks)} task(s)")
    for task in gradle_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}gradle")

    taskfile_tasks, taskfile_warnings = _discover_taskfile(directory, root, prefix=prefix)
    warnings.extend(taskfile_warnings)
    if taskfile_tasks:
        _log(f"discovery: {directory} ({taskfile_tasks[0].source}) -> {len(taskfile_tasks)} task(s)")
    for task in taskfile_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}task")

    mise_tasks, mise_warnings = _discover_mise(directory, root, prefix=prefix)
    warnings.extend(mise_warnings)
    if mise_tasks:
        _log(f"discovery: {directory} ({mise_tasks[0].source}) -> {len(mise_tasks)} task(s)")
    for task in mise_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}mise")

    just_tasks, just_warnings = _discover_justfile(directory, prefix=prefix)
    warnings.extend(just_warnings)
    if just_tasks:
        _log(f"discovery: {directory} ({just_tasks[0].source}) -> {len(just_tasks)} task(s)")
    for task in just_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}just")

    make_tasks, make_warnings = _discover_makefile(directory, prefix=prefix)
    warnings.extend(make_warnings)
    if make_tasks:
        _log(f"discovery: {directory} ({make_tasks[0].source}) -> {len(make_tasks)} task(s)")
    for task in make_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}make")

    compose_tasks, compose_warnings = _discover_compose(directory, prefix=prefix)
    warnings.extend(compose_warnings)
    if compose_tasks:
        _log(f"discovery: {directory} ({compose_tasks[0].source}) -> {len(compose_tasks)} task(s)")
    for task in compose_tasks:
        _register_task(tasks, task, qualifier=f"{prefix}compose")

    return discovered_workspaces


def _load_devup_toml(root: Path, config: WorkspaceConfig) -> list[str]:
    """Load explicit devup.toml configuration, returning any configured workspace globs."""
    toml_path = root / "devup.toml"
    if not toml_path.is_file():
        return []

    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        config.warnings.append(
            f"Failed to parse {toml_path} in repo {root}: {exc}. Run `devup validate` to inspect configuration errors."
        )
        return []

    if not isinstance(data, dict):
        return []

    if isinstance(data.get("fail_fast"), bool):
        config.fail_fast = data["fail_fast"]

    if isinstance(data.get("auto_env"), bool):
        config.auto_env = data["auto_env"]

    if isinstance(data.get("strict_ports"), bool):
        config.strict_ports = data["strict_ports"]

    if isinstance(data.get("shutdown_timeout"), (int, float)):
        config.shutdown_timeout = max(0.5, float(data["shutdown_timeout"]))

    if isinstance(data.get("env_file"), str):
        config.env_files = [data["env_file"]]
    elif isinstance(data.get("env_file"), list):
        config.env_files = [str(f) for f in data["env_file"] if isinstance(f, str)]

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
                ready_url: str | None = None
                if isinstance(entry.get("ready_url"), str):
                    try:
                        ready_url = validate_ready_url(entry["ready_url"])
                    except ValueError as exc:
                        config.warnings.append(f"Task '{task_name}': {exc}")

                ready_tcp: str | None = None
                raw_tcp = entry.get("ready_tcp") if "ready_tcp" in entry else entry.get("ready_port")
                if isinstance(raw_tcp, (str, int)) and not isinstance(raw_tcp, bool):
                    try:
                        ready_tcp = normalize_tcp_target(raw_tcp)
                    except ValueError as exc:
                        config.warnings.append(f"Task '{task_name}': {exc}")

                ready_timeout = 30.0
                if isinstance(entry.get("ready_timeout"), (int, float)) and not isinstance(
                    entry.get("ready_timeout"), bool
                ):
                    ready_timeout = max(0.5, float(entry["ready_timeout"]))

                desc = entry.get("description") if isinstance(entry.get("description"), str) else ""
                task_env_files: list[str] = []
                if isinstance(entry.get("env_file"), str):
                    task_env_files = [entry["env_file"]]
                elif isinstance(entry.get("env_file"), list):
                    task_env_files = [str(f) for f in entry["env_file"] if isinstance(f, str)]

                task_ports: list[int] = []
                if isinstance(entry.get("port"), int):
                    task_ports = [entry["port"]]
                elif isinstance(entry.get("ports"), list):
                    task_ports = [p for p in entry["ports"] if isinstance(p, int)]

                watch_enabled = False
                watch_patterns: list[str] = []
                raw_watch = entry.get("watch")
                if isinstance(raw_watch, bool):
                    watch_enabled = raw_watch
                elif isinstance(raw_watch, str):
                    watch_enabled = True
                    watch_patterns = [raw_watch]
                elif isinstance(raw_watch, list):
                    watch_patterns = [str(w) for w in raw_watch if isinstance(w, str)]
                    watch_enabled = bool(watch_patterns)

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
                        ready_url=ready_url,
                        ready_tcp=ready_tcp,
                        ready_timeout=ready_timeout,
                        env_files=task_env_files,
                        ports=task_ports,
                        watch=watch_patterns,
                        watch_enabled=watch_enabled,
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
    config.debug_events.append(f"discovery: scanning repository root {resolved_root}")

    # First discover manifests at root, then apply devup.toml overrides on top.
    pkg_workspaces = _discover_directory(
        resolved_root,
        resolved_root,
        config.tasks,
        config.warnings,
        debug_events=config.debug_events,
    )
    toml_workspaces = _load_devup_toml(resolved_root, config)

    if scan_workspaces:
        workspace_globs = toml_workspaces or pkg_workspaces
        for ws_dir in _resolve_workspace_dirs(resolved_root, workspace_globs):
            ws_name = ws_dir.name
            config.debug_events.append(f"discovery: scanning workspace '{ws_name}' at {ws_dir}")
            _discover_directory(
                ws_dir,
                resolved_root,
                config.tasks,
                config.warnings,
                prefix=f"{ws_name}:",
                debug_events=config.debug_events,
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
            all_names = sorted(list(config.tasks.keys()) + list(config.groups.keys()))
            suggestions = difflib.get_close_matches(name, all_names, n=3, cutoff=0.45)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            available = ", ".join(all_names) if all_names else "none"
            raise KeyError(
                f"Unknown task or group '{name}'.{hint} "
                f"Available: {available}. Run `devup list` to inspect discovered targets."
            )
        if name not in seen:
            seen.add(name)
            expanded.append(name)

    for target in targets:
        expand_one(target, set())

    return expanded
