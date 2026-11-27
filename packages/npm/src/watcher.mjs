import { readdirSync, realpathSync, statSync } from "node:fs";
import { basename, join, relative, resolve } from "node:path";

import { fnmatchToRegex, isDir } from "./fs_utils.mjs";

export const DEFAULT_IGNORE_DIRS = new Set([
  ".git",
  ".hg",
  ".svn",
  "node_modules",
  "__pycache__",
  ".venv",
  "venv",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  "dist",
  "build",
  ".next",
  ".turbo",
  "target",
  ".eggs",
]);

function matchesAnyPattern(relPosix, patterns) {
  const filename = relPosix.includes("/") ? relPosix.slice(relPosix.lastIndexOf("/") + 1) : relPosix;
  for (const raw of patterns) {
    const pat = raw.replaceAll("\\", "/").replace(/^\.\//, "");
    if (!pat) continue;
    if (!pat.includes("/") && fnmatchToRegex(pat).test(filename)) {
      return true;
    }
    if (fnmatchToRegex(pat).test(relPosix)) {
      return true;
    }
    if (pat.includes("**/") && fnmatchToRegex(pat.replace("**/", "")).test(relPosix)) {
      return true;
    }
  }
  return false;
}

export function snapshotTree(baseDir, patterns = null, ignoreDirs = DEFAULT_IGNORE_DIRS) {
  const snapshots = new Map();
  if (!isDir(baseDir)) return snapshots;

  let resolvedBase;
  try {
    resolvedBase = realpathSync(baseDir);
  } catch {
    resolvedBase = resolve(baseDir);
  }

  const queue = [resolvedBase];
  while (queue.length > 0) {
    const currentDir = queue.shift();
    let entries;
    try {
      entries = readdirSync(currentDir, { withFileTypes: true });
    } catch {
      continue;
    }

    for (const entry of entries) {
      if (entry.isDirectory()) {
        if (!ignoreDirs.has(entry.name) && !entry.name.endsWith(".egg-info")) {
          queue.push(join(currentDir, entry.name));
        }
        continue;
      }
      if (!entry.isFile()) continue;

      const fullPath = join(currentDir, entry.name);
      if (patterns && patterns.length > 0) {
        const relPosix = relative(resolvedBase, fullPath).replaceAll("\\", "/");
        if (!relPosix || relPosix.startsWith("..") || !matchesAnyPattern(relPosix, patterns)) {
          continue;
        }
      }
      try {
        const st = statSync(fullPath, { bigint: true });
        snapshots.set(fullPath, st.mtimeNs);
      } catch {
        continue;
      }
    }
  }

  return snapshots;
}

export function diffSnapshots(prev, curr) {
  for (const [filePath, mtime] of curr.entries()) {
    const oldMtime = prev.get(filePath);
    if (oldMtime === undefined || oldMtime !== mtime) {
      return { path: filePath, name: basename(filePath) };
    }
  }
  for (const filePath of prev.keys()) {
    if (!curr.has(filePath)) {
      return { path: filePath, name: basename(filePath) };
    }
  }
  return null;
}
