import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";

import { BOLD, CYAN, DIM, GREEN, RED, YELLOW, style, supportsColor } from "./colors.mjs";
import { SUPPORTED_SHELLS, generateCompletion } from "./completions.mjs";
import { runDoctor, validateWorkspace } from "./diagnostics.mjs";
import { discoverWorkspace, resolveTargets } from "./discovery.mjs";
import { pathExists } from "./fs_utils.mjs";
import { pickTargets } from "./picker.mjs";
import { runTasks } from "./runner.mjs";
import { checkForUpdate } from "./updates.mjs";
import { VERSION } from "./version.mjs";

const SUBCOMMANDS = new Set(["list", "ls", "run", "pick", "init", "validate", "doctor", "completion"]);

const INIT_TEMPLATES = {
  fullstack: `default = ["dev"]
fail_fast = true
strict_ports = false
auto_env = true
shutdown_timeout = 5.0

[groups]
dev = ["api", "web"]

[tasks.api]
command = "uvicorn app.main:app --reload --port 8000"
cwd = "backend"
port = 8000
ready_url = "http://127.0.0.1:8000/health"
watch = ["app/**/*.py"]

[tasks.web]
command = "npm run dev"
cwd = "frontend"
port = 5173
depends_on = ["api"]
`,
  "python-node": `default = ["dev"]
fail_fast = true
auto_env = true

[groups]
dev = ["api", "web"]

[tasks.api]
command = "uvicorn main:app --reload --port 8000"
cwd = "backend"
port = 8000
ready_url = "http://127.0.0.1:8000/health"

[tasks.web]
command = "npm run dev"
cwd = "frontend"
port = 5173
depends_on = ["api"]
`,
  "java-microservices": `default = ["dev"]
fail_fast = true
strict_ports = true

[groups]
dev = ["infra", "api"]

[tasks.infra]
command = "docker compose up postgres"
port = 5432
ready_tcp = "127.0.0.1:5432"
ready_timeout = 30

[tasks.api]
command = "mvn spring-boot:run"
cwd = "service"
port = 8080
depends_on = ["infra"]
ready_url = "http://127.0.0.1:8080/actuator/health"
ready_timeout = 45
`,
  "docker-compose": `default = ["stack"]
fail_fast = true

[groups]
stack = ["db", "cache", "app"]

[tasks.db]
command = "docker compose up postgres"
port = 5432
ready_tcp = "127.0.0.1:5432"

[tasks.cache]
command = "docker compose up redis"
port = 6379
ready_tcp = "127.0.0.1:6379"

[tasks.app]
command = "docker compose up app"
port = 8000
depends_on = ["db", "cache"]
ready_url = "http://127.0.0.1:8000/health"
`,
};

const TEMPLATE_CHOICES = ["auto", ...Object.keys(INIT_TEMPLATES)];

function helpText() {
  return `usage: devup [-h] [-V] [-C CWD] [--no-workspaces] [-t] [--no-prefix]
             [--keep-going] [--dry-run] [-w] [--env-file ENV_FILE] [--no-dotenv]
             [--no-port-check] [--strict-ports] [--color | --no-color]
             [--json] [--force] [--template {auto,fullstack,python-node,java-microservices,docker-compose}]
             [--interactive] [--yes] [--online] [--check-updates] [--no-update-check]
             [targets ...]

Discover scripts across package.json, pyproject.toml, pom.xml, build.gradle,
Makefile, Justfile, Docker Compose, and devup.toml, and run services in parallel.

positional arguments:
  targets               Subcommand (list, run, pick, init, validate, doctor, completion)
                        or task/group names to run in parallel.

options:
  -h, --help            show this help message and exit
  -V, --version         show program's version number and exit
  -C, --cwd CWD         Project root directory to scan (default: current directory).
  --no-workspaces       Disable automatic workspace/monorepo subdirectory scanning.
  -t, --timestamps      Prepend timestamps to each log line.
  --no-prefix           Do not prepend [service] prefixes to log lines.
  --keep-going          Keep other services running if one exits with a non-zero status.
  --dry-run             Show the resolved execution plan without starting processes.
  -w, --watch           Watch files under each task's working directory and auto-restart on changes.
  --env-file ENV_FILE   Load additional .env file(s) before starting tasks (can be repeated).
  --no-dotenv           Disable automatic loading of .env in the project root.
  --no-port-check       Skip pre-flight port availability checks.
  --strict-ports        Abort startup if any declared or inferred port is already in use.
  --color               Force ANSI color output.
  --no-color            Disable ANSI color output.
  --json                Output JSON (used with list, validate, doctor).
  --force               Overwrite an existing devup.toml (used with init).
  --template TEMPLATE   Preset template for devup init.
  --interactive         Prompt for configuration options during devup init.
  --yes                 Skip interactive prompts during devup init.
  --online              Query registry during devup doctor to check for newer releases.
  --check-updates       Check npm registry (24h cached) for a newer release.
  --no-update-check     Disable update availability notifications.
`;
}

