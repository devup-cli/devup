"""Parallel multi-service process runner with prefixed logs and graceful shutdown."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from devup.colors import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    YELLOW,
    assign_service_colors,
    supports_color,
    style,
)
from devup.dotenv import load_env_files
from devup.models import Task, TaskResult, WorkspaceConfig
from devup.ports import check_task_ports
from devup.proc_tree import ManagedProcess, spawn_managed_process, terminate_managed_process
from devup.readiness import probe_http, probe_tcp
from devup.watcher import diff_snapshots, snapshot_tree

_SHELL_BUILTINS = frozenset({"echo", "cd", "set", "export", "exit", "true", "false", ":"})


@dataclass(slots=True)
class RunOptions:
    """Runtime options controlling runner behavior and log formatting."""

    fail_fast: bool | None = None
    shutdown_timeout: float | None = None
    timestamps: bool = False
    prefix: bool = True
    color: bool | None = None
    dry_run: bool = False
    dotenv: bool = True
    extra_env_files: list[str] = field(default_factory=list)
    port_check: bool = True
    strict_ports: bool | None = None
    watch: bool = False
    watch_interval: float = 0.4
    only: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    buffer_mode: str = "line"  # "line" | "group"
    save_logs: Path | None = None
    verbose: bool = False


class _OutputWriter:
    """Thread-safe log formatter for interleaved or grouped subprocess output."""

    def __init__(
        self,
        stream: TextIO,
        service_colors: dict[str, str],
        pad_width: int,
        use_color: bool,
        timestamps: bool,
        show_prefix: bool,
        only_services: set[str] | None = None,
        exclude_services: set[str] | None = None,
        buffer_mode: str = "line",
        log_file: TextIO | None = None,
        verbose: bool = False,
    ) -> None:
        self._stream = stream
        self._colors = service_colors
        self._pad_width = pad_width
        self._use_color = use_color
        self._timestamps = timestamps
        self._show_prefix = show_prefix
        self._only = only_services
        self._exclude = exclude_services or set()
        self._buffer_mode = buffer_mode
        self._buffered: dict[str, list[tuple[str, str]]] = {}
        self._log_file = log_file
        self._verbose = verbose
        self._lock = threading.Lock()

    def _service_visible(self, service: str) -> bool:
        if self._only is not None and service not in self._only:
            return False
        if service in self._exclude:
            return False
        return True

    def _format_service_line(self, service: str, line: str, ts: str | None = None) -> str:
        parts: list[str] = []
        if self._timestamps:
            stamp = ts or datetime.now().strftime("%H:%M:%S.%f")[:-3]
            parts.append(style(stamp, DIM, enabled=self._use_color))
        if self._show_prefix:
            tag = f"[{service:<{self._pad_width}}]"
            color = self._colors.get(service, "")
            parts.append(style(tag, BOLD, color, enabled=self._use_color and bool(color)))
        prefix = " ".join(parts)
        return f"{prefix} {line}" if prefix else line

    def _write_log_file_line(self, tag: str, line: str, ts: str) -> None:
        if self._log_file is not None:
            self._log_file.write(f"{ts} {tag} {line}\n")
            self._log_file.flush()

    def write_line(self, service: str, line: str, force_visible: bool = False) -> None:
        """Emit a single line prefixed with optional timestamp and colored service tag."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if self._buffer_mode == "group" and not force_visible:
            with self._lock:
                self._write_log_file_line(f"[{service:<{self._pad_width}}]", line, ts)
                if self._service_visible(service):
                    self._buffered.setdefault(service, []).append((ts, line))
            return

        formatted = self._format_service_line(service, line, ts=ts)
        with self._lock:
            self._write_log_file_line(f"[{service:<{self._pad_width}}]", line, ts)
            if force_visible or self._service_visible(service):
                self._stream.write(formatted + "\n")
                self._stream.flush()

    def flush_service(self, service: str) -> None:
        """Flush buffered lines for service atomically when using group buffering."""
        with self._lock:
            entries = self._buffered.pop(service, [])
            if not entries:
                return
            for ts, line in entries:
                formatted = self._format_service_line(service, line, ts=ts)
                self._stream.write(formatted + "\n")
            self._stream.flush()

    def write_system(self, message: str, color_code: str = CYAN) -> None:
        """Emit a runner lifecycle status message."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        ts_prefix = style(ts, DIM, enabled=self._use_color) + " " if self._timestamps else ""
        tag = style("[devup]", BOLD, color_code, enabled=self._use_color)
        with self._lock:
            self._write_log_file_line("[devup]", message, ts)
            self._stream.write(f"{ts_prefix}{tag} {message}\n")
            self._stream.flush()

    def write_debug(self, message: str) -> None:
        """Emit a diagnostic debug line when verbose mode is active."""
        if not self._verbose:
            return
        self.write_system(f"[debug] {message}", DIM)


def _expand_dependencies(config: WorkspaceConfig, initial_targets: list[str]) -> list[str]:
    """Return topologically ordered task names including transitive depends_on."""
    ordered: list[str] = []
    state: dict[str, int] = {}  # 0/absent=unvisited, 1=visiting, 2=done

    def visit(name: str, trail: list[str]) -> None:
        current = state.get(name, 0)
        if current == 2:
            return
        if current == 1:
            cycle = " -> ".join([*trail, name])
            raise ValueError(f"Circular task dependency detected: {cycle}")
        if name not in config.tasks:
            raise KeyError(f"Task '{name}' referenced in depends_on was not found.")

        state[name] = 1
        for dep in config.tasks[name].depends_on:
            visit(dep, [*trail, name])
        state[name] = 2
        ordered.append(name)

    for target in initial_targets:
        visit(target, [])

    return ordered


def _load_workspace_dotenv(
    config: WorkspaceConfig,
    opts: RunOptions,
) -> tuple[dict[str, str], list[str]]:
    """Load root .env (when auto_env is enabled) plus configured workspace env_files."""
    merged: dict[str, str] = {}
    warnings: list[str] = []

    if opts.dotenv and config.auto_env:
        default_env = config.root / ".env"
        if default_env.is_file():
            vals, warns = load_env_files([default_env], config.root, warn_missing=False)
            merged.update(vals)
            warnings.extend(warns)

    explicit_files = [*config.env_files, *opts.extra_env_files]
    if explicit_files:
        vals, warns = load_env_files(explicit_files, config.root, warn_missing=True)
        merged.update(vals)
        warnings.extend(warns)

    return merged, warnings


def _build_task_env(
    config: WorkspaceConfig,
    task: Task,
    workspace_dotenv: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Merge process environment, dotenv files, workspace env, and task-specific env."""
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    for key, val in workspace_dotenv.items():
        env.setdefault(key, val)
    env.update(config.env)

    warnings: list[str] = []
    if task.env_files:
        task_dotenv, warns = load_env_files(
            task.env_files,
            task.cwd or config.root,
            warn_missing=True,
        )
        env.update(task_dotenv)
        warnings.extend(warns)

    env.update(task.env)
    return env, warnings


