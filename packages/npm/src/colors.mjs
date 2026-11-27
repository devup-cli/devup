/**
 * Terminal ANSI color and formatting helpers.
 */

export const RESET = "\x1b[0m";
export const BOLD = "\x1b[1m";
export const DIM = "\x1b[2m";

export const RED = "\x1b[31m";
export const GREEN = "\x1b[32m";
export const YELLOW = "\x1b[33m";
export const BLUE = "\x1b[34m";
export const MAGENTA = "\x1b[35m";
export const CYAN = "\x1b[36m";

export const SERVICE_PALETTE = [
  "\x1b[36m", // cyan
  "\x1b[33m", // yellow
  "\x1b[35m", // magenta
  "\x1b[32m", // green
  "\x1b[34m", // blue
  "\x1b[96m", // bright cyan
  "\x1b[93m", // bright yellow
  "\x1b[95m", // bright magenta
  "\x1b[92m", // bright green
  "\x1b[94m", // bright blue
];

export function supportsColor(stream = process.stdout, force = null) {
  if (force !== null && force !== undefined) {
    return Boolean(force);
  }
  if (process.env.NO_COLOR) {
    return false;
  }
  const forceEnv = process.env.FORCE_COLOR;
  if (forceEnv !== undefined && forceEnv !== "" && forceEnv !== "0") {
    return true;
  }
  try {
    return Boolean(stream && stream.isTTY);
  } catch {
    return false;
  }
}

export function style(text, ...codes) {
  let enabled = true;
  let actualCodes = codes;
  if (
    codes.length > 0 &&
    typeof codes[codes.length - 1] === "object" &&
    codes[codes.length - 1] !== null &&
    "enabled" in codes[codes.length - 1]
  ) {
    enabled = Boolean(codes[codes.length - 1].enabled);
    actualCodes = codes.slice(0, -1);
  }
  if (!enabled || actualCodes.length === 0) {
    return text;
  }
  const active = actualCodes.filter(Boolean);
  if (active.length === 0) {
    return text;
  }
  return `${active.join("")}${text}${RESET}`;
}

export function assignServiceColors(names, enabled = true) {
  const result = {};
  for (let idx = 0; idx < names.length; idx += 1) {
    result[names[idx]] = enabled ? SERVICE_PALETTE[idx % SERVICE_PALETTE.length] : "";
  }
  return result;
}
