"""Data models for discovered tasks, groups, and run results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Task:
    """A runnable command discovered from project config or manifest files."""

    name: str
    command: str
    source: str
    runner: str = "shell"
    description: str = ""
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    ready_pattern: str | None = None
    aliases: list[str] = field(default_factory=list)
    workspace: str | None = None
    env_files: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    watch_enabled: bool = False


@dataclass(slots=True)
class WorkspaceConfig:
    """Aggregated tasks, groups, and runner settings for a project root."""

    root: Path
    tasks: dict[str, Task] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)
    default_targets: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_files: list[str] = field(default_factory=list)
    auto_env: bool = True
    strict_ports: bool = False
    fail_fast: bool = True
    shutdown_timeout: float = 5.0
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TaskResult:
    """Outcome of executing a single task."""

    name: str
    exit_code: int
    duration_seconds: float
    status: str  # "ok" | "failed" | "stopped" | "skipped" | "error"
    error: str | None = None
