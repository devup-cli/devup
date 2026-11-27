"""Cross-platform process-tree spawning and reliable termination."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass(slots=True)
class ManagedProcess:
    """Tracks a subprocess along with its OS process-group or Windows Job Object."""

    proc: subprocess.Popen[str]
    pgid: int
    job_handle: int | None = None

    def close(self) -> None:
        """Release OS handles associated with this managed process."""
        if os.name == "nt" and self.job_handle is not None:
            _close_windows_job(self.job_handle)
            self.job_handle = None


def _create_windows_job_for_process(proc: subprocess.Popen[str]) -> int | None:
    """Create a kill-on-close Windows Job Object and assign proc to it."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000) | JOB_OBJECT_LIMIT_BREAKAWAY_OK (0x0800)
        info.BasicLimitInformation.LimitFlags = 0x2000
        job_object_extended_limit_info = 9
        ok_info = kernel32.SetInformationJobObject(
            job,
            job_object_extended_limit_info,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok_info:
            kernel32.CloseHandle(job)
            return None

        proc_handle = int(proc._handle)  # type: ignore[attr-defined]
        ok_assign = kernel32.AssignProcessToJobObject(job, proc_handle)
        if not ok_assign:
            kernel32.CloseHandle(job)
            return None

        return int(job)
    except Exception:
        return None


def _terminate_windows_job(job_handle: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        return bool(kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1))
    except Exception:
        return False


def _close_windows_job(job_handle: int) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))
    except Exception:
        pass


def spawn_managed_process(
    command: str,
    cwd: str,
    env: dict[str, str],
) -> ManagedProcess:
    """Start a shell command in an isolated process tree / Job Object."""
    popen_kwargs: dict[str, object] = {
        "shell": True,
        "cwd": cwd,
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

    proc = subprocess.Popen(command, **popen_kwargs)  # type: ignore[arg-type]
    job_handle = _create_windows_job_for_process(proc) if os.name == "nt" else None
    return ManagedProcess(proc=proc, pgid=proc.pid, job_handle=job_handle)


def terminate_managed_process(
    managed: ManagedProcess,
    timeout: float,
    force_immediately: bool = False,
) -> None:
    """Gracefully stop a process tree, then force-kill all descendants."""
    proc = managed.proc

    if os.name == "nt":
        if not force_immediately and proc.poll() is None:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                pass
            deadline = time.monotonic() + min(timeout, 1.5)
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)

        # Always sweep the full tree via Job Object or taskkill even if cmd.exe exited early.
        if managed.job_handle is not None:
            _terminate_windows_job(managed.job_handle)
            managed.close()

        if proc.poll() is None:
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

    # POSIX: signal the entire process group (pgid == proc.pid via start_new_session=True).
    if not force_immediately and proc.poll() is None:
        try:
            os.killpg(managed.pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            proc.terminate()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)

    # Always issue SIGKILL to the process group to sweep surviving grandchildren.
    try:
        os.killpg(managed.pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if proc.poll() is None:
            proc.kill()

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