def _expand_filter_names(config: WorkspaceConfig, raw_items: list[str]) -> set[str]:
    """Expand comma-separated filter names and optional group names into task names."""
    expanded: set[str] = set()
    for item in raw_items:
        for tok in item.split(","):
            name = tok.strip()
            if not name:
                continue
            if name in config.groups:
                expanded.update(config.groups[name])
            else:
                expanded.add(name)
    return expanded


def _missing_executable_hint(task: Task, root: Path) -> str | None:
    """Return an actionable hint if a task's lead executable cannot be resolved on PATH."""
    try:
        tokens = shlex.split(task.command, posix=(os.name != "nt"))
    except ValueError:
        tokens = task.command.split()
    if not tokens:
        return None
    head = tokens[0].strip("\"'")
    if not head or head.lower() in _SHELL_BUILTINS or "=" in head:
        return None
    if os.path.sep in head or "/" in head:
        target = Path(head) if Path(head).is_absolute() else (Path(task.cwd or root) / head)
        if not target.exists():
            return (
                f"Executable '{head}' not found for task '{task.name}' "
                f"(command: `{task.command}`, cwd: {task.cwd or root}, repo: {root})."
            )
        return None
    if shutil.which(head) is None:
        return (
            f"Executable '{head}' not found on PATH for task '{task.name}' "
            f"(command: `{task.command}`, repo: {root}). "
            f"Install '{head}' or run `devup doctor`."
        )
    return None


