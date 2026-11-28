import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";

import { validateWorkspace } from "../src/diagnostics.mjs";
import { main } from "../src/cli.mjs";
import { discoverWorkspace, resolveTargets } from "../src/discovery.mjs";
import { runTasks } from "../src/runner.mjs";

class CaptureStream {
  constructor() {
    this.chunks = [];
    this.isTTY = false;
  }
  write(chunk) {
    this.chunks.push(String(chunk));
    return true;
  }
  text() {
    return this.chunks.join("");
  }
}

test("Node discovery supports Cargo.toml, go.mod/go.work, Taskfile.yml, and mise.toml", () => {
  const root = mkdtempSync(join(tmpdir(), "devup-npm-p3disc-"));
  try {
    writeFileSync(
      join(root, "Cargo.toml"),
      `[workspace]\nmembers = ["crates/*"]\n[package]\nname = "app"\n[[bin]]\nname = "cli"\n`,
      "utf8"
    );
    mkdirSync(join(root, "crates", "job"), { recursive: true });
    writeFileSync(join(root, "crates", "job", "Cargo.toml"), '[package]\nname = "job"\n', "utf8");

    writeFileSync(join(root, "go.work"), 'go 1.22\nuse "./svc"\n', "utf8");
    writeFileSync(join(root, "go.mod"), "module example.com/root\n", "utf8");
    mkdirSync(join(root, "cmd", "api"), { recursive: true });
    writeFileSync(join(root, "cmd", "api", "main.go"), "package main\n", "utf8");
    mkdirSync(join(root, "svc"), { recursive: true });
    writeFileSync(join(root, "svc", "go.mod"), "module example.com/svc\n", "utf8");

    writeFileSync(
      join(root, "Taskfile.yml"),
      `version: '3'\ntasks:\n  tdev:\n    desc: Task dev\n  hidden:\n    internal: true\n`,
      "utf8"
    );
    writeFileSync(
      join(root, "mise.toml"),
      `[tasks.mdev]\ndescription = "Mise dev"\nrun = "echo hi"\n[tasks.secret]\nhide = true\nrun = "no"\n`,
      "utf8"
    );

    const cfg = discoverWorkspace(root);
    assert.equal(cfg.tasks["run:cli"].command, "cargo run --bin cli");
    assert.equal(cfg.tasks["job:test"].command, "cargo test");
    assert.equal(cfg.tasks["run:api"].command, "go run ./cmd/api");
    assert.equal(cfg.tasks["svc:build"].command, "go build ./...");
    assert.equal(cfg.tasks.tdev.command, "task tdev");
    assert.equal(cfg.tasks.mdev.command, "mise run mdev");
    assert.equal("hidden" in cfg.tasks, false);
    assert.equal("secret" in cfg.tasks, false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("Node CLI supports --only, --exclude, --buffer group, --save-logs, -v, and missing executable hints", async () => {
  const root = mkdtempSync(join(tmpdir(), "devup-npm-p3run-"));
  try {
    const logPath = join(root, "out", "run.log");
    writeFileSync(
      join(root, "devup.toml"),
      `
[tasks.a]
command = "node -e \\"console.log('ALPHA_1'); console.log('ALPHA_2')\\""

[tasks.b]
command = "node -e \\"console.log('BETA_NOISE')\\""

[tasks.missing]
command = "nonexistent_cmd_999 --help"
`,
      "utf8"
    );

    const out = new CaptureStream();
    const rc = await main(
      ["-C", root, "--no-color", "--only", "a", "--buffer", "group", "--save-logs", logPath, "-v", "a", "b"],
      { stdout: out }
    );
    assert.equal(rc, 0);
    const text = out.text();
    assert.match(text, /ALPHA_1/);
    assert.match(text, /ALPHA_2/);
    assert.equal(text.includes("BETA_NOISE"), false);
    assert.match(text, /\[debug\]/);
    assert.equal(existsSync(logPath), true);
    const saved = readFileSync(logPath, "utf8");
    assert.match(saved, /BETA_NOISE/);

    const exclOut = new CaptureStream();
    await main(["-C", root, "--no-color", "--exclude", "b", "a", "b"], { stdout: exclOut });
    assert.match(exclOut.text(), /ALPHA_1/);
    assert.equal(exclOut.text().includes("BETA_NOISE"), false);

    const cfg = discoverWorkspace(root);
    const failOut = new CaptureStream();
    const failRes = await runTasks(cfg, ["missing"], { color: false, portCheck: false }, failOut);
    assert.notEqual(failRes[0].exitCode, 0);
    assert.match(failOut.text(), /nonexistent_cmd_999/);
    assert.match(failOut.text(), /devup doctor/);
  } finally {
    rmSync(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  }
});

test("Node runner enforces env precedence, dependency skipping, and AbortController shutdown", async () => {
  const root = mkdtempSync(join(tmpdir(), "devup-npm-p3int-"));
  const prevShell = process.env.DEVUP_NODE_SHELL_WINS;
  process.env.DEVUP_NODE_SHELL_WINS = "shell_kept";
  try {
    writeFileSync(join(root, ".env"), "DEVUP_NODE_SHELL_WINS=dotenv_lost\nROOT_DOT=root_ok\n", "utf8");
    writeFileSync(join(root, ".env.svc"), "SVC_DOT=svc_ok\nROOT_DOT=overridden_by_task_file\n", "utf8");
    writeFileSync(
      join(root, "devup.toml"),
      `
[env]
GLOBAL_VAR = "global_ok"

[tasks.envprobe]
command = "node -e \\"console.log('P=' + [process.env.DEVUP_NODE_SHELL_WINS, process.env.GLOBAL_VAR, process.env.SVC_DOT, process.env.TASK_VAR].join('|'))\\""
env_file = ".env.svc"

[tasks.envprobe.env]
TASK_VAR = "task_ok"

[tasks.up_fail]
command = "node -e \\"process.exit(4)\\""

[tasks.down_skip]
command = "node -e \\"console.log('NEVER_RUN')\\""
depends_on = ["up_fail"]

[tasks.sleeper]
command = "node -e \\"console.log('SLEEPING'); setTimeout(() => {}, 15000)\\""
`,
      "utf8",
    );

    const cfg = discoverWorkspace(root);
    const val = validateWorkspace(root);
    assert.equal(val.valid, true);

    const envOut = new CaptureStream();
    const envRes = await runTasks(cfg, resolveTargets(cfg, ["envprobe", "up_fail", "down_skip"]), {
      color: false,
      portCheck: false,
      failFast: false,
    }, envOut);
    const byName = Object.fromEntries(envRes.map((r) => [r.name, r]));
    assert.equal(byName.envprobe.status, "ok");
    assert.equal(byName.up_fail.status, "failed");
    assert.equal(byName.down_skip.status, "skipped");
    assert.match(envOut.text(), /P=shell_kept\|global_ok\|svc_ok\|task_ok/);
    assert.equal(envOut.text().includes("NEVER_RUN"), false);

    const ac = new AbortController();
    const intOut = new CaptureStream();
    const runPromise = runTasks(cfg, ["sleeper"], { color: false, portCheck: false, shutdownTimeout: 0.5 }, intOut, ac.signal);
    await delay(250);
    ac.abort();
    const intRes = await runPromise;
    assert.equal(intRes[0].status, "stopped");
  } finally {
    if (prevShell === undefined) delete process.env.DEVUP_NODE_SHELL_WINS;
    else process.env.DEVUP_NODE_SHELL_WINS = prevShell;
    rmSync(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  }
});
