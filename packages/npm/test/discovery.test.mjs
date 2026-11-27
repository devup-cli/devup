import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { discoverWorkspace, resolveTargets } from "../src/discovery.mjs";

function makeTempDir() {
  return mkdtempSync(join(tmpdir(), "devup-npm-disc-"));
}

test("package.json lockfile detection and workspaces", () => {
  const root = makeTempDir();
  try {
    writeFileSync(join(root, "pnpm-lock.yaml"), "", "utf8");
    writeFileSync(
      join(root, "package.json"),
      JSON.stringify({
        name: "root",
        scripts: { dev: "vite", lint: "eslint ." },
        workspaces: ["apps/*"],
      }),
      "utf8"
    );
    mkdirSync(join(root, "apps", "api"), { recursive: true });
    writeFileSync(
      join(root, "apps", "api", "package.json"),
      JSON.stringify({ scripts: { start: "node server.js" } }),
      "utf8"
    );

    const cfg = discoverWorkspace(root);
    assert.equal(cfg.tasks.dev.command, "pnpm run dev");
    assert.equal(cfg.tasks["api:start"].command, "pnpm run start");
    assert.equal(cfg.tasks["api:start"].workspace, "api");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("pyproject, makefile, justfile, and compose discovery with collision qualification", () => {
  const root = makeTempDir();
  try {
    writeFileSync(
      join(root, "package.json"),
      JSON.stringify({ scripts: { test: "vitest" } }),
      "utf8"
    );
    writeFileSync(
      join(root, "pyproject.toml"),
      `
[tool.poe.tasks]
test = "pytest"
fmt = { cmd = "ruff format .", help = "Format python code" }

[tool.poetry.scripts]
serve = "app:main"
`,
      "utf8"
    );
    writeFileSync(
      join(root, "Makefile"),
      `# Build project
build:
\techo build
`,
      "utf8"
    );
    writeFileSync(
      join(root, "justfile"),
      `check: # Run just checks
\techo ok
`,
      "utf8"
    );
    writeFileSync(
      join(root, "docker-compose.yml"),
      `services:
  db:
    image: postgres:16
  redis:
    image: redis:7
`,
      "utf8"
    );

    const cfg = discoverWorkspace(root);
    assert.equal(cfg.tasks.test.source, "package.json");
    assert.equal(cfg.tasks["py:test"].command, "poe test");
    assert.equal(cfg.tasks.fmt.description, "Format python code");
    assert.equal(cfg.tasks.serve.command, "poetry run serve");
    assert.equal(cfg.tasks.build.command, "make build");
    assert.equal(cfg.tasks.check.command, "just check");
    assert.ok("compose:up" in cfg.tasks);
    assert.ok("compose:db" in cfg.tasks);
    assert.ok("compose:redis" in cfg.tasks);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("maven pom.xml and gradle settings discovery", () => {
  const root = makeTempDir();
  try {
    const mvnDir = join(root, "mvn-app");
    mkdirSync(join(mvnDir, "api"), { recursive: true });
    writeFileSync(join(mvnDir, process.platform === "win32" ? "mvnw.cmd" : "mvnw"), "", "utf8");
    writeFileSync(
      join(mvnDir, "pom.xml"),
      `<project>
  <modules><module>api</module></modules>
  <build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>
</project>`,
      "utf8"
    );
    writeFileSync(
      join(mvnDir, "api", "pom.xml"),
      `<project><artifactId>api</artifactId></project>`,
      "utf8"
    );

    const mvnCfg = discoverWorkspace(mvnDir);
    assert.ok(mvnCfg.tasks.dev.command.includes("mvnw"));
    assert.ok(mvnCfg.tasks.dev.command.includes("spring-boot:run"));
    assert.ok("api:compile" in mvnCfg.tasks);

    const gradDir = join(root, "grad-app");
    mkdirSync(join(gradDir, "worker"), { recursive: true });
    writeFileSync(join(gradDir, "settings.gradle.kts"), 'include(":worker")', "utf8");
    writeFileSync(
      join(gradDir, "build.gradle.kts"),
      `plugins { id("org.springframework.boot") version "3.3.0" }
tasks.register("hello") {}
`,
      "utf8"
    );
    writeFileSync(join(gradDir, "worker", "build.gradle.kts"), "", "utf8");

    const gradCfg = discoverWorkspace(gradDir);
    assert.ok("bootRun" in gradCfg.tasks);
    assert.ok("hello" in gradCfg.tasks);
    assert.ok("worker:build" in gradCfg.tasks);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("devup.toml overrides and close-match target suggestions", () => {
  const root = makeTempDir();
  try {
    writeFileSync(
      join(root, "devup.toml"),
      `
default = ["dev"]
[groups]
dev = ["api", "web"]
[tasks]
api = "node -e 'process.exit(0)'"
web = "node -e 'process.exit(0)'"
`,
      "utf8"
    );
    const cfg = discoverWorkspace(root);
    assert.deepEqual(resolveTargets(cfg, ["dev"]), ["api", "web"]);
    assert.throws(
      () => resolveTargets(cfg, ["ap"]),
      (err) => err.message.includes("Did you mean: api?")
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
