import { readFileSync, statSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

const KEY_RE = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$/;

function unescapeDoubleQuoted(value) {
  return value
    .replaceAll("\\n", "\n")
    .replaceAll("\\r", "\r")
    .replaceAll("\\t", "\t")
    .replaceAll('\\"', '"')
    .replaceAll("\\\\", "\\");
}

function parseQuotedValue(raw, quote) {
  const chars = [];
  let escaped = false;
  for (let i = 1; i < raw.length; i += 1) {
    const ch = raw[i];
    if (quote === '"' && escaped) {
      chars.push("\\" + ch);
      escaped = false;
      continue;
    }
    if (quote === '"' && ch === "\\") {
      escaped = true;
      continue;
    }
    if (ch === quote) {
      const joined = chars.join("");
      return quote === '"' ? unescapeDoubleQuoted(joined) : joined;
    }
    chars.push(ch);
  }
  const joined = chars.join("");
  return quote === '"' ? unescapeDoubleQuoted(joined) : joined;
}

function stripUnquotedComment(raw) {
  for (let idx = 0; idx < raw.length; idx += 1) {
    if (raw[idx] === "#" && idx > 0 && /\s/.test(raw[idx - 1])) {
      return raw.slice(0, idx).trim();
    }
  }
  return raw.trim();
}

export function parseDotenv(text) {
  const result = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }
    const match = KEY_RE.exec(line);
    if (!match) {
      continue;
    }
    const key = match[1];
    const rawVal = match[2].trimStart();
    if (!rawVal) {
      result[key] = "";
    } else if (rawVal[0] === '"' || rawVal[0] === "'") {
      result[key] = parseQuotedValue(rawVal, rawVal[0]);
    } else {
      result[key] = stripUnquotedComment(rawVal);
    }
  }
  return result;
}

function isFile(filePath) {
  try {
    return statSync(filePath).isFile();
  } catch {
    return false;
  }
}

export function loadEnvFiles(fileRefs, baseDir, warnMissing = true) {
  const merged = {};
  const warnings = [];

  for (const ref of fileRefs) {
    const target = isAbsolute(ref) ? resolve(ref) : resolve(baseDir, ref);
    if (!isFile(target)) {
      if (warnMissing) {
        warnings.push(`Env file not found: ${target}`);
      }
      continue;
    }
    try {
      const content = readFileSync(target, "utf8");
      Object.assign(merged, parseDotenv(content));
    } catch (err) {
      warnings.push(`Failed to read env file ${target}: ${err.message}`);
    }
  }

  return { env: merged, warnings };
}
