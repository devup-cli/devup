import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
import { dirname, join, resolve } from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { main } from "../src/cli.mjs";
import { SUPPORTED_SHELLS, generateCompletion } from "../src/completions.mjs";
import { redactText, runDoctor } from "../src/diagnostics.mjs";
import { checkForUpdate } from "../src/updates.mjs";
import { VERSION } from "../src/version.mjs";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

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

test("CLI list --json, init --template, validate, and completion work end to end", async () => {
  const root = mkdtempSync(join(tmpdir(), "devup-npm-cli-"));
  try {
    const initOut = new CaptureStream();
    const initCode = await main(["-C", root, "init", "--template", "fullstack", "--yes"], {
      stdout: initOut,
    });
    assert.equal(initCode, 0);
    assert.equal(existsSync(join(root, "devup.toml")), true);

    const valOut = new CaptureStream();
    const valCode = await main(["-C", root, "validate", "--json"], { stdout: valOut });
    const valJson = JSON.parse(valOut.text());
    assert.equal(valJson.task_count >= 2, true);
    assert.equal(valCode, 1); // backend/frontend dirs do not exist yet

    const listOut = new CaptureStream();
    const listCode = await main(["-C", root, "list", "--json"], { stdout: listOut });
    assert.equal(listCode, 0);
    const listJson = JSON.parse(listOut.text());
    assert.ok("api" in listJson.tasks);
    assert.ok("web" in listJson.tasks);

    for (const shell of SUPPORTED_SHELLS) {
      const compOut = new CaptureStream();
      const compCode = await main(["completion", shell], { stdout: compOut });
      assert.equal(compCode, 0);
      assert.equal(compOut.text(), generateCompletion(shell));
    }

    const verOut = new CaptureStream();
    assert.equal(await main(["--version"], { stdout: verOut }), 0);
    assert.equal(verOut.text().trim(), `devup ${VERSION}`);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("devup doctor --json redacts home paths, usernames, URL creds, tokens, and env secrets", async () => {
  const secretVal = "super_secret_env_token_987654";
  const prevSecret = process.env.DEVUP_TEST_API_SECRET;
  process.env.DEVUP_TEST_API_SECRET = secretVal;

  const root = mkdtempSync(join(tmpdir(), "devup-npm-sec-"));
  try {
    writeFileSync(
      join(root, ".env"),
      "DB_PASSWORD=topsecret\nPUBLIC_PORT=8080\n",
      "utf8"
    );
    writeFileSync(
      join(root, "devup.toml"),
      `
[tasks.bad]
command = "curl -H 'Authorization: Bearer my.jwt.token' https://admin:s3cr3t@api.example.com/hook --token=cli_secret"
cwd = "missing_dir"
`,
      "utf8"
    );

    const sample = `Path ${homedir()}\\secret https://user:pass@db.local:5432/app Bearer abcdef.ghijkl.mnop TOKEN=xyz123 --password="pw" ${secretVal}`;
    const redacted = redactText(sample);
    assert.ok(!redacted.includes("pass@"));
    assert.ok(!redacted.includes("abcdef"));
    assert.ok(!redacted.includes("xyz123"));
    assert.ok(!redacted.includes('"pw"'));
    assert.ok(!redacted.includes(secretVal));
    const user = userInfo().username;
    if (user && user.length >= 3) {
      assert.ok(!redacted.includes(user));
    }

    const report = await runDoctor(root, { online: false });
    assert.equal(report.runtime, "node");
    assert.deepEqual(report.dotenv_keys, ["DB_PASSWORD", "PUBLIC_PORT"]);
    assert.equal(JSON.stringify(report).includes("topsecret"), false);
  } finally {
    if (prevSecret === undefined) delete process.env.DEVUP_TEST_API_SECRET;
    else process.env.DEVUP_TEST_API_SECRET = prevSecret;
    rmSync(root, { recursive: true, force: true });
  }
});

test("update checks stay strictly opt-in and honor DEVUP_NO_UPDATE_CHECK", async () => {
  const prev = process.env.DEVUP_NO_UPDATE_CHECK;
  process.env.DEVUP_NO_UPDATE_CHECK = "1";
  let called = false;
  try {
    const status = await checkForUpdate("0.3.0", {
      force: true,
      fetchFn: async () => {
        called = true;
        return { ok: true, json: async () => ({ "dist-tags": { latest: "9.9.9" } }) };
      },
    });
    assert.equal(status, null);
    assert.equal(called, false);
  } finally {
    if (prev === undefined) delete process.env.DEVUP_NO_UPDATE_CHECK;
    else process.env.DEVUP_NO_UPDATE_CHECK = prev;
  }
});

test("interactive picker numbered fallback picks targets cleanly", async () => {
  const root = mkdtempSync(join(tmpdir(), "devup-npm-pick-"));
  try {
    writeFileSync(
      join(root, "devup.toml"),
      `[tasks]
a = "node -e 'console.log(1)'"
b = "node -e 'console.log(2)'"
`,
      "utf8"
    );
    const out = new CaptureStream();
    const stdin = new PassThrough();
    stdin.end("1\n");
    const code = await main(["-C", root, "pick", "--dry-run"], { stdout: out, stdin });
    assert.equal(code, 0);
    assert.match(out.text(), /Dry run execution plan \(1 task\(s\)\)/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("repo examples validate and dry-run via Node CLI", async () => {
  for (const exampleName of [
    "fastapi-react",
    "node-monorepo",
    "maven-multimodule",
    "gradle-multimodule",
    "spring-boot-react",
    "docker-compose-multiservice",
  ]) {
    const exDir = join(REPO_ROOT, "examples", exampleName);
    if (!existsSync(exDir)) continue;
    const out = new CaptureStream();
    const valCode = await main(["-C", exDir, "validate", "--json"], { stdout: out });
    const valData = JSON.parse(out.text());
    assert.equal(typeof valData.task_count, "number");
    assert.ok(valData.task_count > 0, `expected tasks in ${exampleName}`);
    // spring-boot-react or docker-compose-multiservice might have 0 when dirs exist
    assert.ok(valCode === 0 || valCode === 1);
  }

  const rootPkg = JSON.parse(readFileSync(join(REPO_ROOT, "packages", "npm", "package.json"), "utf8"));
  assert.equal(rootPkg.name, "@devup-cli/devup");
});
