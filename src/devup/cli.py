"""Command-line interface for devup."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence, TextIO

from devup import __version__
from devup.colors import BOLD, CYAN, DIM, GREEN, RED, YELLOW, enable_windows_ansi, style, supports_color
from devup.completions import SUPPORTED_SHELLS, generate_completion
from devup.diagnostics import run_doctor, validate_workspace
from devup.discovery import discover_workspace, resolve_targets
from devup.models import TaskResult, WorkspaceConfig
from devup.picker import PickChoice, pick_targets
from devup.runner import RunOptions, run_tasks
from devup.updates import check_for_update

_SUBCOMMANDS = {"list", "ls", "run", "pick", "init", "validate", "doctor", "completion"}

_INIT_TEMPLATES: dict[str, str] = {
    "fullstack": """default = ["dev"]
fail_fast = true
strict_ports = false
auto_env = true
shutdown_timeout = 5.0

[groups]
dev = ["api", "web"]

[tasks.api]
command = "uvicorn app.main:app --reload --port 8000"
cwd = "backend"
port = 8000
ready_url = "http://127.0.0.1:8000/health"
watch = ["app/**/*.py"]

[tasks.web]
command = "npm run dev"
cwd = "frontend"
port = 5173
depends_on = ["api"]
""",
    "python-node": """default = ["dev"]
fail_fast = true
auto_env = true

[groups]
dev = ["api", "web"]

[tasks.api]
command = "uvicorn main:app --reload --port 8000"
cwd = "backend"
port = 8000
ready_url = "http://127.0.0.1:8000/health"

[tasks.web]
command = "npm run dev"
cwd = "frontend"
port = 5173
depends_on = ["api"]
""",
    "java-microservices": """default = ["dev"]
fail_fast = true
strict_ports = true

[groups]
dev = ["infra", "api"]

[tasks.infra]
command = "docker compose up postgres"
port = 5432
ready_tcp = "127.0.0.1:5432"
ready_timeout = 30

[tasks.api]
command = "mvn spring-boot:run"
cwd = "service"
port = 8080
depends_on = ["infra"]
ready_url = "http://127.0.0.1:8080/actuator/health"
ready_timeout = 45
""",
    "docker-compose": """default = ["stack"]
fail_fast = true

[groups]
stack = ["db", "cache", "app"]

[tasks.db]
command = "docker compose up postgres"
port = 5432
ready_tcp = "127.0.0.1:5432"

[tasks.cache]
command = "docker compose up redis"
port = 6379
ready_tcp = "127.0.0.1:6379"

