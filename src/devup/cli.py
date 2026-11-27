"""Command-line interface for devup."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence, TextIO

from devup import __version__
from devup.colors import BOLD, CYAN, DIM, GREEN, RED, YELLOW, enable_windows_ansi, style, supports_color
from devup.discovery import discover_workspace, resolve_targets
from devup.models import TaskResult, WorkspaceConfig
from devup.picker import PickChoice, pick_targets
from devup.runner import RunOptions, run_tasks

_SUBCOMMANDS = {"list", "ls", "run", "pick", "init"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devup",
        description=(
            "Discover scripts across package.json, pyproject.toml, Makefile, Justfile, "
            "Docker Compose, and devup.toml, and run multiple services in parallel."
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
        help="Output discovered tasks and groups as JSON (used with `list`).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing devup.toml (used with `init`).",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Subcommand (list, run, pick, init) or task/group names to run in parallel.",
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


def _init_devup_toml(config: WorkspaceConfig, force: bool, out: TextIO, use_color: bool) -> int:
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

    sample_tasks = list(config.tasks.keys())[:2]
    group_name = "stack" if "dev" in config.tasks else "dev"
    group_line = (
        f"{group_name} = {json.dumps(sample_tasks)}"
        if sample_tasks
        else f'{group_name} = ["api", "web"]'
    )

    lines = [
        "# devup.toml — project-specific service groups, dependencies, and overrides",
        f"# default = {json.dumps([group_name])}" if sample_tasks else '# default = ["dev"]',
        "fail_fast = true",
        "shutdown_timeout = 5.0",
        "",
        "[groups]",
        group_line,
        "",
        "# Example custom task with readiness detection:",
        "# [tasks.api]",
        '# command = "uvicorn app.main:app --reload"',
        '# cwd = "backend"',
        '# description = "Start API server"',
        '# ready_pattern = "Uvicorn running on"',
        "",
    ]
    target_path.write_text("\n".join(lines), encoding="utf-8")
    out.write(style(f"Created {target_path}\n", GREEN, enabled=use_color))
    return 0


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

    if command in ("list", "ls"):
        return _print_list(config, out, use_color, as_json=args.json)

    if command == "init":
        return _init_devup_toml(config, force=args.force, out=out, use_color=use_color)

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