def run_tasks(
    config: WorkspaceConfig,
    targets: list[str],
    options: RunOptions | None = None,
    stream: TextIO | None = None,
    stop_signal: threading.Event | None = None,
) -> list[TaskResult]:
    """Execute one or more tasks in parallel respecting dependencies and shutdown signals."""
    opts = options or RunOptions()
    out = stream or sys.stdout
    use_color = supports_color(out, force=opts.color)
    fail_fast = config.fail_fast if opts.fail_fast is None else opts.fail_fast
    strict_ports = config.strict_ports if opts.strict_ports is None else opts.strict_ports
    shutdown_timeout = (
        config.shutdown_timeout if opts.shutdown_timeout is None else opts.shutdown_timeout
    )

    ordered_names = _expand_dependencies(config, targets)
    if not ordered_names:
        return []

    only_set = _expand_filter_names(config, opts.only) if opts.only else None
    exclude_set = _expand_filter_names(config, opts.exclude) if opts.exclude else set()

    log_handle: TextIO | None = None
    if opts.save_logs is not None and not opts.dry_run:
        opts.save_logs.parent.mkdir(parents=True, exist_ok=True)
        log_handle = opts.save_logs.open("a", encoding="utf-8")

    pad_width = min(24, max(len(name) for name in ordered_names))
    colors = assign_service_colors(ordered_names, enabled=use_color)
    writer = _OutputWriter(
        stream=out,
        service_colors=colors,
        pad_width=pad_width,
        use_color=use_color,
        timestamps=opts.timestamps,
        show_prefix=opts.prefix,
        only_services=only_set,
        exclude_services=exclude_set,
        buffer_mode=opts.buffer_mode,
        log_file=log_handle,
        verbose=opts.verbose or os.environ.get("DEVUP_DEBUG") in ("1", "true", "yes"),
    )

    for ev in config.debug_events:
        writer.write_debug(ev)
    writer.write_debug(
        f"execution plan: targets=[{', '.join(ordered_names)}], buffer={opts.buffer_mode}, fail_fast={fail_fast}"
    )

    if opts.dry_run:
        writer.write_system(f"Dry run execution plan ({len(ordered_names)} task(s)):", CYAN)
        results: list[TaskResult] = []
        for name in ordered_names:
            task = config.tasks[name]
            deps_note = f" (after: {', '.join(task.depends_on)})" if task.depends_on else ""
            cwd_note = f" @ {task.cwd or config.root}"
            writer.write_line(name, f"{task.command}{deps_note}{cwd_note}", force_visible=True)
            results.append(TaskResult(name=name, exit_code=0, duration_seconds=0.0, status="ok"))
        return results

    workspace_dotenv, dotenv_warnings = _load_workspace_dotenv(config, opts)
    if workspace_dotenv:
        writer.write_debug(f"loaded {len(workspace_dotenv)} env key(s) from root/workspace .env files: {', '.join(sorted(workspace_dotenv))}")
    for warn in dotenv_warnings:
        writer.write_system(warn, YELLOW)

    if opts.port_check:
        writer.write_debug(f"running pre-flight port check across {len(ordered_names)} task(s)")
        conflicts = check_task_ports(config.tasks, ordered_names)
        if conflicts:
            for conflict in conflicts:
                writer.write_system(
                    f"Port {'error' if strict_ports else 'warning'} ({conflict.task_name}): {conflict.reason}",
                    RED if strict_ports else YELLOW,
                )
            if strict_ports:
                conflict_by_task = {c.task_name: c for c in conflicts}
                if log_handle is not None:
                    log_handle.close()
                return [
                    TaskResult(
                        name=name,
                        exit_code=2 if name in conflict_by_task else 1,
                        duration_seconds=0.0,
                        status="error" if name in conflict_by_task else "skipped",
                        error=conflict_by_task[name].reason if name in conflict_by_task else None,
                    )
                    for name in ordered_names
                ]

    ready_events: dict[str, threading.Event] = {name: threading.Event() for name in ordered_names}
    dep_failed: dict[str, bool] = {name: False for name in ordered_names}
    ready_regexes: dict[str, re.Pattern[str] | None] = {
        name: re.compile(config.tasks[name].ready_pattern)
        if config.tasks[name].ready_pattern
        else None
        for name in ordered_names
    }

    stop_event = stop_signal or threading.Event()
    force_kill_event = threading.Event()
    interrupted_event = threading.Event()

    procs_lock = threading.Lock()
    active_procs: dict[str, ManagedProcess] = {}
    results_map: dict[str, TaskResult] = {}

    def stop_all(reason: str, force: bool = False) -> None:
        stop_event.set()
        if force:
            force_kill_event.set()
        with procs_lock:
            snapshot = list(active_procs.items())
        if snapshot:
            writer.write_system(reason, YELLOW)
        for _, managed in snapshot:
            terminate_managed_process(managed, timeout=shutdown_timeout, force_immediately=force)
        for ev in ready_events.values():
            ev.set()

    # Register SIGINT / SIGTERM handlers safely when invoked on the main thread.
    prev_sigint = None
    prev_sigterm = None
    is_main = threading.current_thread() is threading.main_thread()

    def _handle_signal(signum: int, _frame: object) -> None:
        if not interrupted_event.is_set():
            interrupted_event.set()
            stop_event.set()
            t = threading.Thread(
                target=stop_all,
                args=("Interrupt received — stopping running services...", False),
                daemon=True,
            )
            stop_threads.append(t)
            t.start()
        else:
            force_kill_event.set()
            t = threading.Thread(
                target=stop_all,
                args=("Second interrupt — force killing remaining services...", True),
                daemon=True,
            )
            stop_threads.append(t)
            t.start()

    stop_threads: list[threading.Thread] = []

    if is_main:
        prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _handle_signal)
        if hasattr(signal, "SIGTERM"):
            prev_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _handle_signal)

    writer.write_system(
        f"Starting {len(ordered_names)} task(s): {', '.join(ordered_names)}",
        CYAN,
    )

    def run_one(name: str) -> None:
        task = config.tasks[name]

        # Wait for upstream dependencies to reach readiness or finish cleanly.
        for dep in task.depends_on:
            while not ready_events[dep].wait(timeout=0.1):
                if stop_event.is_set():
                    break
            if stop_event.is_set() or dep_failed.get(dep, False):
                dep_failed[name] = True
                ready_events[name].set()
                results_map[name] = TaskResult(
                    name=name,
                    exit_code=1,
                    duration_seconds=0.0,
                    status="skipped",
                    error=f"Skipped because dependency '{dep}' did not become ready.",
                )
                writer.write_line(
                    name,
                    style(f"skipped (dependency '{dep}' failed or stopped)", YELLOW, enabled=use_color),
                )
                return

        if stop_event.is_set():
            dep_failed[name] = True
            ready_events[name].set()
            results_map[name] = TaskResult(
                name=name,
                exit_code=0,
                duration_seconds=0.0,
                status="skipped",
            )
            return

        is_watched = opts.watch or task.watch_enabled
        watch_dir = Path(task.cwd or config.root)
        watch_patterns = task.watch or None
        snap = snapshot_tree(watch_dir, watch_patterns) if is_watched else {}

        env, task_env_warnings = _build_task_env(config, task, workspace_dotenv)
        for warn in task_env_warnings:
            writer.write_line(name, style(warn, YELLOW, enabled=use_color))

        while True:
            start = time.monotonic()
            with procs_lock:
                if stop_event.is_set():
                    dep_failed[name] = True
                    ready_events[name].set()
                    results_map[name] = TaskResult(
                        name=name,
                        exit_code=0,
                        duration_seconds=0.0,
                        status="skipped",
                    )
                    return
                try:
                    managed = spawn_managed_process(
                        task.command,
                        cwd=str(task.cwd or config.root),
                        env=env,
                    )
                except OSError as exc:
                    dep_failed[name] = True
                    ready_events[name].set()
                    results_map[name] = TaskResult(
                        name=name,
                        exit_code=127,
                        duration_seconds=time.monotonic() - start,
                        status="error",
                        error=str(exc),
                    )
                    writer.write_line(name, style(f"failed to start: {exc}", RED, enabled=use_color))
                    if fail_fast and not is_watched:
                        stop_all(f"Task '{name}' failed to start — stopping other services...", force=False)
                    return
                active_procs[name] = managed

            proc = managed.proc
            pattern = ready_regexes[name]
            has_network_probe = bool(task.ready_url or task.ready_tcp)
            pattern_event = threading.Event()
            if pattern is None:
                pattern_event.set()
            probe_event = threading.Event()
            if not has_network_probe:
                probe_event.set()
            attempt_done = threading.Event()
            probe_timed_out = threading.Event()

            def maybe_mark_ready() -> None:
                if pattern_event.is_set() and probe_event.is_set() and not ready_events[name].is_set():
                    ready_events[name].set()

            def pump_output(active_proc: subprocess.Popen[str] = proc) -> None:
                if active_proc.stdout is None:
                    return
                try:
                    for raw_line in active_proc.stdout:
                        cleaned = raw_line.rstrip("\r\n")
                        writer.write_line(name, cleaned)
                        if pattern is not None and not pattern_event.is_set():
                            if pattern.search(cleaned):
                                pattern_event.set()
                                maybe_mark_ready()
                except (ValueError, OSError):
                    pass

            def run_network_probe() -> None:
                deadline = start + task.ready_timeout
                targets_desc = " & ".join(
                    p for p in (task.ready_tcp and f"tcp://{task.ready_tcp}", task.ready_url) if p
                )
                while not stop_event.is_set() and not attempt_done.is_set():
                    tcp_ok = probe_tcp(task.ready_tcp) if task.ready_tcp else True
                    http_ok = probe_http(task.ready_url) if task.ready_url else True
                    if tcp_ok and http_ok:
                        probe_event.set()
                        writer.write_line(
                            name,
                            style(f"ready ({targets_desc})", GREEN, enabled=use_color),
                        )
                        maybe_mark_ready()
                        return
                    if time.monotonic() >= deadline:
                        probe_timed_out.set()
                        dep_failed[name] = True
                        writer.write_line(
                            name,
                            style(
                                f"readiness check timed out after {task.ready_timeout:.1f}s ({targets_desc})",
                                RED,
                                enabled=use_color,
                            ),
                        )
                        ready_events[name].set()
                        if fail_fast and not is_watched and not stop_event.is_set():
                            stop_all(
                                f"Task '{name}' readiness check timed out — stopping remaining services...",
                                force=False,
                            )
                        else:
                            terminate_managed_process(managed, timeout=shutdown_timeout)
                        return
                    time.sleep(0.15)

            reader = threading.Thread(target=pump_output, name=f"devup-log-{name}", daemon=True)
            reader.start()
            probe_thread: threading.Thread | None = None
            if has_network_probe:
                probe_thread = threading.Thread(
                    target=run_network_probe,
                    name=f"devup-probe-{name}",
                    daemon=True,
                )
                probe_thread.start()

            restart_requested = False
            next_watch_poll = time.monotonic() + opts.watch_interval
            next_desc_poll = time.monotonic() + 0.15

            while proc.poll() is None:
                if os.name == "nt" and managed.job_handle is None and time.monotonic() >= next_desc_poll:
                    next_desc_poll = time.monotonic() + 0.2
                    managed.refresh_descendants()
                if stop_event.is_set():
                    terminate_managed_process(
                        managed,
                        timeout=shutdown_timeout,
                        force_immediately=force_kill_event.is_set(),
                    )
                    break
                if is_watched and time.monotonic() >= next_watch_poll:
                    next_watch_poll = time.monotonic() + opts.watch_interval
                    new_snap = snapshot_tree(watch_dir, watch_patterns)
                    changed = diff_snapshots(snap, new_snap)
                    if changed is not None:
                        time.sleep(0.15)
                        snap = snapshot_tree(watch_dir, watch_patterns)
                        writer.write_system(
                            f"Change detected in {changed.name} — restarting '{name}'...",
                            CYAN,
                        )
                        restart_requested = True
                        terminate_managed_process(managed, timeout=shutdown_timeout)
                        break
                time.sleep(0.05)

            attempt_done.set()
            if probe_thread is not None:
                probe_thread.join(timeout=1.5)
            reader.join(timeout=2.0)
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass

            with procs_lock:
                active_procs.pop(name, None)
            if stop_event.is_set():
                # Ensure any descendants tracked before an early parent exit are also terminated.
                terminate_managed_process(
                    managed,
                    timeout=shutdown_timeout,
                    force_immediately=force_kill_event.is_set(),
                )
            managed.close()

            exit_code = proc.returncode if proc.returncode is not None else 1
            duration = time.monotonic() - start

            if restart_requested and not stop_event.is_set():
                writer.flush_service(name)
                ready_events[name].set()
                env, _ = _build_task_env(config, task, workspace_dotenv)
                continue

            if probe_timed_out.is_set():
                status = "failed"
                exit_code = exit_code if exit_code != 0 else 1
                dep_failed[name] = True
            elif interrupted_event.is_set() or (stop_event.is_set() and exit_code != 0):
                status = "stopped"
            elif exit_code == 0:
                status = "ok"
            else:
                status = "failed"
                if not is_watched:
                    dep_failed[name] = True

            writer.flush_service(name)
            ready_events[name].set()
            results_map[name] = TaskResult(
                name=name,
                exit_code=exit_code,
                duration_seconds=duration,
                status=status,
            )

            if status == "ok":
                writer.write_line(
                    name,
                    style(f"exited with code 0 ({duration:.2f}s)", GREEN, enabled=use_color),
                    force_visible=True,
                )
            elif status == "stopped":
                writer.write_line(
                    name,
                    style(f"stopped ({duration:.2f}s)", YELLOW, enabled=use_color),
                    force_visible=True,
                )
            else:
                writer.write_line(
                    name,
                    style(
                        f"exited with code {exit_code} ({duration:.2f}s) — cmd: `{task.command}` @ {task.cwd or config.root}",
                        RED,
                        enabled=use_color,
                    ),
                    force_visible=True,
                )
                hint = _missing_executable_hint(task, config.root)
                if hint:
                    writer.write_line(name, style(hint, YELLOW, enabled=use_color), force_visible=True)
                if fail_fast and not is_watched and not stop_event.is_set():
                    stop_all(
                        f"Task '{name}' exited with code {exit_code} — stopping remaining services...",
                        force=False,
                    )

            if not is_watched or stop_event.is_set():
                break

            writer.write_line(
                name,
                style("watching for file changes...", DIM, enabled=use_color),
            )
            while not stop_event.is_set():
                time.sleep(opts.watch_interval)
                if stop_event.is_set():
                    break
                new_snap = snapshot_tree(watch_dir, watch_patterns)
                changed = diff_snapshots(snap, new_snap)
                if changed is not None:
                    time.sleep(0.15)
                    snap = snapshot_tree(watch_dir, watch_patterns)
                    writer.write_system(
                        f"Change detected in {changed.name} — restarting '{name}'...",
                        CYAN,
                    )
                    env, _ = _build_task_env(config, task, workspace_dotenv)
                    break

            if stop_event.is_set():
                break

    try:
        with ThreadPoolExecutor(max_workers=len(ordered_names), thread_name_prefix="devup-task") as pool:
            futures = [pool.submit(run_one, name) for name in ordered_names]
            for fut in futures:
                while not fut.done():
                    try:
                        # Short timeout lets the main thread wake promptly to process SIGINT/SIGTERM.
                        fut.result(timeout=0.1)
                    except TimeoutError:
                        pass
    finally:
        for t in stop_threads:
            t.join(timeout=1.0)
        if is_main:
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
            if prev_sigterm is not None and hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, prev_sigterm)
        ordered_results = [results_map[name] for name in ordered_names if name in results_map]
        _print_summary(writer, ordered_results, use_color, interrupted_event.is_set())
        if log_handle is not None:
            log_handle.close()

    return ordered_results


def _print_summary(
    writer: _OutputWriter,
    results: list[TaskResult],
    use_color: bool,
    interrupted: bool,
) -> None:
    """Print a concise execution summary after all tasks finish."""
    if not results:
        return
    parts: list[str] = []
    for res in results:
        if res.status == "ok":
            badge = style("OK", GREEN, enabled=use_color)
        elif res.status == "stopped":
            badge = style("STOPPED", YELLOW, enabled=use_color)
        elif res.status == "skipped":
            badge = style("SKIPPED", YELLOW, enabled=use_color)
        else:
            badge = style(f"FAIL({res.exit_code})", RED, enabled=use_color)
        parts.append(f"{res.name}:{badge}")

    header = "Interrupted" if interrupted else "Summary"
    writer.write_system(f"{header}: " + " | ".join(parts), CYAN)