[tasks.app]
command = "docker compose up app"
port = 8000
depends_on = ["db", "cache"]
ready_url = "http://127.0.0.1:8000/health"
""",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devup",
        description=(
            "Discover scripts across package.json, pyproject.toml, pom.xml, build.gradle, "
            "Makefile, Justfile, Docker Compose, and devup.toml, and run services in parallel."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        type=Path,
        default=Path.cwd(),
        help="Project root directory to scan (default: current directory).",
    )
    parser.add_argument(
        "--no-workspaces",
        action="store_true",
        help="Disable automatic workspace/monorepo subdirectory scanning.",
    )
    parser.add_argument(
        "-t",
        "--timestamps",
        action="store_true",
        help="Prepend timestamps to each log line.",
    )
    parser.add_argument(
        "--no-prefix",
        action="store_true",
        help="Do not prepend [service] prefixes to log lines.",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Keep other services running if one exits with a non-zero status.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the resolved execution plan without starting processes.",
    )
    parser.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help="Watch files under each task's working directory and auto-restart on changes.",
    )
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="Load additional .env file(s) before starting tasks (can be repeated).",
    )
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Disable automatic loading of .env in the project root.",
    )
    parser.add_argument(
        "--no-port-check",
        action="store_true",
        help="Skip pre-flight port availability checks.",
    )
    parser.add_argument(
        "--strict-ports",
        action="store_true",
        help="Abort startup if any declared or inferred port is already in use.",
    )
    color_group = parser.add_mutually_exclusive_group()
    color_group.add_argument("--color", action="store_true", help="Force ANSI color output.")
    color_group.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON (used with `list`, `validate`, `doctor`).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing devup.toml (used with `init`).",
    )
    parser.add_argument(
        "--template",
        choices=("auto", *tuple(_INIT_TEMPLATES.keys())),
        default="auto",
        help="Preset template for `devup init`.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for configuration options during `devup init`.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive prompts during `devup init`.",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="Query PyPI during `devup doctor` to check for newer releases.",
    )
    parser.add_argument(
        "--check-updates",
        action="store_true",
        help="Check PyPI (24h cached) for a newer release of devup-cli.",
    )
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="Disable update availability notifications.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help=(
            "Subcommand (list, run, pick, init, validate, doctor, completion) "
            "or task/group names to run in parallel."
        ),
    )
    return parser


def _resolve_color_flag(args: argparse.Namespace) -> bool | None:
    if args.color:
        return True
    if args.no_color:
        return False
    return None


def _print_list(config: WorkspaceConfig, out: TextIO, use_color: bool, as_json: bool) -> int:
    if as_json:
        payload = {
            "root": str(config.root),
            "default": config.default_targets,
            "groups": config.groups,
            "tasks": {
                name: {
                    "command": task.command,
                    "source": task.source,
                    "runner": task.runner,
                    "description": task.description,
                    "cwd": str(task.cwd) if task.cwd else str(config.root),
                    "depends_on": task.depends_on,
                    "ready_pattern": task.ready_pattern,
                    "ready_url": task.ready_url,
                    "ready_tcp": task.ready_tcp,
                }
                for name, task in config.tasks.items()
            },
            "warnings": config.warnings,
        }
        out.write(json.dumps(payload, indent=2) + "\n")
        return 0

    for warning in config.warnings:
        out.write(style(f"warning: {warning}\n", YELLOW, enabled=use_color))

    if not config.tasks and not config.groups:
        out.write(
            f"No tasks discovered in {config.root}.\n"
            "Tip: run `devup init` to create a starter devup.toml.\n"
        )
        return 0

    if config.groups:
        out.write(style("Groups:\n", BOLD, CYAN, enabled=use_color))
        group_pad = max(len(g) for g in config.groups)
        for group_name, members in config.groups.items():
            default_mark = " (default)" if config.default_targets == [group_name] else ""
            out.write(
                f"  {style(f'{group_name:<{group_pad}}', BOLD, GREEN, enabled=use_color)}"
                f"  -> {', '.join(members)}{style(default_mark, DIM, enabled=use_color)}\n"
            )
        out.write("\n")

    if config.tasks:
        out.write(style(f"Tasks in {config.root}:\n", BOLD, CYAN, enabled=use_color))
        name_w = min(28, max(len(n) for n in config.tasks))
        src_w = min(16, max(len(t.source) for t in config.tasks.values()))
        cmd_w = min(38, max(len(t.command) for t in config.tasks.values()))
        for name, task in config.tasks.items():
            desc = f"  {style(task.description, DIM, enabled=use_color)}" if task.description else ""
            cmd_display = task.command if len(task.command) <= 38 else task.command[:35] + "..."
            out.write(
                f"  {style(f'{name:<{name_w}}', BOLD, enabled=use_color)}  "
                f"{style(f'{task.source:<{src_w}}', CYAN, enabled=use_color)}  "
                f"{cmd_display:<{cmd_w}}{desc}\n"
            )

    out.write(
        "\n"
        + style("Run multiple services: ", DIM, enabled=use_color)
        + "devup run <task1> <task2>\n"
    )
    return 0


def _interactive_pick(
    config: WorkspaceConfig,
    out: TextIO,
    use_color: bool,
) -> list[str]:
    """Prompt the user to choose one or more tasks/groups."""
    choices: list[PickChoice] = []
    for gname, members in config.groups.items():
        choices.append(PickChoice(name=gname, detail=f"group: {', '.join(members)}"))
    for tname, task in config.tasks.items():
        choices.append(PickChoice(name=tname, detail=f"{task.command} ({task.source})"))
    return pick_targets(choices, out=out, use_color=use_color)


def _prompt_line(out: TextIO, in_stream: TextIO, prompt: str, default: str = "") -> str:
    out.write(prompt)
    out.flush()
    raw = in_stream.readline()
    stripped = raw.strip()
    return stripped if stripped else default


def _init_devup_toml(
    config: WorkspaceConfig,
    force: bool,
    out: TextIO,
    use_color: bool,
    template: str = "auto",
    interactive: bool = False,
    yes: bool = False,
    stdin: TextIO | None = None,
) -> int:
    target_path = config.root / "devup.toml"
    if target_path.exists() and not force:
        out.write(
            style(
                f"{target_path} already exists. Pass --force to overwrite.\n",
                RED,
                enabled=use_color,
            )
        )
        return 1

    in_stream = stdin or sys.stdin
    try:
        is_tty = bool(in_stream.isatty() and out.isatty())
    except Exception:
        is_tty = False

    should_prompt = interactive or (is_tty and not yes and template == "auto")
    chosen_template = template
    sample_tasks = list(config.tasks.keys())[:2]
    default_group = "stack" if "dev" in config.tasks else "dev"
    strict_ports = False
    fail_fast = True

    if should_prompt:
        avail_templates = ", ".join(("auto", *tuple(_INIT_TEMPLATES.keys())))
        chosen_template = _prompt_line(
            out,
            in_stream,
            f"Template ({avail_templates}) [{template}]: ",
            default=template,
        )
        if chosen_template not in ("auto", *_INIT_TEMPLATES):
            out.write(
                style(
                    f"Unknown template '{chosen_template}'. Falling back to 'auto'.\n",
                    YELLOW,
                    enabled=use_color,
                )
            )
            chosen_template = "auto"

    if chosen_template in _INIT_TEMPLATES:
        target_path.write_text(_INIT_TEMPLATES[chosen_template], encoding="utf-8")
        out.write(
            style(
                f"Created {target_path} from '{chosen_template}' template.\n",
                GREEN,
                enabled=use_color,
            )
        )
        return 0

    if should_prompt:
        default_group = _prompt_line(
            out,
            in_stream,
            f"Primary group name [{default_group}]: ",
            default=default_group,
        )
        strict_ans = _prompt_line(
            out,
            in_stream,
            "Abort startup if any port is busy (strict_ports)? [y/N]: ",
            default="n",
        ).lower()
        strict_ports = strict_ans in ("y", "yes")
        ff_ans = _prompt_line(
            out,
            in_stream,
            "Stop sibling services on crash (fail_fast)? [Y/n]: ",
            default="y",
        ).lower()
        fail_fast = ff_ans not in ("n", "no")

    group_line = (
        f"{default_group} = {json.dumps(sample_tasks)}"
        if sample_tasks
        else f'{default_group} = ["api", "web"]'
    )

    lines = [
        "# devup.toml — project-specific service groups, dependencies, and overrides",
        f"# default = {json.dumps([default_group])}" if sample_tasks else '# default = ["dev"]',
        f"fail_fast = {'true' if fail_fast else 'false'}",
        f"strict_ports = {'true' if strict_ports else 'false'}",
        "auto_env = true",
        "shutdown_timeout = 5.0",
        "",
        "[groups]",
        group_line,
        "",
        "# Example custom task with HTTP readiness detection:",
        "# [tasks.api]",
        '# command = "uvicorn app.main:app --reload --port 8000"',
        '# cwd = "backend"',
        "# port = 8000",
        '# ready_url = "http://127.0.0.1:8000/health"',
        "",
    ]
    target_path.write_text("\n".join(lines), encoding="utf-8")
    out.write(style(f"Created {target_path}\n", GREEN, enabled=use_color))
    return 0


def _print_validation(
    root: Path,
    scan_workspaces: bool,
    out: TextIO,
    use_color: bool,
    as_json: bool,
) -> int:
    report = validate_workspace(root, scan_workspaces=scan_workspaces)
    if as_json:
        out.write(json.dumps(report.to_dict(redact_paths=False), indent=2) + "\n")
        return 0 if report.valid else 1

    for warn in report.warnings:
        out.write(style(f"warning: {warn}\n", YELLOW, enabled=use_color))
    for err in report.errors:
        out.write(style(f"error: {err}\n", RED, enabled=use_color))

    if report.valid:
        out.write(
            style(
                f"Valid configuration ({report.task_count} task(s), {report.group_count} group(s)).\n",
                GREEN,
                enabled=use_color,
            )
        )
        return 0
    return 1


def _print_doctor(
    root: Path,
    scan_workspaces: bool,
    online: bool,
    out: TextIO,
    use_color: bool,
    as_json: bool,
) -> int:
    report = run_doctor(root, online=online, scan_workspaces=scan_workspaces)
    if as_json:
        out.write(json.dumps(report, indent=2) + "\n")
        return 0 if report["validation"]["valid"] else 1

    out.write(style("devup doctor\n", BOLD, CYAN, enabled=use_color))
    out.write(f"  devup version : {report['devup_version']}\n")
    out.write(f"  python        : {report['python_version']} ({report['os']})\n")
    out.write(f"  project root  : {report['cwd']}\n")
    out.write(f"  devup.toml    : {'present' if report['devup_toml_present'] else 'absent'}\n")
    out.write(f"  .env          : {'present' if report['dotenv_present'] else 'absent'}\n")
    out.write(f"  sources       : {', '.join(report['sources']) or 'none'}\n")

    available = [name for name, info in report["runners"].items() if info["available"]]
    missing = [name for name, info in report["runners"].items() if not info["available"]]
    out.write(f"  runners found : {', '.join(available) or 'none'}\n")
    out.write(style(f"  not in PATH   : {', '.join(missing) or 'none'}\n", DIM, enabled=use_color))

    val = report["validation"]
    if val["errors"]:
        for err in val["errors"]:
            out.write(style(f"  error         : {err}\n", RED, enabled=use_color))
    elif val["warnings"]:
        for warn in val["warnings"]:
            out.write(style(f"  warning       : {warn}\n", YELLOW, enabled=use_color))
    else:
        out.write(style("  validation    : OK\n", GREEN, enabled=use_color))

    upd = report.get("update_check")
    if isinstance(upd, dict):
        if upd.get("update_available"):
            out.write(
                style(
                    f"  update        : devup-cli {upd['latest']} available (current {upd['current']})\n",
                    YELLOW,
                    enabled=use_color,
                )
            )
        else:
            out.write(style(f"  update        : up to date ({upd['current']})\n", GREEN, enabled=use_color))

    return 0 if val["valid"] else 1


def _maybe_notify_update(
    args: argparse.Namespace,
    out: TextIO,
    use_color: bool,
    command: str | None,
) -> None:
    if args.no_update_check or args.json or command == "completion":
        return
    env_opt_in = os.environ.get("DEVUP_CHECK_UPDATES") in ("1", "true", "yes")
    if not (args.check_updates or env_opt_in):
        return
    status = check_for_update(__version__, force=bool(args.check_updates))
    if status and status.update_available:
        out.write(
            style(
                f"Note: devup-cli {status.latest} is available (you have {status.current}). "
                "Upgrade with `pipx upgrade devup-cli` or `pip install -U devup-cli`.\n",
                DIM,
                enabled=use_color,
            )
        )


def _exit_code_from_results(results: list[TaskResult]) -> int:
    if any(r.status in ("failed", "error") for r in results):
        for r in results:
            if r.status in ("failed", "error") and r.exit_code != 0:
                return r.exit_code
        return 1
    if any(r.status == "stopped" for r in results):
        return 130
    return 0


def main(argv: Sequence[str] | None = None, stream: TextIO | None = None) -> int:
    """CLI entrypoint for devup."""
    enable_windows_ansi()
    out = stream or sys.stdout
    parser = _build_parser()
    args = parser.parse_intermixed_args(list(argv) if argv is not None else None)
    color_pref = _resolve_color_flag(args)
    use_color = supports_color(out, force=color_pref)

    root = args.cwd
    if not root.exists():
        out.write(style(f"Error: directory does not exist: {root}\n", RED, enabled=use_color))
        return 2

    config = discover_workspace(root, scan_workspaces=not args.no_workspaces)
    positionals: list[str] = list(args.targets)

    command = positionals[0] if positionals and positionals[0] in _SUBCOMMANDS else None
    rest = positionals[1:] if command else positionals

    if command == "completion":
        if len(rest) != 1:
            out.write(
                style(
                    f"Usage: devup completion <{'|'.join(SUPPORTED_SHELLS)}>\n",
                    RED,
                    enabled=use_color,
                )
            )
            return 2
        try:
            out.write(generate_completion(rest[0]))
            return 0
        except ValueError as exc:
            out.write(style(f"Error: {exc}\n", RED, enabled=use_color))
            return 2

    if command == "validate":
        return _print_validation(
            root,
            scan_workspaces=not args.no_workspaces,
            out=out,
            use_color=use_color,
            as_json=args.json,
        )

    if command == "doctor":
        return _print_doctor(
            root,
            scan_workspaces=not args.no_workspaces,
            online=bool(args.online or args.check_updates),
            out=out,
            use_color=use_color,
            as_json=args.json,
        )

    _maybe_notify_update(args, out, use_color, command)

    if command in ("list", "ls"):
        return _print_list(config, out, use_color, as_json=args.json)

    if command == "init":
        return _init_devup_toml(
            config,
            force=args.force,
            out=out,
            use_color=use_color,
            template=args.template,
            interactive=args.interactive,
            yes=args.yes,
        )

    if command == "pick":
        try:
            picked = _interactive_pick(config, out, use_color)
        except ValueError as exc:
            out.write(style(f"Error: {exc}\n", RED, enabled=use_color))
            return 2
        if not picked:
            return 0
        rest = picked
        command = "run"

    if command is None and not rest:
        if config.default_targets:
            rest = list(config.default_targets)
        elif sys.stdin.isatty() and (config.tasks or config.groups):
            try:
                rest = _interactive_pick(config, out, use_color)
            except ValueError as exc:
                out.write(style(f"Error: {exc}\n", RED, enabled=use_color))
                return 2
            if not rest:
                return 0
        else:
            return _print_list(config, out, use_color, as_json=args.json)

    if command == "run" and not rest:
        out.write(style("Error: `devup run` requires at least one task or group name.\n", RED, enabled=use_color))
        return 2

    try:
        resolved = resolve_targets(config, rest)
    except (KeyError, ValueError) as exc:
        out.write(style(f"Error: {exc}\n", RED, enabled=use_color))
        return 2

    run_opts = RunOptions(
        fail_fast=False if args.keep_going else None,
        timestamps=args.timestamps,
        prefix=not args.no_prefix,
        color=color_pref,
        dry_run=args.dry_run,
        dotenv=not args.no_dotenv,
        extra_env_files=list(args.env_file),
        port_check=not args.no_port_check,
        strict_ports=True if args.strict_ports else None,
        watch=args.watch,
    )
    try:
        results = run_tasks(config, resolved, options=run_opts, stream=out)
    except (KeyError, ValueError) as exc:
        out.write(style(f"Error: {exc}\n", RED, enabled=use_color))
        return 2

    return _exit_code_from_results(results)


if __name__ == "__main__":
    raise SystemExit(main())
