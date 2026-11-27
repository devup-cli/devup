"""Parallel multi-service process runner with prefixed logs and graceful shutdown."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
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
from devup.models import Task, TaskResult, WorkspaceConfig


@dataclass(slots=True)
class RunOptions:
    """Runtime options controlling runner behavior and log formatting."""

    fail_fast: bool | None = None
    shutdown_timeout: float | None = None
    timestamps: bool = False
    prefix: bool = True
    color: bool | None = None
    dry_run: bool = False


class _OutputWriter:
    """Thread-safe log formatter for interleaved subprocess output."""

    def __init__(
        self,
        stream: TextIO,
        service_colors: dict[str, str],
        pad_width: int,
        use_color: bool,
        timestamps: bool,
        show_prefix: bool,
    ) -> None:
        self._stream = stream
        self._colors = service_colors
        self._pad_width = pad_width
        self._use_color = use_color
        self._timestamps = timestamps
        self._show_prefix = show_prefix
        self._lock = threading.Lock()

    def write_line(self, service: str, line: str) -> None:
        """Emit a single line prefixed with optional timestamp and colored service tag."""
        parts: list[str] = []
        if self._timestamps:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            parts.append(style(ts, DIM, enabled=self._use_color))
        if self._show_prefix:
            tag = f"[{service:<{self._pad_width}}]"
            color = self._colors.get(service, "")
            parts.append(style(tag, BOLD, color, enabled=self._use_color and bool(color)))
        prefix = " ".join(parts)
        formatted = f"{prefix} {line}" if prefix else line
        with self._lock:
            self._stream.write(formatted + "\n")
            self._stream.flush()

    def write_system(self, message: str, color_code: str = CYAN) -> None:
        """Emit a runner lifecycle status message."""
        ts_prefix = ""
        if self._timestamps:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            ts_prefix = style(ts, DIM, enabled=self._use_color) + " "
        tag = style("[devup]", BOLD, color_code, enabled=self._use_color)
        with self._lock:
            self._stream.write(f"{ts_prefix}{tag} {message}\n")
            self._stream.flush()


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


def _terminate_process_tree(
    proc: subprocess.Popen[str],
    timeout: float,
    force_immediately: bool = False,
) -> None:
    """Gracefully terminate a process and its descendants, force-killing on timeout."""
    if proc.poll() is not None:
        return

    if os.name == "nt":
        if not force_immediately:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                pass
            deadline = time.monotonic() + min(timeout, 1.5)
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.05)

        if proc.poll() is None:
            # Ensure child processes (e.g. uvicorn workers, vite, node) are terminated too.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return

    # POSIX: signal the entire process group started via start_new_session=True.
    if not force_immediately:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            proc.terminate()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)

    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _build_task_env(config: WorkspaceConfig, task: Task) -> dict[str, str]:
    """Merge process environment, workspace env, and task-specific env."""
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.update(config.env)
    env.update(task.env)
    return env


def _spawn_task(task: Task, env: dict[str, str], root: os.PathLike[str] | str) -> subprocess.Popen[str]:
    """Start a task subprocess in its own process group with merged output pipe."""
    popen_kwargs: dict[str, object] = {
        "shell": True,
        "cwd": str(task.cwd or root),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    return subprocess.Popen(task.command, **popen_kwargs)  # type: ignore[arg-type]


def run_tasks(
    config: WorkspaceConfig,
    targets: list[str],
    options: RunOptions | None = None,
    stream: TextIO | None = None,
) -> list[TaskResult]:
    """Execute one or more tasks in parallel respecting dependencies and shutdown signals."""
    opts = options or RunOptions()
    out = stream or sys.stdout
    use_color = supports_color(out, force=opts.color)
    fail_fast = config.fail_fast if opts.fail_fast is None else opts.fail_fast
    shutdown_timeout = (
        config.shutdown_timeout if opts.shutdown_timeout is None else opts.shutdown_timeout
    )

    ordered_names = _expand_dependencies(config, targets)
    if not ordered_names:
        return []

    pad_width = min(24, max(len(name) for name in ordered_names))
    colors = assign_service_colors(ordered_names, enabled=use_color)
    writer = _OutputWriter(
        stream=out,
        service_colors=colors,
        pad_width=pad_width,
        use_color=use_color,
        timestamps=opts.timestamps,
        show_prefix=opts.prefix,
    )

    if opts.dry_run:
        writer.write_system(f"Dry run execution plan ({len(ordered_names)} task(s)):", CYAN)
        results: list[TaskResult] = []
        for name in ordered_names:
            task = config.tasks[name]
            deps_note = f" (after: {', '.join(task.depends_on)})" if task.depends_on else ""
            cwd_note = f" @ {task.cwd or config.root}"
            writer.write_line(name, f"{task.command}{deps_note}{cwd_note}")
            results.append(TaskResult(name=name, exit_code=0, duration_seconds=0.0, status="ok"))
        return results

    ready_events: dict[str, threading.Event] = {name: threading.Event() for name in ordered_names}
    dep_failed: dict[str, bool] = {name: False for name in ordered_names}
    ready_regexes: dict[str, re.Pattern[str] | None] = {
        name: re.compile(config.tasks[name].ready_pattern)
        if config.tasks[name].ready_pattern
        else None
        for name in ordered_names
    }

    stop_event = threading.Event()
    force_kill_event = threading.Event()
    interrupted_event = threading.Event()

    procs_lock = threading.Lock()
    active_procs: dict[str, subprocess.Popen[str]] = {}
    results_map: dict[str, TaskResult] = {}

    def stop_all(reason: str, force: bool = False) -> None:
        stop_event.set()
        if force:
            force_kill_event.set()
        with procs_lock:
            snapshot = list(active_procs.items())
        if snapshot:
            writer.write_system(reason, YELLOW)
        for _, proc in snapshot:
            _terminate_process_tree(proc, timeout=shutdown_timeout, force_immediately=force)
        for ev in ready_events.values():
            ev.set()

    # Register SIGINT / SIGTERM handlers safely when invoked on the main thread.
    prev_sigint = None
    prev_sigterm = None
    is_main = threading.current_thread() is threading.main_thread()

    def _handle_signal(signum: int, _frame: object) -> None:
        if not interrupted_event.is_set():
            interrupted_event.set()
            stop_all("Interrupt received — stopping running services...", force=False)
        else:
            stop_all("Second interrupt — force killing remaining services...", force=True)

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

        start = time.monotonic()
        env = _build_task_env(config, task)
        try:
            proc = _spawn_task(task, env, config.root)
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
            if fail_fast:
                stop_all(f"Task '{name}' failed to start — stopping other services...", force=False)
            return

        with procs_lock:
            active_procs[name] = proc

        pattern = ready_regexes[name]

        def pump_output() -> None:
            if proc.stdout is None:
                return
            try:
                for raw_line in proc.stdout:
                    cleaned = raw_line.rstrip("\r\n")
                    writer.write_line(name, cleaned)
                    if pattern is not None and not ready_events[name].is_set():
                        if pattern.search(cleaned):
                            ready_events[name].set()
            except ValueError:
                pass

        reader = threading.Thread(target=pump_output, name=f"devup-log-{name}", daemon=True)
        reader.start()

        # Monitor process exit while staying responsive to stop_event.
        while proc.poll() is None:
            if stop_event.is_set():
                _terminate_process_tree(
                    proc,
                    timeout=shutdown_timeout,
                    force_immediately=force_kill_event.is_set(),
                )
                break
            time.sleep(0.05)

        reader.join(timeout=1.0)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass

        with procs_lock:
            active_procs.pop(name, None)

        exit_code = proc.returncode if proc.returncode is not None else 1
        duration = time.monotonic() - start

        if interrupted_event.is_set() or (stop_event.is_set() and exit_code != 0):
            status = "stopped"
        elif exit_code == 0:
            status = "ok"
        else:
            status = "failed"
            dep_failed[name] = True

        # Unblock dependents (either completed ok, or marked failed).
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
            )
        elif status == "stopped":
            writer.write_line(
                name,
                style(f"stopped ({duration:.2f}s)", YELLOW, enabled=use_color),
            )
        else:
            writer.write_line(
                name,
                style(f"exited with code {exit_code} ({duration:.2f}s)", RED, enabled=use_color),
            )
            if fail_fast and not stop_event.is_set():
                stop_all(
                    f"Task '{name}' exited with code {exit_code} — stopping remaining services...",
                    force=False,
                )

    try:
        with ThreadPoolExecutor(max_workers=len(ordered_names), thread_name_prefix="devup-task") as pool:
            futures = [pool.submit(run_one, name) for name in ordered_names]
            for fut in futures:
                fut.result()
    finally:
        if is_main:
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
            if prev_sigterm is not None and hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, prev_sigterm)

    ordered_results = [results_map[name] for name in ordered_names if name in results_map]
    _print_summary(writer, ordered_results, use_color, interrupted_event.is_set())
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
