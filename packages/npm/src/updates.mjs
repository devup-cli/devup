import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import { isFile } from "./fs_utils.mjs";

const NPM_REGISTRY_URL = "https://registry.npmjs.org/@devup-cli%2fdevup";
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;

export function parseVersionTuple(version) {
  const parts = [];
  const clean = String(version).trim().replace(/^v/, "");
  for (const raw of clean.split(".")) {
    const m = /^(\d+)/.exec(raw);
    if (!m) break;
    parts.push(Number(m[1]));
  }
  return parts.length > 0 ? parts : [0];
}

export function isNewerVersion(latest, current) {
  const a = parseVersionTuple(latest);
  const b = parseVersionTuple(current);
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i += 1) {
    const av = a[i] ?? 0;
    const bv = b[i] ?? 0;
    if (av > bv) return true;
    if (av < bv) return false;
  }
  return false;
}

export function getUpdateCacheFile() {
  const base =
    process.platform === "win32"
      ? process.env.LOCALAPPDATA || join(homedir(), "AppData", "Local")
      : process.env.XDG_CACHE_HOME || join(homedir(), ".cache");
  return join(base, "devup", "npm_update_check.json");
}

export async function checkForUpdate(
  currentVersion,
  { force = false, timeoutSeconds = 0.8, registryUrl = NPM_REGISTRY_URL, fetchFn = null } = {}
) {
  const disabled = String(process.env.DEVUP_NO_UPDATE_CHECK || "").toLowerCase();
  if (disabled === "1" || disabled === "true" || disabled === "yes") {
    return null;
  }

  const cachePath = getUpdateCacheFile();
  const nowSec = Date.now() / 1000;

  if (!force && isFile(cachePath)) {
    try {
      const cached = JSON.parse(readFileSync(cachePath, "utf8"));
      if ((nowSec - Number(cached.checked_at || 0)) * 1000 < CACHE_TTL_MS && cached.latest) {
        const latest = String(cached.latest);
        return {
          current: currentVersion,
          latest,
          update_available: isNewerVersion(latest, currentVersion),
        };
      }
    } catch {
      // Ignore corrupt cache
    }
  }

  const doFetch = fetchFn || globalThis.fetch;
  if (typeof doFetch !== "function") return null;

  let latest;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.max(100, Math.round(timeoutSeconds * 1000)));
  try {
    const resp = await doFetch(registryUrl, {
      headers: {
        "User-Agent": `@devup-cli/devup/${currentVersion}`,
        Accept: "application/vnd.npm.install-v1+json, application/json",
      },
      signal: controller.signal,
    });
    if (!resp.ok) return null;
    const payload = await resp.json();
    latest = payload?.["dist-tags"]?.latest || payload?.version;
    if (!latest) return null;
    latest = String(latest);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }

  try {
    mkdirSync(dirname(cachePath), { recursive: true });
    writeFileSync(cachePath, JSON.stringify({ checked_at: nowSec, latest }), "utf8");
  } catch {
    // Non-fatal cache write failure
  }

  return {
    current: currentVersion,
    latest,
    update_available: isNewerVersion(latest, currentVersion),
  };
}
