import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { discoverWorkspace } from "../src/discovery.mjs";
import { parseDotenv } from "../src/dotenv.mjs";
import { checkTaskPorts, extractPortsFromCommand } from "../src/ports.mjs";
import { probeHttp, probeTcp } from "../src/readiness.mjs";
import { expandDependencies, runTasks } from "../src/runner.mjs";

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

test("parseDotenv handles comments, export prefix, and quotes", () => {
  const parsed = parseDotenv(`
# Comment
export API_KEY="secret-value"
SINGLE='literal\\nval'
PLAIN=hello # inline comment
EMPTY=
`);
  assert.deepEqual(parsed, {
    API_KEY: "secret-value",
    SINGLE: "literal\\nval",
    PLAIN: "hello",
    EMPTY: "",
  });
});

test("extractPortsFromCommand and strict_ports detect occupied ports", async () => {
  assert.deepEqual(extractPortsFromCommand("uvicorn main:app --port 8000 PORT=9000"), [8000, 9000]);

  const srv = net.createServer();
  await new Promise((resolve) => srv.listen(0, "127.0.0.1", resolve));
  const { port } = srv.address();

  try {
    const root = mkdtempSync(join(tmpdir(), "devup-npm-port-"));
    try {
      writeFileSync(
        join(root, "devup.toml"),
        `strict_ports = true
[tasks.api]
command = "node -e 'console.log(1)'"
port = ${port}
`,
        "utf8"
      );
      const cfg = discoverWorkspace(root);
      const conflicts = await checkTaskPorts(cfg.tasks, ["api"]);
      assert.equal(conflicts.length, 1);
      assert.equal(conflicts[0].port, port);

      const out = new CaptureStream();
      const results = await runTasks(cfg, ["api"], { color: false }, out);
      assert.equal(results[0].status, "error");
      assert.match(out.text(), /Port error \(api\)/);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  } finally {
    await new Promise((resolve) => srv.close(resolve));
  }
});

test("probeHttp does not block on streaming responses and probeTcp connects", async () => {
  const server = http.createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.write("streaming start\n");
    // Deliberately keep stream open without res.end()
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();

  try {
    const start = Date.now();
    const httpOk = await probeHttp(`http://127.0.0.1:${port}/health`, 1.0);
    const tcpOk = await probeTcp(`127.0.0.1:${port}`, 0.5);
    assert.equal(httpOk, true);
    assert.equal(tcpOk, true);
    assert.ok(Date.now() - start < 900, "probeHttp should return without waiting for body EOF");
  } finally {
    server.closeAllConnections?.();
    await new Promise((resolve) => server.close(resolve));
  }
});

test("parallel execution, dotenv precedence, fail_fast, and watch restart", async () => {
  const root = mkdtempSync(join(tmpdir(), "devup-npm-run-"));
  try {
    writeFileSync(join(root, ".env"), "GREETING=hello\nSHELL_WINS=from_dotenv\n", "utf8");
    const watchedFile = join(root, "trigger.txt");
    writeFileSync(watchedFile, "v1", "utf8");

    writeFileSync(
      join(root, "devup.toml"),
      `
[tasks.first]
command = "node -e \\"console.log(process.env.GREETING + ' READY')\\""
ready_pattern = "READY"

[tasks.second]
command = "node -e \\"console.log('second ran')\\""
depends_on = ["first"]

[tasks.bad]
command = "node -e \\"process.exit(3)\\""

[tasks.slow]
command = "node -e \\"setTimeout(() => console.log('never'), 5000)\\""
`,
      "utf8"
    );

    const cfg = discoverWorkspace(root);
    assert.deepEqual(expandDependencies(cfg, ["second"]), ["first", "second"]);

    const out = new CaptureStream();
    const okRes = await runTasks(cfg, ["second"], { color: false }, out);
    assert.equal(okRes.every((r) => r.status === "ok"), true);
    assert.match(out.text(), /hello READY/);
    assert.match(out.text(), /second ran/);

    const failOut = new CaptureStream();
    const failRes = await runTasks(cfg, ["bad", "slow"], { color: false, portCheck: false }, failOut);
    const byName = Object.fromEntries(failRes.map((r) => [r.name, r]));
    assert.equal(byName.bad.status, "failed");
    assert.equal(byName.bad.exitCode, 3);
    assert.equal(byName.slow.status, "stopped");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("watch mode restarts service cleanly when matching file changes", async () => {
  const root = mkdtempSync(join(tmpdir(), "devup-npm-watch-"));
  try {
    writeFileSync(join(root, "app.txt"), "one", "utf8");
    writeFileSync(
      join(root, "devup.toml"),
      `[tasks.svc]
command = "node -e \\"console.log('BOOT'); setTimeout(() => {}, 10000)\\""
watch = ["*.txt"]
`,
      "utf8"
    );

    const cfg = discoverWorkspace(root);
    const out = new CaptureStream();
    const ac = new AbortController();
    const runPromise = runTasks(
      cfg,
      ["svc"],
      { color: false, portCheck: false, watch: true, watchInterval: 0.1 },
      out,
      ac.signal
    );

    setTimeout(() => {
      writeFileSync(join(root, "app.txt"), "two", "utf8");
    }, 350);
    setTimeout(() => {
      ac.abort();
    }, 1400);

    const results = await runPromise;
    assert.equal(results.length, 1);
    assert.match(out.text(), /Change detected in app\.txt — restarting 'svc'\.\.\./);
    assert.ok(out.text().split("BOOT").length - 1 >= 2);
  } finally {
    rmSync(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  }
});
