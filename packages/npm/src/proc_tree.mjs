import { spawn } from "node:child_process";

import { which } from "./fs_utils.mjs";

let taskkillUnavailable = false;
let cimUnavailable = false;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function runQuick(cmd, args, timeoutMs = 1500) {
  return new Promise((resolve) => {
    let settled = false;
    let stdout = "";
    let stderr = "";
    let proc;
    try {
      proc = spawn(cmd, args, {
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (err) {
      resolve({ ok: false, code: -1, stdout: "", stderr: err.message });
      return;
    }

    const done = (ok, code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok, code, stdout, stderr });
    };

    const timer = setTimeout(() => {
      try {
        proc.kill("SIGKILL");
      } catch {
        // Ignore
      }
      done(false, -1);
    }, timeoutMs);

    proc.stdout?.on("data", (d) => {
      stdout += d.toString("utf8");
    });
    proc.stderr?.on("data", (d) => {
      stderr += d.toString("utf8");
    });
    proc.once("error", () => done(false, -1));
    proc.once("close", (code) => done(code === 0, code ?? -1));
  });
}

export async function snapshotWindowsDescendants(rootPid) {
  if (process.platform !== "win32" || !rootPid || cimUnavailable) return [];
  const res = await runQuick(
    "powershell",
    [
      "-NoProfile",
      "-NoLogo",
      "-NonInteractive",
      "-InputFormat",
      "None",
      "-Command",
      'Get-CimInstance Win32_Process | ForEach-Object { "$($_.ParentProcessId),$($_.ProcessId)" }',
    ],
    1500
  );
  if (!res.ok) {
    cimUnavailable = true;
    return [];
  }

  const childrenByParent = new Map();
  for (const line of res.stdout.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const [ppidStr, pidStr] = trimmed.split(",", 2);
    const ppid = Number(ppidStr);
    const pid = Number(pidStr);
    if (Number.isInteger(ppid) && Number.isInteger(pid)) {
      if (!childrenByParent.has(ppid)) childrenByParent.set(ppid, []);
      childrenByParent.get(ppid).push(pid);
    }
  }

  const descendants = [];
  const queue = [...(childrenByParent.get(rootPid) || [])];
  const seen = new Set([rootPid]);
  while (queue.length > 0) {
    const pid = queue.shift();
    if (seen.has(pid)) continue;
    seen.add(pid);
    descendants.push(pid);
    for (const child of childrenByParent.get(pid) || []) {
      queue.push(child);
    }
  }
  return descendants;
}

async function win32ToolhelpKillTree(rootPid) {
  if (process.platform !== "win32" || !rootPid) return false;
  const py = which("python") || which("python3");
  if (!py) return false;

  const script = [
    "import ctypes, sys",
    "from ctypes import wintypes",
    "root = int(sys.argv[1])",
    "k32 = ctypes.windll.kernel32",
    "class PE(ctypes.Structure):",
    "  _fields_ = [('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',ctypes.c_long),('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]",
    "snap = k32.CreateToolhelp32Snapshot(2, 0)",
    "children = {}",
    "e = PE(); e.dwSize = ctypes.sizeof(PE)",
    "ok = k32.Process32FirstW(snap, ctypes.byref(e))",
    "while ok:",
    "  children.setdefault(int(e.th32ParentProcessID), []).append(int(e.th32ProcessID))",
    "  ok = k32.Process32NextW(snap, ctypes.byref(e))",
    "k32.CloseHandle(snap)",
    "pids, q, seen = [], [root], set()",
    "while q:",
    "  p = q.pop(0)",
    "  if p in seen: continue",
    "  seen.add(p); pids.append(p)",
    "  q.extend(children.get(p, ()))",
    "for p in reversed(pids):",
    "  h = k32.OpenProcess(1, False, p)",
    "  if h: k32.TerminateProcess(h, 1); k32.CloseHandle(h)",
  ].join("\n");

  const res = await runQuick(py, ["-c", script, String(rootPid)], 1500);
  return res.ok;
}

export function spawnManagedProcess(command, cwd, env) {
  const isWin = process.platform === "win32";
  const proc = spawn(command, {
    shell: true,
    cwd,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    detached: !isWin,
    windowsHide: isWin,
  });

  const managed = {
    proc,
    pid: proc.pid ?? null,
    pgid: proc.pid ?? null,
    exited: false,
    exitCode: null,
    spawnError: null,
    knownDescendants: new Set(),
    refreshing: false,
    exitPromise: null,
  };

  managed.exitPromise = new Promise((resolve) => {
    proc.once("error", (err) => {
      managed.spawnError = err;
      managed.exited = true;
      if (managed.exitCode === null) managed.exitCode = 127;
      resolve(managed.exitCode);
    });
    proc.once("close", (code, signal) => {
      managed.exited = true;
      if (code !== null && code !== undefined) {
        managed.exitCode = code;
      } else if (signal) {
        managed.exitCode = 1;
      } else {
        managed.exitCode = 1;
      }
      resolve(managed.exitCode);
    });
    proc.once("exit", (code, signal) => {
      managed.exited = true;
      if (managed.exitCode === null) {
        managed.exitCode = code !== null && code !== undefined ? code : signal ? 1 : 1;
      }
    });
  });

  return managed;
}

export function scheduleDescendantSnapshot(managed) {
  if (
    process.platform !== "win32" ||
    !managed.pid ||
    managed.exited ||
    managed.refreshing ||
    cimUnavailable
  ) {
    return;
  }
  managed.refreshing = true;
  snapshotWindowsDescendants(managed.pid)
    .then((pids) => {
      for (const p of pids) managed.knownDescendants.add(p);
    })
    .catch(() => {})
    .finally(() => {
      managed.refreshing = false;
    });
}

export async function terminateManagedProcess(managed, timeoutSeconds, forceImmediately = false) {
  const proc = managed.proc;
  if (!managed.pid) return;

  if (process.platform === "win32") {
    if (!forceImmediately && !managed.exited && timeoutSeconds > 0) {
      await Promise.race([
        managed.exitPromise,
        sleep(Math.min(Math.round(timeoutSeconds * 1000), 80)),
      ]);
    }

    // 1. Standard Windows tree termination via taskkill /F /T.
    let treeKilled = false;
    if (!taskkillUnavailable) {
      const tk = await runQuick("taskkill", ["/F", "/T", "/PID", String(managed.pid)], 1200);
      treeKilled = tk.ok;
      if (!tk.ok && /user name or password is incorrect/i.test(tk.stderr)) {
        taskkillUnavailable = true;
      }
    }

    // 2. If taskkill failed (e.g. restricted sandbox token or exited parent), sweep descendants.
    if (!treeKilled) {
      const cimPids = await snapshotWindowsDescendants(managed.pid);
      for (const p of cimPids) managed.knownDescendants.add(p);

      if (managed.knownDescendants.size > 0) {
        for (const childPid of [...managed.knownDescendants].sort((a, b) => b - a)) {
          try {
            process.kill(childPid, "SIGKILL");
          } catch {
            // Already exited
          }
        }
      } else {
        await win32ToolhelpKillTree(managed.pid);
      }
    }

    if (!managed.exited) {
      try {
        proc.kill("SIGKILL");
      } catch {
        // Ignore
      }
    }

    try {
      proc.stdout?.destroy();
      proc.stderr?.destroy();
    } catch {
      // Ignore
    }
    await Promise.race([managed.exitPromise, sleep(1000)]);
    return;
  }

  // POSIX: signal the entire process group (-pgid via detached: true).
  if (!forceImmediately && !managed.exited) {
    try {
      process.kill(-managed.pgid, "SIGTERM");
    } catch {
      try {
        proc.kill("SIGTERM");
      } catch {
        // Already gone
      }
    }

    const graceMs = Math.max(100, Math.round(timeoutSeconds * 1000));
    await Promise.race([managed.exitPromise, sleep(graceMs)]);
  }

  try {
    process.kill(-managed.pgid, "SIGKILL");
  } catch {
    if (!managed.exited) {
      try {
        proc.kill("SIGKILL");
      } catch {
        // Ignore
      }
    }
  }

  try {
    proc.stdout?.destroy();
    proc.stderr?.destroy();
  } catch {
    // Ignore
  }
  await Promise.race([managed.exitPromise, sleep(1000)]);
}