export function parseCliArgs(argv) {
  const args = {
    help: false,
    version: false,
    cwd: process.cwd(),
    noWorkspaces: false,
    timestamps: false,
    noPrefix: false,
    keepGoing: false,
    dryRun: false,
    watch: false,
    envFile: [],
    noDotenv: false,
    noPortCheck: false,
    strictPorts: false,
    color: false,
    noColor: false,
    json: false,
    force: false,
    template: "auto",
    interactive: false,
    yes: false,
    online: false,
    checkUpdates: false,
    noUpdateCheck: false,
    targets: [],
  };

  let stopFlags = false;
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (stopFlags) {
      args.targets.push(token);
      continue;
    }
    if (token === "--") {
      stopFlags = true;
      continue;
    }
    if (!token.startsWith("-") || token === "-") {
      args.targets.push(token);
      continue;
    }

    const eqIdx = token.indexOf("=");
    const flag = eqIdx >= 0 ? token.slice(0, eqIdx) : token;
    const inlineVal = eqIdx >= 0 ? token.slice(eqIdx + 1) : null;

    const readValue = (name) => {
      if (inlineVal !== null) return inlineVal;
      if (i + 1 >= argv.length) {
        throw new Error(`option ${name} requires an argument`);
      }
      i += 1;
      return argv[i];
    };

    switch (flag) {
      case "-h":
      case "--help":
        args.help = true;
        break;
      case "-V":
      case "--version":
        args.version = true;
        break;
      case "-C":
      case "--cwd":
        args.cwd = readValue(flag);
        break;
      case "--no-workspaces":
        args.noWorkspaces = true;
        break;
      case "-t":
      case "--timestamps":
        args.timestamps = true;
        break;
      case "--no-prefix":
        args.noPrefix = true;
        break;
      case "--keep-going":
        args.keepGoing = true;
        break;
      case "--dry-run":
        args.dryRun = true;
        break;
      case "-w":
      case "--watch":
        args.watch = true;
        break;
      case "--env-file":
        args.envFile.push(readValue(flag));
        break;
      case "--no-dotenv":
        args.noDotenv = true;
        break;
      case "--no-port-check":
        args.noPortCheck = true;
        break;
      case "--strict-ports":
        args.strictPorts = true;
        break;
      case "--color":
        args.color = true;
        break;
      case "--no-color":
        args.noColor = true;
        break;
      case "--json":
        args.json = true;
        break;
      case "--force":
        args.force = true;
        break;
      case "--template": {
        const val = readValue(flag);
        if (!TEMPLATE_CHOICES.includes(val)) {
          throw new Error(
            `invalid choice '${val}' for --template (choose from ${TEMPLATE_CHOICES.join(", ")})`
          );
        }
        args.template = val;
        break;
      }
      case "--interactive":
        args.interactive = true;
        break;
      case "--yes":
        args.yes = true;
        break;
      case "--online":
        args.online = true;
        break;
      case "--check-updates":
        args.checkUpdates = true;
        break;
      case "--no-update-check":
        args.noUpdateCheck = true;
        break;
      default:
        if (flag.startsWith("-C") && flag.length > 2 && inlineVal === null) {
          args.cwd = flag.slice(2);
        } else {
          throw new Error(`unrecognized argument: ${token}`);
        }
    }
  }

  if (args.color && args.noColor) {
    throw new Error("argument --no-color: not allowed with argument --color");
  }

  return args;
}

