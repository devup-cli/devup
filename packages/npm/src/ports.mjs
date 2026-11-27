import { execFile } from "node:child_process";
import net from "node:net";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const PORT_PATTERNS = [
  /--port(?:=|\s+)(\d{2,5})\b/g,
  /(?<![\w.])PORT=(\d{2,5})\b/g,
];

export function extractPortsFromCommand(command) {
  const found = [];
  for (const pattern of PORT_PATTERNS) {
    pattern.lastIndex = 0;
    let match;
    while ((match = pattern.exec(command)) !== null) {
      const port = Number(match[1]);
      if (port >= 1 && port <= 65535 && !found.includes(port)) {
        found.push(port);
      }
    }
  }
  return found;
}

function tryConnect(host, port, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const sock = net.createConnection({ host, port });
    const done = (ok) => {
      if (settled) return;
      settled = true;
      sock.destroy();
      resolve(ok);
    };
    sock.setTimeout(timeoutMs);
    sock.once("connect", () => done(true));
    sock.once("timeout", () => done(false));
    sock.once("error", () => done(false));
  });
}

export async function isPortInUse(port, timeoutSeconds = 0.15) {
  const timeoutMs = Math.max(20, Math.round(timeoutSeconds * 1000));
  for (const host of ["127.0.0.1", "::1"]) {
    if (await tryConnect(host, port, timeoutMs)) {
      return true;
    }
  }
  return false;
}

async function lookupWindowsOwner(port) {
  let stdout = "";
  try {
    ({ stdout } = await execFileAsync("netstat", ["-ano", "-p", "TCP"], { timeout: 2000 }));
  } catch {
    return null;
  }

  let pid = null;
  const suffix = `:${port}`;
  for (const line of stdout.split(/\r?\n/)) {
    const parts = line.trim().split(/\s+/);
    if (
      parts.length >= 5 &&
      parts[0].toUpperCase() === "TCP" &&
      parts[3].toUpperCase() === "LISTENING" &&
      parts[1].endsWith(suffix)
    ) {
      pid = parts[4];
      break;
    }
  }
  if (!pid) return null;

  try {
    const { stdout: taskOut } = await execFileAsync(
      "tasklist",
      ["/FI", `PID eq ${pid}`, "/FO", "CSV", "/NH"],
      { timeout: 2000 }
    );
    const trimmed = taskOut.trim();
    if (trimmed && !trimmed.startsWith("INFO:")) {
      const firstCell = trimmed.split(",", 1)[0].replace(/^"|"$/g, "");
      if (firstCell) return `PID ${pid} (${firstCell})`;
    }
  } catch {
    // Fallback to PID only
  }
  return `PID ${pid}`;
}

async function lookupPosixOwner(port) {
  try {
    const { stdout } = await execFileAsync(
      "lsof",
      ["-nP", `-iTCP:${port}`, "-sTCP:LISTEN", "-Fp", "-Fc"],
      { timeout: 2000 }
    );
    if (stdout.trim()) {
      let pid = null;
      let cmd = null;
      for (const line of stdout.split(/\r?\n/)) {
        if (line.startsWith("p") && !pid) pid = line.slice(1);
        else if (line.startsWith("c") && !cmd) cmd = line.slice(1);
      }
      if (pid && cmd) return `PID ${pid} (${cmd})`;
      if (pid) return `PID ${pid}`;
    }
  } catch {
    // Fall through to ss
  }

  try {
    const { stdout } = await execFileAsync("ss", ["-tlnp"], { timeout: 2000 });
    const pattern = new RegExp(`:${port}\\b.*users:\\(\\("([^"]+)",pid=(\\d+)`);
    for (const line of stdout.split(/\r?\n/)) {
      const m = pattern.exec(line);
      if (m) return `PID ${m[2]} (${m[1]})`;
    }
  } catch {
    // Best-effort lookup
  }
  return null;
}

export async function describePortOwner(port) {
  if (process.platform === "win32") {
    return lookupWindowsOwner(port);
  }
  return lookupPosixOwner(port);
}

export async function checkTaskPorts(tasks, orderedNames) {
  const conflicts = [];
  const claimed = new Map();

  for (const name of orderedNames) {
    const task = tasks[name];
    const ports =
      task.ports && task.ports.length > 0
        ? [...task.ports]
        : extractPortsFromCommand(task.command);

    for (const port of ports) {
      if (claimed.has(port) && claimed.get(port) !== name) {
        conflicts.push({
          taskName: name,
          port,
          reason: `port ${port} is also configured for '${claimed.get(port)}'`,
          owner: null,
        });
        continue;
      }
      claimed.set(port, name);
      if (await isPortInUse(port)) {
        const owner = await describePortOwner(port);
        const ownerNote = owner ? ` by ${owner}` : "";
        conflicts.push({
          taskName: name,
          port,
          reason: `port ${port} is already in use${ownerNote}`,
          owner,
        });
      }
    }
  }

  return conflicts;
}
