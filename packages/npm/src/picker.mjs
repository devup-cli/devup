import { createInterface } from "node:readline";

import { BOLD, CYAN, DIM, GREEN, style } from "./colors.mjs";

function renderMenu(choices, cursor, selected, out, useColor, redrawLines = 0) {
  if (redrawLines > 0) {
    out.write(`\x1b[${redrawLines}A\r`);
  }

  const header = style(
    "Select task(s) or group(s) (Up/Down move, Space toggle, Enter run, q cancel):",
    BOLD,
    CYAN,
    { enabled: useColor }
  );
  const lines = [header];

  for (let idx = 0; idx < choices.length; idx += 1) {
    const choice = choices[idx];
    const pointer = idx === cursor ? style(">", BOLD, CYAN, { enabled: useColor }) : " ";
    const mark = selected.has(idx) ? "[x]" : "[ ]";
    const markStyled = selected.has(idx)
      ? style(mark, GREEN, BOLD, { enabled: useColor })
      : mark;
    const label = style(choice.name, idx === cursor ? BOLD : "", { enabled: useColor });
    const detail = style(choice.detail, DIM, { enabled: useColor });
    lines.push(` ${pointer} ${markStyled} ${label}  ${detail}`);
  }

  for (const line of lines) {
    out.write(`\x1b[2K${line}\n`);
  }
  return lines.length;
}

function interactiveArrowPick(choices, out, useColor, stdin) {
  return new Promise((resolve, reject) => {
    let cursor = 0;
    const selected = new Set();
    let rendered = renderMenu(choices, cursor, selected, out, useColor, 0);

    const prevRaw = stdin.isRaw;
    try {
      stdin.setRawMode(true);
    } catch (err) {
      reject(err);
      return;
    }
    stdin.resume();

    const cleanup = () => {
      stdin.off("data", onData);
      try {
        stdin.setRawMode(Boolean(prevRaw));
      } catch {
        // Ignore
      }
      stdin.pause();
    };

    const onData = (buf) => {
      const seq = buf.toString("utf8");
      let action = "";
      if (seq === "\r" || seq === "\n") {
        action = "ENTER";
      } else if (seq === " ") {
        action = "SPACE";
      } else if (seq === "\x03" || seq === "q" || seq === "Q" || seq === "\x1b") {
        action = "CANCEL";
      } else if (seq === "k" || seq === "\x1b[A") {
        action = "UP";
      } else if (seq === "j" || seq === "\x1b[B") {
        action = "DOWN";
      }

      if (action === "UP") {
        cursor = (cursor - 1 + choices.length) % choices.length;
      } else if (action === "DOWN") {
        cursor = (cursor + 1) % choices.length;
      } else if (action === "SPACE") {
        if (selected.has(cursor)) selected.delete(cursor);
        else selected.add(cursor);
      } else if (action === "ENTER") {
        const chosenIndices = selected.size > 0 ? [...selected].sort((a, b) => a - b) : [cursor];
        cleanup();
        resolve(chosenIndices.map((i) => choices[i].name));
        return;
      } else if (action === "CANCEL") {
        cleanup();
        resolve([]);
        return;
      } else {
        return;
      }

      rendered = renderMenu(choices, cursor, selected, out, useColor, rendered);
    };

    stdin.on("data", onData);
  });
}

async function readSingleLine(stdin) {
  return new Promise((resolve) => {
    const rl = createInterface({ input: stdin, crlfDelay: Infinity });
    let settled = false;
    rl.once("line", (line) => {
      settled = true;
      rl.close();
      resolve(line);
    });
    rl.once("close", () => {
      if (!settled) resolve("");
    });
  });
}

export async function numberedPick(choices, out, useColor, stdin = process.stdin) {
  out.write(style("Select task(s) or group(s) to run:\n", BOLD, CYAN, { enabled: useColor }));
  for (let idx = 0; idx < choices.length; idx += 1) {
    const choice = choices[idx];
    const num = String(idx + 1).padStart(2, " ");
    out.write(
      `  ${style(`${num}.`, BOLD, { enabled: useColor })} ` +
        `${style(choice.name, GREEN, { enabled: useColor })} ` +
        `${style(choice.detail, DIM, { enabled: useColor })}\n`
    );
  }
  out.write("Enter numbers or names separated by spaces/commas (empty to cancel): ");

  const raw = await readSingleLine(stdin);
  const tokens = raw
    .replaceAll(",", " ")
    .split(/\s+/)
    .map((t) => t.trim())
    .filter(Boolean);

  const picked = [];
  for (const tok of tokens) {
    if (/^\d+$/.test(tok)) {
      const idx = Number(tok);
      if (idx >= 1 && idx <= choices.length) {
        picked.push(choices[idx - 1].name);
      } else {
        throw new Error(`Selection out of range: ${tok}`);
      }
    } else {
      picked.push(tok);
    }
  }
  return picked;
}

export async function pickTargets(choices, out, useColor, stdin = process.stdin) {
  if (!choices || choices.length === 0) return [];
  const inStream = stdin || process.stdin;
  const isTty = Boolean(inStream && inStream.isTTY && out && out.isTTY && typeof inStream.setRawMode === "function");

  if (isTty) {
    try {
      return await interactiveArrowPick(choices, out, useColor, inStream);
    } catch {
      // Fall back to numbered prompt
    }
  }
  return numberedPick(choices, out, useColor, inStream);
}