function resolveColorFlag(args) {
  if (args.color) return true;
  if (args.noColor) return false;
  return null;
}

function printList(config, out, useColor, asJson) {
  if (asJson) {
    const payload = {
      root: String(config.root),
      default: config.defaultTargets,
      groups: config.groups,
      tasks: Object.fromEntries(
        Object.entries(config.tasks).map(([name, task]) => [
          name,
          {
            command: task.command,
            source: task.source,
            runner: task.runner,
            description: task.description,
            cwd: String(task.cwd || config.root),
            depends_on: task.dependsOn,
            ready_pattern: task.readyPattern,
            ready_url: task.readyUrl,
            ready_tcp: task.readyTcp,
          },
        ])
      ),
      warnings: config.warnings,
    };
    out.write(`${JSON.stringify(payload, null, 2)}\n`);
    return 0;
  }

  for (const warning of config.warnings) {
    out.write(style(`warning: ${warning}\n`, YELLOW, { enabled: useColor }));
  }

  const taskKeys = Object.keys(config.tasks);
  const groupKeys = Object.keys(config.groups);

  if (taskKeys.length === 0 && groupKeys.length === 0) {
    out.write(
      `No tasks discovered in ${config.root}.\n` +
        "Tip: run `devup init` to create a starter devup.toml.\n"
    );
    return 0;
  }

  if (groupKeys.length > 0) {
    out.write(style("Groups:\n", BOLD, CYAN, { enabled: useColor }));
    const groupPad = Math.max(...groupKeys.map((g) => g.length));
    for (const [groupName, members] of Object.entries(config.groups)) {
      const defaultMark =
        config.defaultTargets.length === 1 && config.defaultTargets[0] === groupName
          ? " (default)"
          : "";
      out.write(
        `  ${style(groupName.padEnd(groupPad, " "), BOLD, GREEN, { enabled: useColor })}` +
          `  -> ${members.join(", ")}${style(defaultMark, DIM, { enabled: useColor })}\n`
      );
    }
    out.write("\n");
  }

  if (taskKeys.length > 0) {
    out.write(style(`Tasks in ${config.root}:\n`, BOLD, CYAN, { enabled: useColor }));
    const nameW = Math.min(28, Math.max(...taskKeys.map((n) => n.length)));
    const srcW = Math.min(16, Math.max(...Object.values(config.tasks).map((t) => t.source.length)));
    const cmdW = Math.min(38, Math.max(...Object.values(config.tasks).map((t) => t.command.length)));
    for (const [name, task] of Object.entries(config.tasks)) {
      const desc = task.description ? `  ${style(task.description, DIM, { enabled: useColor })}` : "";
      const cmdDisplay =
        task.command.length <= 38 ? task.command : `${task.command.slice(0, 35)}...`;
      out.write(
        `  ${style(name.padEnd(nameW, " "), BOLD, { enabled: useColor })}  ` +
          `${style(task.source.padEnd(srcW, " "), CYAN, { enabled: useColor })}  ` +
          `${cmdDisplay.padEnd(cmdW, " ")}${desc}\n`
      );
    }
  }

  out.write(
    `\n${style("Run multiple services: ", DIM, { enabled: useColor })}devup run <task1> <task2>\n`
  );
  return 0;
}

async function interactivePick(config, out, useColor, stdin) {
  const choices = [];
  for (const [gname, members] of Object.entries(config.groups)) {
    choices.push({ name: gname, detail: `group: ${members.join(", ")}` });
  }
  for (const [tname, task] of Object.entries(config.tasks)) {
    choices.push({ name: tname, detail: `${task.command} (${task.source})` });
  }
  return pickTargets(choices, out, useColor, stdin);
}

