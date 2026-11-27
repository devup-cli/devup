import { spawn } from "node:child_process";

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

/**
 * Pure Windows fallback (zero Python dependency) using PowerShell + Win32
 * CreateToolhelp32Snapshot / TerminateProcess when taskkill and CIM are unavailable.
 */
async function psToolhelpKillTree(rootPid) {
  if (process.platform !== "win32" || !rootPid) return false;
  const cs = `
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class DevupTreeKill {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  public struct PE {
    public uint dwSize, cntUsage, pid;
    public UIntPtr heap;
    public uint mod, thr, ppid;
    public int pri;
    public uint flags;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
    public string exe;
  }
  [DllImport("kernel32.dll")] public static extern IntPtr CreateToolhelp32Snapshot(uint f, uint p);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern bool Process32FirstW(IntPtr s, ref PE e);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern bool Process32NextW(IntPtr s, ref PE e);
  [DllImport("kernel32.dll")] public static extern IntPtr OpenProcess(uint a, bool inh, uint p);
  [DllImport("kernel32.dll")] public static extern bool TerminateProcess(IntPtr h, uint c);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
  public static void Kill(uint root) {
    IntPtr s = CreateToolhelp32Snapshot(2, 0);
    if (s == IntPtr.Zero || s == new IntPtr(-1)) return;
    var ch = new Dictionary<uint, List<uint>>();
    PE e = new PE();
    e.dwSize = (uint)Marshal.SizeOf(typeof(PE));
    bool ok = Process32FirstW(s, ref e);
    while (ok) {
      if (!ch.ContainsKey(e.ppid)) ch[e.ppid] = new List<uint>();
      ch[e.ppid].Add(e.pid);
      ok = Process32NextW(s, ref e);
    }
    CloseHandle(s);
    var pids = new List<uint>();
    var q = new Queue<uint>();
    var seen = new HashSet<uint>();
    q.Enqueue(root);
    while (q.Count > 0) {
      uint p = q.Dequeue();
      if (!seen.Add(p)) continue;
      pids.Add(p);
      if (ch.ContainsKey(p)) foreach (uint c in ch[p]) q.Enqueue(c);
    }
    for (int i = pids.Count - 1; i >= 0; i--) {
      IntPtr h = OpenProcess(1, false, pids[i]);
      if (h != IntPtr.Zero) { TerminateProcess(h, 1); CloseHandle(h); }
    }
  }
}`;
  const psScript = `Add-Type -TypeDefinition @'\n${cs}\n'@\n[DevupTreeKill]::Kill(${Number(rootPid)})`;
  const encoded = Buffer.from(psScript, "utf16le").toString("base64");
  const res = await runQuick(
    "powershell",
    ["-NoProfile", "-NoLogo", "-NonInteractive", "-InputFormat", "None", "-EncodedCommand", encoded],
    15000
  );
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
        await psToolhelpKillTree(managed.pid);
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
