import { readFileSync } from "node:fs";
import os from "node:os";
import { isAbsolute, join, resolve } from "node:path";

import { loadEnvFiles } from "./dotenv.mjs";
import { discoverWorkspace, resolveTargets } from "./discovery.mjs";
import { isDir, isFile, which } from "./fs_utils.mjs";
import { extractPortsFromCommand } from "./ports.mjs";
import { normalizeTcpTarget, validateReadyUrl } from "./readiness.mjs";
import { expandDependencies } from "./runner.mjs";
import { parseToml } from "./toml.mjs";
import { checkForUpdate } from "./updates.mjs";
import { VERSION } from "./version.mjs";

const KNOWN_TOP_LEVEL_KEYS = new Set([
  "default",
  "fail_fast",
  "shutdown_timeout",
  "auto_env",
  "env_file",
  "strict_ports",
  "workspaces",
  "env",
  "groups",
  "tasks",
]);

const KNOWN_TASK_KEYS = new Set([
  "command",
  "description",
  "cwd",
  "env",
  "env_file",
  "depends_on",
  "ready_pattern",
  "ready_url",
  "ready_tcp",
  "ready_port",
  "ready_timeout",
  "port",
  "ports",
  "watch",
]);

const TOOL_BINARIES = [
  "git",
  "node",
  "npm",
  "pnpm",
  "yarn",
  "bun",
  "poetry",
  "pdm",
  "hatch",
  "poe",
  "make",
  "just",
  "docker",
  "java",
  "mvn",
  "gradle",
];

const SENSITIVE_KEY_RE =
  /(SECRET|TOKEN|KEY|PASS|PWD|AUTH|COOKIE|CREDENTIAL|PRIVATE|CERT|DSN|DATABASE_URL|CONN_STR)/i;

