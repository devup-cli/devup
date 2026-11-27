import assert from "node:assert/strict";
import test from "node:test";

import { TomlParseError, parseToml } from "../src/toml.mjs";

test("parseToml handles tables, inline tables, arrays, and multiline strings", () => {
  const doc = parseToml(`
# Top comment
default = ["dev"]
fail_fast = true
shutdown_timeout = 7.5

[env]
LOG_LEVEL = "info"
ESCAPED = "line1\\nline2"
LITERAL = 'C:\\Users\\dev'

[groups]
dev = [
  "api", # inline comment
  "web",
]

[tasks.api]
command = "uvicorn app.main:app --port 8000"
env = { PORT = "8000", DEBUG = "1" }
port = 8000
notes = """
hello
world"""
`);

  assert.deepEqual(doc.default, ["dev"]);
  assert.equal(doc.fail_fast, true);
  assert.equal(doc.shutdown_timeout, 7.5);
  assert.equal(doc.env.LOG_LEVEL, "info");
  assert.equal(doc.env.ESCAPED, "line1\nline2");
  assert.equal(doc.env.LITERAL, "C:\\Users\\dev");
  assert.deepEqual(doc.groups.dev, ["api", "web"]);
  assert.equal(doc.tasks.api.port, 8000);
  assert.deepEqual(doc.tasks.api.env, { PORT: "8000", DEBUG: "1" });
  assert.equal(doc.tasks.api.notes, "hello\nworld");
});

test("parseToml throws TomlParseError on malformed syntax", () => {
  assert.throws(() => parseToml("[unclosed"), TomlParseError);
  assert.throws(() => parseToml("a = 1\na = 2"), TomlParseError);
});