async function promptLine(out, rl, prompt, defaultVal = "") {
  out.write(prompt);
  const answer = await new Promise((resolve) => {
    rl.once("line", (line) => resolve(line));
    rl.once("close", () => resolve(""));
  });
  const stripped = String(answer).trim();
  return stripped || defaultVal;
}

async function initDevupToml(
  config,
  force,
  out,
  useColor,
  { template = "auto", interactive = false, yes = false, stdin = process.stdin } = {}
) {
  const targetPath = join(config.root, "devup.toml");
  if (pathExists(targetPath) && !force) {
    out.write(
      style(`${targetPath} already exists. Pass --force to overwrite.\n`, RED, { enabled: useColor })
    );
    return 1;
  }

  const inStream = stdin || process.stdin;
  const isTty = Boolean(inStream && inStream.isTTY && out && out.isTTY);
  const shouldPrompt = interactive || (isTty && !yes && template === "auto");

  let chosenTemplate = template;
  const sampleTasks = Object.keys(config.tasks).slice(0, 2);
  let defaultGroup = "dev" in config.tasks ? "stack" : "dev";
  let strictPorts = false;
  let failFast = true;

  let rl = null;
  try {
    if (shouldPrompt) {
      rl = createInterface({ input: inStream, crlfDelay: Infinity });
      chosenTemplate = await promptLine(
        out,
        rl,
        `Template (${TEMPLATE_CHOICES.join(", ")}) [${template}]: `,
        template
      );
      if (!TEMPLATE_CHOICES.includes(chosenTemplate)) {
        out.write(
          style(`Unknown template '${chosenTemplate}'. Falling back to 'auto'.\n`, YELLOW, {
            enabled: useColor,
          })
        );
        chosenTemplate = "auto";
      }
    }

    if (chosenTemplate in INIT_TEMPLATES) {
      writeFileSync(targetPath, INIT_TEMPLATES[chosenTemplate], "utf8");
      out.write(
        style(`Created ${targetPath} from '${chosenTemplate}' template.\n`, GREEN, {
          enabled: useColor,
        })
      );
      return 0;
    }

    if (shouldPrompt) {
      defaultGroup = await promptLine(out, rl, `Primary group name [${defaultGroup}]: `, defaultGroup);
      const strictAns = (
        await promptLine(out, rl, "Abort startup if any port is busy (strict_ports)? [y/N]: ", "n")
      ).toLowerCase();
      strictPorts = strictAns === "y" || strictAns === "yes";
      const ffAns = (
        await promptLine(out, rl, "Stop sibling services on crash (fail_fast)? [Y/n]: ", "y")
      ).toLowerCase();
      failFast = ffAns !== "n" && ffAns !== "no";
    }
  } finally {
    if (rl) rl.close();
  }

  const groupLine =
    sampleTasks.length > 0
      ? `${defaultGroup} = ${JSON.stringify(sampleTasks)}`
      : `${defaultGroup} = ["api", "web"]`;

  const lines = [
    "# devup.toml — project-specific service groups, dependencies, and overrides",
    sampleTasks.length > 0
      ? `# default = ${JSON.stringify([defaultGroup])}`
      : '# default = ["dev"]',
    `fail_fast = ${failFast ? "true" : "false"}`,
    `strict_ports = ${strictPorts ? "true" : "false"}`,
    "auto_env = true",
    "shutdown_timeout = 5.0",
    "",
    "[groups]",
    groupLine,
    "",
    "# Example custom task with HTTP readiness detection:",
    "# [tasks.api]",
    '# command = "uvicorn app.main:app --reload --port 8000"',
    '# cwd = "backend"',
    "# port = 8000",
    '# ready_url = "http://127.0.0.1:8000/health"',
    "",
  ];
  writeFileSync(targetPath, lines.join("\n"), "utf8");
  out.write(style(`Created ${targetPath}\n`, GREEN, { enabled: useColor }));
  return 0;
}

