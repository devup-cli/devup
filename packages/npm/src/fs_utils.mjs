import { existsSync, readdirSync, statSync } from "node:fs";
import { delimiter, isAbsolute, join, resolve } from "node:path";

export function isFile(targetPath) {
  try {
    return statSync(targetPath).isFile();
  } catch {
    return false;
  }
}

export function isDir(targetPath) {
  try {
    return statSync(targetPath).isDirectory();
  } catch {
    return false;
  }
}

export function which(binary) {
  if (!binary) return null;
  if (binary.includes("/") || binary.includes("\\") || isAbsolute(binary)) {
    return isFile(binary) ? resolve(binary) : null;
  }

  const pathVar = process.env.PATH || process.env.Path || "";
  const dirs = pathVar.split(delimiter).filter(Boolean);
  const isWin = process.platform === "win32";
  const pathExts = isWin
    ? (process.env.PATHEXT || ".COM;.EXE;.BAT;.CMD")
        .split(";")
        .map((e) => e.trim())
        .filter(Boolean)
    : [""];

  for (const dir of dirs) {
    if (isWin) {
      const lower = binary.toLowerCase();
      const hasExt = pathExts.some((ext) => lower.endsWith(ext.toLowerCase()));
      const candidates = hasExt
        ? [join(dir, binary)]
        : [join(dir, binary), ...pathExts.map((ext) => join(dir, `${binary}${ext}`))];
      for (const cand of candidates) {
        if (isFile(cand)) return cand;
      }
    } else {
      const cand = join(dir, binary);
      if (isFile(cand)) {
        try {
          const st = statSync(cand);
          if ((st.mode & 0o111) !== 0) return cand;
        } catch {
          return cand;
        }
      }
    }
  }
  return null;
}

export function globSegmentToRegex(segment) {
  let out = "^";
  for (let i = 0; i < segment.length; i += 1) {
    const ch = segment[i];
    if (ch === "*") {
      out += "[^/]*";
    } else if (ch === "?") {
      out += "[^/]";
    } else if ("\\^$.|+()[]{}".includes(ch)) {
      out += "\\" + ch;
    } else {
      out += ch;
    }
  }
  out += "$";
  return new RegExp(out);
}

export function fnmatchToRegex(pattern) {
  let out = "^";
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i];
    if (ch === "*") {
      out += ".*";
    } else if (ch === "?") {
      out += ".";
    } else if ("\\^$.|+()[]{}".includes(ch)) {
      out += "\\" + ch;
    } else {
      out += ch;
    }
  }
  out += "$";
  return new RegExp(out);
}

export function expandGlobDirs(rootDir, pattern) {
  const clean = pattern.replaceAll("\\", "/").replace(/\/+$/, "").trim();
  if (!clean || clean.startsWith("!")) return [];

  const segments = clean.split("/").filter(Boolean);
  let currentDirs = [resolve(rootDir)];

  for (const seg of segments) {
    const nextDirs = [];
    if (seg === "**") {
      // Recursive zero-or-more directory walk
      const queue = [...currentDirs];
      const visited = new Set(queue);
      while (queue.length > 0) {
        const dir = queue.shift();
        nextDirs.push(dir);
        let entries = [];
        try {
          entries = readdirSync(dir, { withFileTypes: true });
        } catch {
          continue;
        }
        for (const entry of entries) {
          if (entry.isDirectory()) {
            const sub = join(dir, entry.name);
            if (!visited.has(sub)) {
              visited.add(sub);
              queue.push(sub);
            }
          }
        }
      }
      currentDirs = nextDirs;
      continue;
    }

    if (!seg.includes("*") && !seg.includes("?")) {
      for (const dir of currentDirs) {
        const cand = join(dir, seg);
        if (isDir(cand)) nextDirs.push(cand);
      }
      currentDirs = nextDirs;
      continue;
    }

    const re = globSegmentToRegex(seg);
    for (const dir of currentDirs) {
      let entries = [];
      try {
        entries = readdirSync(dir, { withFileTypes: true });
      } catch {
        continue;
      }
      const matched = entries
        .filter((e) => e.isDirectory() && re.test(e.name))
        .map((e) => join(dir, e.name))
        .sort();
      nextDirs.push(...matched);
    }
    currentDirs = nextDirs;
  }

  return currentDirs.sort();
}

export function formatError(err) {
  return err && typeof err.message === "string" ? err.message : String(err);
}

export function pathExists(targetPath) {
  return existsSync(targetPath);
}
