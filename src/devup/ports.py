"""Cross-platform port availability check and owning process lookup."""

from __future__ import annotations

import csv
import io
import os
import re
import socket
import subprocess
from dataclasses import dataclass

from devup.models import Task

_PORT_PATTERNS = (
    re.compile(r"--port(?:=|\s+)(\d{2,5})\b"),
    re.compile(r"(?<![\w.])PORT=(\d{2,5})\b"),
)


@dataclass(slots=True)
class PortConflict:
    """Represents an occupied or duplicated TCP port detected before launch."""

    task_name: str
    port: int
    reason: str
    owner: str | None = None


def extract_ports_from_command(command: str) -> list[int]:
    """Conservatively infer listening ports from explicit --port / PORT= flags."""
    found: list[int] = []
    for pattern in _PORT_PATTERNS:
        for match in pattern.finditer(command):
            port = int(match.group(1))
            if 1 <= port <= 65535 and port not in found:
                found.append(port)
    return found


def is_port_in_use(port: int, timeout: float = 0.15) -> bool:
    """Return True if a TCP listener accepts connections on localhost:port."""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _lookup_windows_owner(port: int) -> str | None:
    try:
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError:
        return None

    pid: str | None = None
    suffix = f":{port}"
    for line in netstat.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            local_addr = parts[1]
            if local_addr.endswith(suffix):
                pid = parts[4]
                break

    if not pid:
        return None

    try:
        tasklist = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        rows = list(csv.reader(io.StringIO(tasklist.stdout.strip())))
        if rows and len(rows[0]) >= 1 and not rows[0][0].startswith("INFO:"):
            return f"PID {pid} ({rows[0][0]})"
    except OSError:
        pass
    return f"PID {pid}"


def _lookup_posix_owner(port: int) -> str | None:
    try:
        lsof = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp", "-Fc"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if lsof.returncode == 0 and lsof.stdout.strip():
            pid = None
            cmd = None
            for line in lsof.stdout.splitlines():
                if line.startswith("p") and pid is None:
                    pid = line[1:]
                elif line.startswith("c") and cmd is None:
                    cmd = line[1:]
            if pid and cmd:
                return f"PID {pid} ({cmd})"
            if pid:
                return f"PID {pid}"
    except OSError:
        pass

    try:
        ss = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        pattern = re.compile(rf":{port}\b.*users:\(\(\"([^\"]+)\",pid=(\d+)")
        for line in ss.stdout.splitlines():
            match = pattern.search(line)
            if match:
                return f"PID {match.group(2)} ({match.group(1)})"
    except OSError:
        pass

    return None


def describe_port_owner(port: int) -> str | None:
    """Best-effort lookup of the process currently listening on port."""
    if os.name == "nt":
        return _lookup_windows_owner(port)
    return _lookup_posix_owner(port)


def check_task_ports(tasks: dict[str, Task], ordered_names: list[str]) -> list[PortConflict]:
    """Check declared or inferred task ports for active listeners or duplicate bindings."""
    conflicts: list[PortConflict] = []
    claimed: dict[int, str] = {}

    for name in ordered_names:
        task = tasks[name]
        ports = list(task.ports) if task.ports else extract_ports_from_command(task.command)
        for port in ports:
            if port in claimed and claimed[port] != name:
                conflicts.append(
                    PortConflict(
                        task_name=name,
                        port=port,
                        reason=f"port {port} is also configured for '{claimed[port]}'",
                    )
                )
                continue
            claimed[port] = name
            if is_port_in_use(port):
                owner = describe_port_owner(port)
                owner_note = f" by {owner}" if owner else ""
                conflicts.append(
                    PortConflict(
                        task_name=name,
                        port=port,
                        reason=f"port {port} is already in use{owner_note}",
                        owner=owner,
                    )
                )

    return conflicts