function printValidation(root, scanWorkspaces, out, useColor, asJson) {
  const report = validateWorkspace(root, scanWorkspaces);
  if (asJson) {
    out.write(`${JSON.stringify(report.toDict(false), null, 2)}\n`);
    return report.valid ? 0 : 1;
  }

  for (const warn of report.warnings) {
    out.write(style(`warning: ${warn}\n`, YELLOW, { enabled: useColor }));
  }
  for (const err of report.errors) {
    out.write(style(`error: ${err}\n`, RED, { enabled: useColor }));
  }

  if (report.valid) {
    out.write(
      style(
        `Valid configuration (${report.taskCount} task(s), ${report.groupCount} group(s)).\n`,
        GREEN,
        { enabled: useColor }
      )
    );
    return 0;
  }
  return 1;
}

async function printDoctor(root, scanWorkspaces, online, out, useColor, asJson) {
  const report = await runDoctor(root, { online, scanWorkspaces });
  if (asJson) {
    out.write(`${JSON.stringify(report, null, 2)}\n`);
    return report.validation.valid ? 0 : 1;
  }

  out.write(style("devup doctor\n", BOLD, CYAN, { enabled: useColor }));
  out.write(`  devup version : ${report.devup_version}\n`);
  out.write(`  node          : ${report.node_version} (${report.os})\n`);
  out.write(`  project root  : ${report.cwd}\n`);
  out.write(`  devup.toml    : ${report.devup_toml_present ? "present" : "absent"}\n`);
  out.write(`  .env          : ${report.dotenv_present ? "present" : "absent"}\n`);
  out.write(`  sources       : ${report.sources.join(", ") || "none"}\n`);

  const available = Object.entries(report.runners)
    .filter(([, info]) => info.available)
    .map(([name]) => name);
  const missing = Object.entries(report.runners)
    .filter(([, info]) => !info.available)
    .map(([name]) => name);

  out.write(`  runners found : ${available.join(", ") || "none"}\n`);
  out.write(style(`  not in PATH   : ${missing.join(", ") || "none"}\n`, DIM, { enabled: useColor }));

  const val = report.validation;
  if (val.errors.length > 0) {
    for (const err of val.errors) {
      out.write(style(`  error         : ${err}\n`, RED, { enabled: useColor }));
    }
  } else if (val.warnings.length > 0) {
    for (const warn of val.warnings) {
      out.write(style(`  warning       : ${warn}\n`, YELLOW, { enabled: useColor }));
    }
  } else {
    out.write(style("  validation    : OK\n", GREEN, { enabled: useColor }));
  }

  const upd = report.update_check;
  if (upd && typeof upd === "object") {
    if (upd.update_available) {
      out.write(
        style(
          `  update        : @devup-cli/devup ${upd.latest} available (current ${upd.current})\n`,
          YELLOW,
          { enabled: useColor }
        )
      );
    } else {
      out.write(style(`  update        : up to date (${upd.current})\n`, GREEN, { enabled: useColor }));
    }
  }

  return val.valid ? 0 : 1;
}

async function maybeNotifyUpdate(args, out, useColor, command) {
  if (args.noUpdateCheck || args.json || command === "completion") return;
  const optInEnv = String(process.env.DEVUP_CHECK_UPDATES || "").toLowerCase();
  const envOptIn = optInEnv === "1" || optInEnv === "true" || optInEnv === "yes";
  if (!args.checkUpdates && !envOptIn) return;

  const status = await checkForUpdate(VERSION, { force: Boolean(args.checkUpdates) });
  if (status && status.update_available) {
    out.write(
      style(
        `Note: @devup-cli/devup ${status.latest} is available (you have ${status.current}). ` +
          "Upgrade with `npm install -g @devup-cli/devup@latest`.\n",
        DIM,
        { enabled: useColor }
      )
    );
  }
}

function exitCodeFromResults(results) {
  const failed = results.filter((r) => r.status === "failed" || r.status === "error");
  if (failed.length > 0) {
    const nonZero = failed.find((r) => r.exitCode !== 0);
    return nonZero ? nonZero.exitCode : 1;
  }
  if (results.some((r) => r.status === "stopped")) {
    return 130;
  }
  return 0;
}