const URL_USERINFO_RE = /([A-Za-z][A-Za-z0-9+.-]*:\/\/)([^/\s:@"']+(?::[^/\s@"']*)?)@/g;

const KV_SECRET_RE =
  /(\b[A-Za-z0-9_]*(?:SECRET|TOKEN|KEY|PASS|PASSWORD|PWD|AUTH|COOKIE|CREDENTIAL|PRIVATE|DSN|DATABASE_URL)[A-Za-z0-9_]*\s*[=:]\s*)(?!Bearer\b|Basic\b)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi;

const BEARER_RE = /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi;
const JWT_RE = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\b/g;
const CLI_SECRET_FLAG_RE =
  /(--(?:token|password|passwd|secret|api[-_]?key|auth[-_]?token)\s*(?:=|\s)\s*)(?:"[^"]*"|'[^']*'|[^\s]+)/gi;

const WIN_USER_DIR_RE = /([A-Za-z]:[\\/]+Users[\\/]+)([^\\/:*?"<>|\s'"]+)/gi;
const POSIX_USER_DIR_RE = /(\/(?:Users|home)\/)([^/\s'"\\]+)/g;

function escapeRegex(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function currentUsername() {
  try {
    return os.userInfo().username || "";
  } catch {
    return process.env.USERNAME || process.env.USER || "";
  }
}

export function redactText(text) {
  let out = String(text);
  const home = os.homedir();
  if (home) {
    for (const variant of [home, home.replaceAll("\\", "/")]) {
      out = out.replace(new RegExp(escapeRegex(variant), "gi"), "~");
    }
  }

  out = out.replace(WIN_USER_DIR_RE, "$1<user>");
  out = out.replace(POSIX_USER_DIR_RE, "$1<user>");

  const username = currentUsername();
  if (username && username.length >= 3) {
    const flags = process.platform === "win32" ? "gi" : "g";
    const userRe = new RegExp(`(?<![A-Za-z0-9_-])${escapeRegex(username)}(?![A-Za-z0-9_-])`, flags);
    out = out.replace(userRe, "<user>");
  }

  out = out.replace(URL_USERINFO_RE, "$1<redacted>@");
  out = out.replace(BEARER_RE, "$1 <redacted>");
  out = out.replace(JWT_RE, "<redacted>");
  out = out.replace(KV_SECRET_RE, "$1<redacted>");
  out = out.replace(CLI_SECRET_FLAG_RE, "$1<redacted>");

  for (const [key, val] of Object.entries(process.env)) {
    if (val && val.length >= 6 && SENSITIVE_KEY_RE.test(key) && out.includes(val)) {
      out = out.replaceAll(val, "<redacted>");
    }
  }

  return out;
}

export function redactPath(targetPath) {
  let text = String(targetPath);
  const home = os.homedir();
  if (home) {
    for (const variant of [home, home.replaceAll("\\", "/")]) {
      if (text.toLowerCase().startsWith(variant.toLowerCase())) {
        text = "~" + text.slice(variant.length);
        break;
      }
    }
  }
  return redactText(text);
}

function validateRawToml(root, errors, warnings) {
  const tomlPath = join(root, "devup.toml");
  if (!isFile(tomlPath)) return;

  let data;
  try {
    data = parseToml(readFileSync(tomlPath, "utf8"));
  } catch (err) {
    errors.push(`Invalid TOML in devup.toml: ${err.message}`);
    return;
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    errors.push("devup.toml root must be a TOML table.");
    return;
  }

  for (const key of Object.keys(data)) {
    if (!KNOWN_TOP_LEVEL_KEYS.has(key)) {
      warnings.push(`Unknown top-level key '${key}' in devup.toml.`);
    }
  }

  const rawTasks = data.tasks;
  if (rawTasks !== undefined && (typeof rawTasks !== "object" || rawTasks === null || Array.isArray(rawTasks))) {
    errors.push("devup.toml `[tasks]` must be a table.");
    return;
  }

  if (rawTasks && typeof rawTasks === "object") {
    for (const [tname, entry] of Object.entries(rawTasks)) {
      if (typeof entry === "string") continue;
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
        errors.push(`Task '${tname}' must be a command string or table.`);
        continue;
      }
      for (const key of Object.keys(entry)) {
        if (!KNOWN_TASK_KEYS.has(key)) {
          warnings.push(`Task '${tname}' has unknown key '${key}'.`);
        }
      }
      if (typeof entry.command !== "string" || !entry.command.trim()) {
        errors.push(`Task '${tname}' is missing a non-empty 'command' string.`);
      }
      if ("ready_url" in entry) {
        if (typeof entry.ready_url !== "string") {
          errors.push(`Task '${tname}': ready_url must be a string.`);
        } else {
          try {
            validateReadyUrl(entry.ready_url);
          } catch (err) {
            errors.push(`Task '${tname}': ${err.message}`);
          }
        }
      }
      const rawTcp = "ready_tcp" in entry ? entry.ready_tcp : entry.ready_port;
      if (rawTcp !== undefined) {
        if (typeof rawTcp !== "string" && typeof rawTcp !== "number") {
          errors.push(`Task '${tname}': ready_tcp/ready_port must be a port or host:port.`);
        } else {
          try {
            normalizeTcpTarget(rawTcp);
          } catch (err) {
            errors.push(`Task '${tname}': ${err.message}`);
          }
        }
      }
      if ("port" in entry) {
        const p = entry.port;
        if (!Number.isInteger(p) || p < 1 || p > 65535) {
          errors.push(`Task '${tname}': port must be an integer between 1 and 65535.`);
        }
      }
      if ("ports" in entry) {
        const ps = entry.ports;
        if (!Array.isArray(ps) || ps.some((p) => !Number.isInteger(p) || p < 1 || p > 65535)) {
          errors.push(`Task '${tname}': ports must be a list of integers (1-65535).`);
        }
      }
    }
  }
}

export function validateWorkspace(root, scanWorkspaces = true) {
  const resolvedRoot = resolve(root);
  const errors = [];
  const warnings = [];

  validateRawToml(resolvedRoot, errors, warnings);
  const config = discoverWorkspace(resolvedRoot, scanWorkspaces);
  warnings.push(...config.warnings);

  for (const envRef of config.envFiles) {
    const envPath = isAbsolute(envRef) ? resolve(envRef) : resolve(resolvedRoot, envRef);
    if (!isFile(envPath)) {
      warnings.push(`Workspace env_file does not exist: ${envPath}`);
    }
  }

  for (const [groupName, members] of Object.entries(config.groups)) {
    if (!members || members.length === 0) {
      warnings.push(`Group '${groupName}' is empty.`);
      continue;
    }
    try {
      const expanded = resolveTargets(config, [groupName]);
      expandDependencies(config, expanded);
    } catch (err) {
      errors.push(`Group '${groupName}': ${err.message}`);
    }
  }

  if (config.defaultTargets.length > 0) {
    try {
      const expandedDefault = resolveTargets(config, config.defaultTargets);
      expandDependencies(config, expandedDefault);
    } catch (err) {
      errors.push(`Default target error: ${err.message}`);
    }
  }

  const claimedPorts = new Map();
  for (const [name, task] of Object.entries(config.tasks)) {
    if (task.cwd && !isDir(task.cwd)) {
      if (task.source === "devup.toml") {
        errors.push(`Task '${name}' cwd does not exist: ${task.cwd}`);
      } else {
        warnings.push(`Task '${name}' cwd does not exist: ${task.cwd}`);
      }
    }

    for (const envRef of task.envFiles) {
      const baseDir = task.cwd || resolvedRoot;
      const envPath = isAbsolute(envRef) ? resolve(envRef) : resolve(baseDir, envRef);
      if (!isFile(envPath)) {
        warnings.push(`Task '${name}' env_file does not exist: ${envPath}`);
      }
    }

    if (task.dependsOn.length > 0) {
      try {
        expandDependencies(config, [name]);
      } catch (err) {
        errors.push(`Task '${name}': ${err.message}`);
      }
    }

    const ports =
      task.ports && task.ports.length > 0
        ? [...task.ports]
        : extractPortsFromCommand(task.command);
    for (const port of ports) {
      if (!claimedPorts.has(port)) claimedPorts.set(port, []);
      claimedPorts.get(port).push(name);
    }
  }

  for (const [port, owners] of claimedPorts.entries()) {
    if (owners.length > 1) {
      warnings.push(`Port ${port} is declared by multiple tasks (${owners.join(", ")}).`);
    }
  }

  return {
    valid: errors.length === 0,
    root: resolvedRoot,
    errors,
    warnings,
    taskCount: Object.keys(config.tasks).length,
    groupCount: Object.keys(config.groups).length,
    toDict(redactPaths = false) {
      return {
        valid: this.valid,
        root: redactPaths ? redactPath(this.root) : String(this.root),
        task_count: this.taskCount,
        group_count: this.groupCount,
        errors: this.errors.map((e) => (redactPaths ? redactText(e) : e)),
        warnings: this.warnings.map((w) => (redactPaths ? redactText(w) : w)),
      };
    },
  };
}

export async function runDoctor(root, { online = false, scanWorkspaces = true } = {}) {
  const resolvedRoot = resolve(root);
  const validation = validateWorkspace(resolvedRoot, scanWorkspaces);
  const config = discoverWorkspace(resolvedRoot, scanWorkspaces);

  const runners = {};
  for (const tool of TOOL_BINARIES) {
    const found = which(tool);
    runners[tool] = {
      available: found !== null,
      path: found ? redactPath(found) : null,
    };
  }

  let dotenvKeys = [];
  const dotenvPath = join(resolvedRoot, ".env");
  if (isFile(dotenvPath)) {
    const loaded = loadEnvFiles([dotenvPath], resolvedRoot, false);
    dotenvKeys = Object.keys(loaded.env).sort();
  }

  let sensitiveEnvCount = 0;
  for (const key of Object.keys(process.env)) {
    if (SENSITIVE_KEY_RE.test(key)) sensitiveEnvCount += 1;
  }

  let updateInfo = null;
  if (online) {
    const status = await checkForUpdate(VERSION, { force: true });
    if (status) {
      updateInfo = status;
    }
  }

  const sources = [...new Set(Object.values(config.tasks).map((t) => t.source))].sort();

  return {
    devup_version: VERSION,
    runtime: "node",
    node_version: process.versions.node,
    platform: `${os.platform()}-${os.release()}-${os.arch()}`,
    os: process.platform === "win32" ? "nt" : "posix",
    cwd: redactPath(resolvedRoot),
    devup_toml_present: isFile(join(resolvedRoot, "devup.toml")),
    dotenv_present: isFile(dotenvPath),
    dotenv_keys: dotenvKeys,
    redacted_env_vars_count: sensitiveEnvCount,
    sources,
    runners,
    validation: validation.toDict(true),
    update_check: updateInfo,
  };
}