export async function main(argv = process.argv.slice(2), io = {}) {
  const out = io.stdout || process.stdout;
  const errOut = io.stderr || process.stderr;
  const inStream = io.stdin || process.stdin;

  let args;
  try {
    args = parseCliArgs(argv);
  } catch (err) {
    errOut.write(`error: ${err.message}\n`);
    return 2;
  }

  if (args.help) {
    out.write(helpText());
    return 0;
  }
  if (args.version) {
    out.write(`devup ${VERSION}\n`);
    return 0;
  }

  const colorPref = resolveColorFlag(args);
  const useColor = supportsColor(out, colorPref);
  const root = args.cwd;

  if (!pathExists(root)) {
    out.write(style(`Error: directory does not exist: ${root}\n`, RED, { enabled: useColor }));
    return 2;
  }

  const config = discoverWorkspace(root, !args.noWorkspaces);
  const positionals = [...args.targets];
  let command = positionals.length > 0 && SUBCOMMANDS.has(positionals[0]) ? positionals[0] : null;
  let rest = command ? positionals.slice(1) : positionals;

  if (command === "completion") {
    if (rest.length !== 1) {
      out.write(
        style(`Usage: devup completion <${SUPPORTED_SHELLS.join("|")}>\n`, RED, {
          enabled: useColor,
        })
      );
      return 2;
    }
    try {
      out.write(generateCompletion(rest[0]));
      return 0;
    } catch (err) {
      out.write(style(`Error: ${err.message}\n`, RED, { enabled: useColor }));
      return 2;
    }
  }

  if (command === "validate") {
    return printValidation(root, !args.noWorkspaces, out, useColor, args.json);
  }

  if (command === "doctor") {
    return printDoctor(
      root,
      !args.noWorkspaces,
      Boolean(args.online || args.checkUpdates),
      out,
      useColor,
      args.json
    );
  }

  await maybeNotifyUpdate(args, out, useColor, command);

  if (command === "list" || command === "ls") {
    return printList(config, out, useColor, args.json);
  }

  if (command === "init") {
    return initDevupToml(config, args.force, out, useColor, {
      template: args.template,
      interactive: args.interactive,
      yes: args.yes,
      stdin: inStream,
    });
  }

  if (command === "pick") {
    let picked;
    try {
      picked = await interactivePick(config, out, useColor, inStream);
    } catch (err) {
      out.write(style(`Error: ${err.message}\n`, RED, { enabled: useColor }));
      return 2;
    }
    if (!picked || picked.length === 0) return 0;
    rest = picked;
    command = "run";
  }

  if (command === null && rest.length === 0) {
    if (config.defaultTargets.length > 0) {
      rest = [...config.defaultTargets];
    } else if (
      inStream &&
      inStream.isTTY &&
      (Object.keys(config.tasks).length > 0 || Object.keys(config.groups).length > 0)
    ) {
      try {
        rest = await interactivePick(config, out, useColor, inStream);
      } catch (err) {
        out.write(style(`Error: ${err.message}\n`, RED, { enabled: useColor }));
        return 2;
      }
      if (!rest || rest.length === 0) return 0;
    } else {
      return printList(config, out, useColor, args.json);
    }
  }

  if (command === "run" && rest.length === 0) {
    out.write(
      style("Error: `devup run` requires at least one task or group name.\n", RED, {
        enabled: useColor,
      })
    );
    return 2;
  }

  let resolved;
  try {
    resolved = resolveTargets(config, rest);
  } catch (err) {
    out.write(style(`Error: ${err.message}\n`, RED, { enabled: useColor }));
    return 2;
  }

  const runOpts = {
    failFast: args.keepGoing ? false : null,
    timestamps: args.timestamps,
    prefix: !args.noPrefix,
    color: colorPref,
    dryRun: args.dryRun,
    dotenv: !args.noDotenv,
    extraEnvFiles: [...args.envFile],
    portCheck: !args.noPortCheck,
    strictPorts: args.strictPorts ? true : null,
    watch: args.watch,
  };

  let results;
  try {
    results = await runTasks(config, resolved, runOpts, out);
  } catch (err) {
    out.write(style(`Error: ${err.message}\n`, RED, { enabled: useColor }));
    return 2;
  }

  return exitCodeFromResults(results);
}
