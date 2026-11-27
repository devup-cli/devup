import { readFileSync, realpathSync } from "node:fs";
import { basename, join, relative, resolve } from "node:path";

import { expandGlobDirs, isFile, which } from "./fs_utils.mjs";
import { createTask, createWorkspaceConfig } from "./models.mjs";
import { normalizeTcpTarget, validateReadyUrl } from "./readiness.mjs";
import { parseToml } from "./toml.mjs";
import { extractPomMetadata } from "./xml.mjs";

const COMPOSE_FILES = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"];
const JUSTFILES = ["justfile", "Justfile", ".justfile"];
const MAKEFILES = ["GNUmakefile", "Makefile", "makefile"];
const GRADLE_FILES = ["build.gradle.kts", "build.gradle"];
const GRADLE_SETTINGS_FILES = ["settings.gradle.kts", "settings.gradle"];

const MAKE_TARGET_RE = /^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)(?:\s*[^#\n]*)(?:#+\s*(.*))?$/;
const JUST_RECIPE_RE = /^@?([A-Za-z][A-Za-z0-9_-]*)(?:\s+[^:=\n]+?)?\s*:\s*(?:#\s*(.*))?$/;
const GRADLE_REGISTER_RE = /tasks\.register(?:<[^>]+>)?\(\s*["']([A-Za-z0-9_-]+)["']/g;
const GRADLE_KTS_DELEGATE_RE = /val\s+([A-Za-z][A-Za-z0-9_-]*)\s+by\s+tasks\.(?:registering|creating)/g;
const GRADLE_GROOVY_TASK_RE = /^\s*task\s+([A-Za-z][A-Za-z0-9_-]*)\s*(?:\(|{|$)/;
const GRADLE_INCLUDE_RE = /include\s*\(?\s*([^\n)]+)/g;

function safeRealpath(p) {
  try {
    return realpathSync(p);
  } catch {
    return resolve(p);
  }
}

function registerTask(tasks, task, qualifier, allowOverride = false) {
  if (allowOverride || !(task.name in tasks)) {
    tasks[task.name] = task;
    return;
  }
  const qualifiedName = `${qualifier}:${task.name}`;
  if (!(qualifiedName in tasks)) {
    task.name = qualifiedName;
    tasks[qualifiedName] = task;
  }
}

function detectNodeRunner(directory, root) {
  const searchDirs = directory === root ? [directory] : [directory, root];
  for (const base of searchDirs) {
    if (isFile(join(base, "pnpm-lock.yaml"))) return "pnpm";
    if (isFile(join(base, "yarn.lock"))) return "yarn";
    if (isFile(join(base, "bun.lockb")) || isFile(join(base, "bun.lock"))) return "bun";
    if (isFile(join(base, "package-lock.json"))) return "npm";
  }
  return "npm";
}

function discoverPackageJson(directory, root, prefix = "") {
  const pkgPath = join(directory, "package.json");
  if (!isFile(pkgPath)) {
    return { tasks: [], workspaces: [], warnings: [] };
  }

  let data;
  try {
    data = JSON.parse(readFileSync(pkgPath, "utf8"));
  } catch (err) {
    return { tasks: [], workspaces: [], warnings: [`Failed to parse ${pkgPath}: ${err.message}`] };
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return { tasks: [], workspaces: [], warnings: [] };
  }

  const tasks = [];
  const runner = detectNodeRunner(directory, root);
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;

  if (data.scripts && typeof data.scripts === "object" && !Array.isArray(data.scripts)) {
    for (const [name, cmd] of Object.entries(data.scripts)) {
      if (typeof name !== "string" || typeof cmd !== "string") continue;
      if (name.startsWith("//") || !cmd.trim()) continue;
      const runCmd = runner === "yarn" ? `${runner} ${name}` : `${runner} run ${name}`;
      tasks.push(
        createTask({
          name: `${prefix}${name}`,
          command: runCmd,
          source: "package.json",
          runner,
          description: cmd.trim(),
          cwd: directory,
          workspace: workspaceName,
        })
      );
    }
  }

  let workspaceGlobs = [];
  if (Array.isArray(data.workspaces)) {
    workspaceGlobs = data.workspaces.filter((w) => typeof w === "string");
  } else if (data.workspaces && typeof data.workspaces === "object" && Array.isArray(data.workspaces.packages)) {
    workspaceGlobs = data.workspaces.packages.filter((w) => typeof w === "string");
  }

  return { tasks, workspaces: workspaceGlobs, warnings: [] };
}

function formatPoeOrPdmDesc(entry) {
  if (typeof entry === "string") return entry.trim();
  if (entry && typeof entry === "object" && !Array.isArray(entry)) {
    if (typeof entry.help === "string" && entry.help.trim()) {
      return entry.help.trim();
    }
    for (const key of ["cmd", "shell", "script", "composite"]) {
      const val = entry[key];
      if (typeof val === "string" && val.trim()) return val.trim();
      if (Array.isArray(val)) return val.map(String).join(" && ");
    }
  }
  return "";
}

function discoverPyproject(directory, prefix = "") {
  const pyprojectPath = join(directory, "pyproject.toml");
  if (!isFile(pyprojectPath)) {
    return { tasks: [], warnings: [] };
  }

  let data;
  try {
    data = parseToml(readFileSync(pyprojectPath, "utf8"));
  } catch (err) {
    return { tasks: [], warnings: [`Failed to parse ${pyprojectPath}: ${err.message}`] };
  }

  const tasks = [];
  const seen = new Set();
  const tool = data.tool && typeof data.tool === "object" ? data.tool : {};
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;

  function addTask(name, command, runner, description) {
    const fullName = `${prefix}${name}`;
    if (seen.has(fullName)) return;
    seen.add(fullName);
    tasks.push(
      createTask({
        name: fullName,
        command,
        source: "pyproject.toml",
        runner,
        description,
        cwd: directory,
        workspace: workspaceName,
      })
    );
  }

  const poeTasks = tool.poe && typeof tool.poe === "object" ? tool.poe.tasks : null;
  if (poeTasks && typeof poeTasks === "object") {
    for (const [name, entry] of Object.entries(poeTasks)) {
      if (name.startsWith("_")) continue;
      addTask(name, `poe ${name}`, "poe", formatPoeOrPdmDesc(entry));
    }
  }

  const pdmScripts = tool.pdm && typeof tool.pdm === "object" ? tool.pdm.scripts : null;
  if (pdmScripts && typeof pdmScripts === "object") {
    for (const [name, entry] of Object.entries(pdmScripts)) {
      if (name.startsWith("_") || name === "_") continue;
      addTask(name, `pdm run ${name}`, "pdm", formatPoeOrPdmDesc(entry));
    }
  }

  const hatchEnvs = tool.hatch && typeof tool.hatch === "object" ? tool.hatch.envs : null;
  if (hatchEnvs && typeof hatchEnvs === "object") {
    for (const [envName, envCfg] of Object.entries(hatchEnvs)) {
      if (!envCfg || typeof envCfg !== "object") continue;
      const scripts = envCfg.scripts;
      if (!scripts || typeof scripts !== "object") continue;
      for (const [name, entry] of Object.entries(scripts)) {
        const targetName = envName === "default" ? name : `${envName}:${name}`;
        const hatchTarget = envName === "default" ? name : `${envName}:${name}`;
        const desc =
          typeof entry === "string"
            ? entry
            : Array.isArray(entry)
              ? entry.map(String).join(" | ")
              : "";
        addTask(targetName, `hatch run ${hatchTarget}`, "hatch", desc);
      }
    }
  }

  const poetryScripts = tool.poetry && typeof tool.poetry === "object" ? tool.poetry.scripts : null;
  if (poetryScripts && typeof poetryScripts === "object") {
    for (const [name, entry] of Object.entries(poetryScripts)) {
      const desc = typeof entry === "string" ? entry : String(entry);
      addTask(name, `poetry run ${name}`, "poetry", desc);
    }
  }

  const projectScripts = data.project && typeof data.project === "object" ? data.project.scripts : null;
  if (projectScripts && typeof projectScripts === "object") {
    for (const [name, entry] of Object.entries(projectScripts)) {
      if (typeof entry === "string") {
        addTask(name, name, "python", entry);
      }
    }
  }

  return { tasks, warnings: [] };
}

function discoverMakefile(directory, prefix = "") {
  const makefileName = MAKEFILES.find((n) => isFile(join(directory, n)));
  if (!makefileName) return { tasks: [], warnings: [] };
  const makefilePath = join(directory, makefileName);

  let lines;
  try {
    lines = readFileSync(makefilePath, "utf8").split(/\r?\n/);
  } catch (err) {
    return { tasks: [], warnings: [`Failed to read ${makefilePath}: ${err.message}`] };
  }

  const tasks = [];
  const seen = new Set();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;
  let prevComment = "";

  for (const line of lines) {
    const stripped = line.trim();
    if (!stripped) {
      prevComment = "";
      continue;
    }
    if (stripped.startsWith("#")) {
      prevComment = stripped.replace(/^#+/, "").trim();
      continue;
    }
    if (line.startsWith("\t") || line.startsWith(" ")) {
      prevComment = "";
      continue;
    }
    const match = MAKE_TARGET_RE.exec(line);
    if (match) {
      const target = match[1];
      const inlineDoc = (match[2] || "").trim();
      if (!target.startsWith(".") && !seen.has(target)) {
        seen.add(target);
        tasks.push(
          createTask({
            name: `${prefix}${target}`,
            command: `make ${target}`,
            source: makefileName,
            runner: "make",
            description: inlineDoc || prevComment,
            cwd: directory,
            workspace: workspaceName,
          })
        );
      }
    }
    prevComment = "";
  }

  return { tasks, warnings: [] };
}

function discoverJustfile(directory, prefix = "") {
  const justName = JUSTFILES.find((n) => isFile(join(directory, n)));
  if (!justName) return { tasks: [], warnings: [] };
  const justPath = join(directory, justName);

  let lines;
  try {
    lines = readFileSync(justPath, "utf8").split(/\r?\n/);
  } catch (err) {
    return { tasks: [], warnings: [`Failed to read ${justPath}: ${err.message}`] };
  }

  const tasks = [];
  const seen = new Set();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;
  let prevComment = "";

  for (const line of lines) {
    const stripped = line.trim();
    if (!stripped) {
      prevComment = "";
      continue;
    }
    if (stripped.startsWith("#")) {
      prevComment = stripped.replace(/^#+/, "").trim();
      continue;
    }
    if (line.startsWith("\t") || line.startsWith(" ")) {
      prevComment = "";
      continue;
    }
    if (/^(set |alias |export |import |mod )/.test(stripped)) {
      prevComment = "";
      continue;
    }
    const match = JUST_RECIPE_RE.exec(line);
    if (match) {
      const recipe = match[1];
      const inlineDoc = (match[2] || "").trim();
      if (!recipe.startsWith("_") && !seen.has(recipe)) {
        seen.add(recipe);
        tasks.push(
          createTask({
            name: `${prefix}${recipe}`,
            command: `just ${recipe}`,
            source: justName,
            runner: "just",
            description: inlineDoc || prevComment,
            cwd: directory,
            workspace: workspaceName,
          })
        );
      }
    }
    prevComment = "";
  }

  return { tasks, warnings: [] };
}

function parseComposeServices(text) {
  const services = [];
  let inServices = false;
  let serviceIndent = null;

  for (const rawLine of text.split(/\r?\n/)) {
    const trimStart = rawLine.trimStart();
    if (!trimStart || trimStart.startsWith("#")) continue;

    const indent = rawLine.length - trimStart.length;
    const stripped = rawLine.trim();

    if (indent === 0) {
      if (stripped.startsWith("services:")) {
        inServices = true;
        serviceIndent = null;
      } else {
        inServices = false;
      }
      continue;
    }

    if (!inServices) continue;
    if (serviceIndent === null) serviceIndent = indent;

    if (indent === serviceIndent && stripped.includes(":") && !stripped.startsWith("-")) {
      const key = stripped.split(":", 1)[0].trim().replace(/^["']|["']$/g, "");
      if (key) services.push(key);
    }
  }

  return services;
}

function detectComposeBinary() {
  if (which("docker")) return "docker compose";
  if (which("docker-compose")) return "docker-compose";
  return "docker compose";
}

function discoverCompose(directory, prefix = "") {
  const composeName = COMPOSE_FILES.find((n) => isFile(join(directory, n)));
  if (!composeName) return { tasks: [], warnings: [] };
  const composePath = join(directory, composeName);

  let content;
  try {
    content = readFileSync(composePath, "utf8");
  } catch (err) {
    return { tasks: [], warnings: [`Failed to read ${composePath}: ${err.message}`] };
  }

  const services = parseComposeServices(content);
  if (services.length === 0) return { tasks: [], warnings: [] };

  const composeCmd = detectComposeBinary();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;
  const tasks = [
    createTask({
      name: `${prefix}compose:up`,
      command: `${composeCmd} up`,
      source: composeName,
      runner: "docker-compose",
      description: `Start all services (${services.join(", ")})`,
      cwd: directory,
      workspace: workspaceName,
    }),
    createTask({
      name: `${prefix}compose:down`,
      command: `${composeCmd} down`,
      source: composeName,
      runner: "docker-compose",
      description: "Stop and remove compose containers",
      cwd: directory,
      workspace: workspaceName,
    }),
  ];

  for (const svc of services) {
    tasks.push(
      createTask({
        name: `${prefix}compose:${svc}`,
        command: `${composeCmd} up ${svc}`,
        source: composeName,
        runner: "docker-compose",
        description: `Start Docker Compose service '${svc}'`,
        cwd: directory,
        workspace: workspaceName,
      })
    );
  }

  return { tasks, warnings: [] };
}

function formatWrapperCmd(wrapperPath, directory) {
  let rel = relative(directory, wrapperPath);
  if (!rel) rel = basename(wrapperPath);
  if (process.platform === "win32") {
    return rel.includes(" ") ? `"${rel}"` : rel;
  }
  if (!rel.startsWith(".") && !rel.startsWith("/")) {
    rel = `./${rel}`;
  }
  return rel.includes(" ") ? `"${rel}"` : rel;
}

function detectMavenRunner(directory, root) {
  const wrapperName = process.platform === "win32" ? "mvnw.cmd" : "mvnw";
  const searchDirs = directory === root ? [directory] : [directory, root];
  for (const base of searchDirs) {
    const candidate = join(base, wrapperName);
    if (isFile(candidate)) return formatWrapperCmd(candidate, directory);
  }
  return "mvn";
}

function discoverMaven(directory, root, prefix = "") {
  const pomPath = join(directory, "pom.xml");
  if (!isFile(pomPath)) return { tasks: [], modules: [], warnings: [] };

  let meta;
  try {
    meta = extractPomMetadata(readFileSync(pomPath, "utf8"));
  } catch (err) {
    return { tasks: [], modules: [], warnings: [`Failed to parse ${pomPath}: ${err.message}`] };
  }

  const mvn = detectMavenRunner(directory, root);
  const tasks = [];
  const seen = new Set();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;

  function addTask(name, goal, description) {
    const fullName = `${prefix}${name}`;
    if (seen.has(fullName)) return;
    seen.add(fullName);
    tasks.push(
      createTask({
        name: fullName,
        command: `${mvn} ${goal}`,
        source: "pom.xml",
        runner: "maven",
        description,
        cwd: directory,
        workspace: workspaceName,
      })
    );
  }

  for (const artifact of meta.artifactIds) {
    if (artifact === "spring-boot-maven-plugin") {
      addTask("dev", "spring-boot:run", "Run Spring Boot application");
    } else if (artifact === "quarkus-maven-plugin") {
      addTask("dev", "quarkus:dev", "Run Quarkus in dev mode");
    } else if (artifact === "exec-maven-plugin") {
      addTask("exec", "exec:java", "Run main class via exec-maven-plugin");
    }
  }

  for (const [goal, desc] of [
    ["compile", "Compile project sources"],
    ["test", "Run Maven tests"],
    ["package", "Package Maven artifact"],
    ["verify", "Run verification checks"],
    ["clean", "Clean Maven target directory"],
  ]) {
    addTask(goal, goal, desc);
  }

  return { tasks, modules: meta.modules, warnings: [] };
}

function detectGradleRunner(directory, root) {
  const wrapperName = process.platform === "win32" ? "gradlew.bat" : "gradlew";
  const searchDirs = directory === root ? [directory] : [directory, root];
  for (const base of searchDirs) {
    const candidate = join(base, wrapperName);
    if (isFile(candidate)) return formatWrapperCmd(candidate, directory);
  }
  return "gradle";
}

function discoverGradleSubprojects(directory) {
  const settingsName = GRADLE_SETTINGS_FILES.find((n) => isFile(join(directory, n)));
  if (!settingsName) return [];

  let text;
  try {
    text = readFileSync(join(directory, settingsName), "utf8");
  } catch {
    return [];
  }

  const subprojects = [];
  const seen = new Set();
  GRADLE_INCLUDE_RE.lastIndex = 0;
  let match;
  while ((match = GRADLE_INCLUDE_RE.exec(text)) !== null) {
    const blob = match[1];
    const quoteRe = /["']([^"']+)["']/g;
    let qm;
    while ((qm = quoteRe.exec(blob)) !== null) {
      const rel = qm[1].replace(/^:+/, "").replaceAll(":", "/").trim();
      if (rel && !seen.has(rel)) {
        seen.add(rel);
        subprojects.push(rel);
      }
    }
  }
  return subprojects;
}

function discoverGradle(directory, root, prefix = "") {
  const gradleName = GRADLE_FILES.find((n) => isFile(join(directory, n)));
  const subprojects = discoverGradleSubprojects(directory);
  if (!gradleName && subprojects.length === 0) {
    return { tasks: [], subprojects: [], warnings: [] };
  }

  const gradle = detectGradleRunner(directory, root);
  const sourceName = gradleName || "settings.gradle";
  const tasks = [];
  const seen = new Set();
  const workspaceName = prefix ? prefix.replace(/:+$/, "") : null;

  function addTask(name, gradleTask, description) {
    const fullName = `${prefix}${name}`;
    if (seen.has(fullName)) return;
    seen.add(fullName);
    tasks.push(
      createTask({
        name: fullName,
        command: `${gradle} ${gradleTask}`,
        source: sourceName,
        runner: "gradle",
        description,
        cwd: directory,
        workspace: workspaceName,
      })
    );
  }

  if (gradleName) {
    const gradlePath = join(directory, gradleName);
    let content;
    try {
      content = readFileSync(gradlePath, "utf8");
    } catch (err) {
      return { tasks: [], subprojects, warnings: [`Failed to read ${gradlePath}: ${err.message}`] };
    }

    if (content.includes("org.springframework.boot")) {
      addTask("bootRun", "bootRun", "Run Spring Boot application");
    }
    if (/\bapplication\b|\bmainClass\b/.test(content)) {
      addTask("run", "run", "Run Gradle application");
    }

    for (const regex of [GRADLE_REGISTER_RE, GRADLE_KTS_DELEGATE_RE]) {
      regex.lastIndex = 0;
      let m;
      while ((m = regex.exec(content)) !== null) {
        addTask(m[1], m[1], `Custom Gradle task '${m[1]}'`);
      }
    }
    for (const line of content.split(/\r?\n/)) {
      const m = GRADLE_GROOVY_TASK_RE.exec(line);
      if (m) {
        addTask(m[1], m[1], `Custom Gradle task '${m[1]}'`);
      }
    }
  }

  for (const [tname, desc] of [
    ["build", "Assemble and test Gradle project"],
    ["test", "Run Gradle tests"],
    ["check", "Run Gradle verification checks"],
    ["clean", "Clean Gradle build directory"],
  ]) {
    addTask(tname, tname, desc);
  }

  return { tasks, subprojects, warnings: [] };
}

function resolveWorkspaceDirs(root, patterns) {
  const dirs = [];
  const seen = new Set();
  const resolvedRootReal = safeRealpath(root);

  for (const pattern of patterns) {
    const candidates = expandGlobDirs(root, pattern);
    for (const cand of candidates) {
      const realCand = safeRealpath(cand);
      if (realCand !== resolvedRootReal && !seen.has(realCand)) {
        seen.add(realCand);
        dirs.push(cand);
      }
    }
  }
  return dirs;
}

function discoverDirectory(directory, root, tasks, warnings, prefix = "") {
  const discoveredWorkspaces = [];

  const pkg = discoverPackageJson(directory, root, prefix);
  warnings.push(...pkg.warnings);
  discoveredWorkspaces.push(...pkg.workspaces);
  for (const task of pkg.tasks) registerTask(tasks, task, `${prefix}node`);

  const py = discoverPyproject(directory, prefix);
  warnings.push(...py.warnings);
  for (const task of py.tasks) registerTask(tasks, task, `${prefix}py`);

  const mvn = discoverMaven(directory, root, prefix);
  warnings.push(...mvn.warnings);
  discoveredWorkspaces.push(...mvn.modules);
  for (const task of mvn.tasks) registerTask(tasks, task, `${prefix}mvn`);

  const grad = discoverGradle(directory, root, prefix);
  warnings.push(...grad.warnings);
  discoveredWorkspaces.push(...grad.subprojects);
  for (const task of grad.tasks) registerTask(tasks, task, `${prefix}gradle`);

  const jst = discoverJustfile(directory, prefix);
  warnings.push(...jst.warnings);
  for (const task of jst.tasks) registerTask(tasks, task, `${prefix}just`);

  const mk = discoverMakefile(directory, prefix);
  warnings.push(...mk.warnings);
  for (const task of mk.tasks) registerTask(tasks, task, `${prefix}make`);

  const cmp = discoverCompose(directory, prefix);
  warnings.push(...cmp.warnings);
  for (const task of cmp.tasks) registerTask(tasks, task, `${prefix}compose`);

  return discoveredWorkspaces;
}

function loadDevupToml(root, config) {
  const tomlPath = join(root, "devup.toml");
  if (!isFile(tomlPath)) return [];

  let data;
  try {
    data = parseToml(readFileSync(tomlPath, "utf8"));
  } catch (err) {
    config.warnings.push(`Failed to parse ${tomlPath}: ${err.message}`);
    return [];
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) return [];

  if (typeof data.fail_fast === "boolean") config.failFast = data.fail_fast;
  if (typeof data.auto_env === "boolean") config.autoEnv = data.auto_env;
  if (typeof data.strict_ports === "boolean") config.strictPorts = data.strict_ports;
  if (typeof data.shutdown_timeout === "number" && Number.isFinite(data.shutdown_timeout)) {
    config.shutdownTimeout = Math.max(0.5, data.shutdown_timeout);
  }

  if (typeof data.env_file === "string") {
    config.envFiles = [data.env_file];
  } else if (Array.isArray(data.env_file)) {
    config.envFiles = data.env_file.filter((f) => typeof f === "string");
  }

  if (Array.isArray(data.default)) {
    config.defaultTargets = data.default.filter((d) => typeof d === "string");
  } else if (typeof data.default === "string") {
    config.defaultTargets = [data.default];
  }

  if (data.env && typeof data.env === "object" && !Array.isArray(data.env)) {
    config.env = Object.fromEntries(Object.entries(data.env).map(([k, v]) => [String(k), String(v)]));
  }

  if (data.groups && typeof data.groups === "object" && !Array.isArray(data.groups)) {
    for (const [groupName, members] of Object.entries(data.groups)) {
      if (Array.isArray(members)) {
        config.groups[groupName] = members.filter((m) => typeof m === "string");
      }
    }
  }

  if (data.tasks && typeof data.tasks === "object" && !Array.isArray(data.tasks)) {
    for (const [taskName, entry] of Object.entries(data.tasks)) {
      if (typeof entry === "string") {
        registerTask(
          config.tasks,
          createTask({
            name: taskName,
            command: entry,
            source: "devup.toml",
            runner: "devup",
            cwd: root,
          }),
          "devup",
          true
        );
      } else if (entry && typeof entry === "object" && !Array.isArray(entry) && typeof entry.command === "string") {
        const taskCwd = typeof entry.cwd === "string" ? resolve(root, entry.cwd) : root;
        const taskEnv =
          entry.env && typeof entry.env === "object" && !Array.isArray(entry.env)
            ? Object.fromEntries(Object.entries(entry.env).map(([k, v]) => [String(k), String(v)]))
            : {};
        const deps = Array.isArray(entry.depends_on)
          ? entry.depends_on.filter((d) => typeof d === "string")
          : [];
        const readyPattern = typeof entry.ready_pattern === "string" ? entry.ready_pattern : null;

        let readyUrl = null;
        if (typeof entry.ready_url === "string") {
          try {
            readyUrl = validateReadyUrl(entry.ready_url);
          } catch (err) {
            config.warnings.push(`Task '${taskName}': ${err.message}`);
          }
        }

        let readyTcp = null;
        const rawTcp = "ready_tcp" in entry ? entry.ready_tcp : entry.ready_port;
        if (typeof rawTcp === "string" || typeof rawTcp === "number") {
          try {
            readyTcp = normalizeTcpTarget(rawTcp);
          } catch (err) {
            config.warnings.push(`Task '${taskName}': ${err.message}`);
          }
        }

        const readyTimeout =
          typeof entry.ready_timeout === "number" && Number.isFinite(entry.ready_timeout)
            ? Math.max(0.5, entry.ready_timeout)
            : 30.0;

        const description = typeof entry.description === "string" ? entry.description : "";

        let taskEnvFiles = [];
        if (typeof entry.env_file === "string") {
          taskEnvFiles = [entry.env_file];
        } else if (Array.isArray(entry.env_file)) {
          taskEnvFiles = entry.env_file.filter((f) => typeof f === "string");
        }

        let taskPorts = [];
        if (typeof entry.port === "number" && Number.isInteger(entry.port)) {
          taskPorts = [entry.port];
        } else if (Array.isArray(entry.ports)) {
          taskPorts = entry.ports.filter((p) => typeof p === "number" && Number.isInteger(p));
        }

        let watchEnabled = false;
        let watchPatterns = [];
        if (typeof entry.watch === "boolean") {
          watchEnabled = entry.watch;
        } else if (typeof entry.watch === "string") {
          watchEnabled = true;
          watchPatterns = [entry.watch];
        } else if (Array.isArray(entry.watch)) {
          watchPatterns = entry.watch.filter((w) => typeof w === "string");
          watchEnabled = watchPatterns.length > 0;
        }

        registerTask(
          config.tasks,
          createTask({
            name: taskName,
            command: entry.command,
            source: "devup.toml",
            runner: "devup",
            description,
            cwd: taskCwd,
            env: taskEnv,
            dependsOn: deps,
            readyPattern,
            readyUrl,
            readyTcp,
            readyTimeout,
            envFiles: taskEnvFiles,
            ports: taskPorts,
            watch: watchPatterns,
            watchEnabled,
          }),
          "devup",
          true
        );
      }
    }
  }

  if (Array.isArray(data.workspaces)) {
    return data.workspaces.filter((w) => typeof w === "string");
  }
  return [];
}

export function discoverWorkspace(root, scanWorkspaces = true) {
  const resolvedRoot = resolve(root);
  const config = createWorkspaceConfig(resolvedRoot);

  const pkgWorkspaces = discoverDirectory(resolvedRoot, resolvedRoot, config.tasks, config.warnings);
  const tomlWorkspaces = loadDevupToml(resolvedRoot, config);

  if (scanWorkspaces) {
    const workspaceGlobs = tomlWorkspaces.length > 0 ? tomlWorkspaces : pkgWorkspaces;
    for (const wsDir of resolveWorkspaceDirs(resolvedRoot, workspaceGlobs)) {
      const wsName = basename(wsDir);
      discoverDirectory(wsDir, resolvedRoot, config.tasks, config.warnings, `${wsName}:`);
    }
    if (tomlWorkspaces.length > 0 || pkgWorkspaces.length > 0) {
      loadDevupToml(resolvedRoot, config);
    }
  }

  return config;
}

function similarityRatio(a, b) {
  if (a === b) return 1;
  if (!a || !b) return 0;
  const la = a.length;
  const lb = b.length;
  const dp = Array.from({ length: la + 1 }, () => new Array(lb + 1).fill(0));
  for (let i = 1; i <= la; i += 1) {
    for (let j = 1; j <= lb; j += 1) {
      if (a[i - 1] === b[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }
  const lcs = dp[la][lb];
  return (2 * lcs) / (la + lb);
}

function getCloseMatches(word, possibilities, n = 3, cutoff = 0.45) {
  const scored = [];
  for (const candidate of possibilities) {
    const score = similarityRatio(word, candidate);
    if (score >= cutoff) {
      scored.push({ candidate, score });
    }
  }
  scored.sort((x, y) => y.score - x.score);
  return scored.slice(0, n).map((s) => s.candidate);
}

export function resolveTargets(config, targets) {
  const expanded = [];
  const seen = new Set();

  function expandOne(name, stack) {
    if (stack.has(name)) {
      throw new Error(`Circular group reference detected: ${name}`);
    }
    if (Object.hasOwn(config.groups, name)) {
      stack.add(name);
      for (const member of config.groups[name]) {
        expandOne(member, stack);
      }
      stack.delete(name);
      return;
    }
    if (!Object.hasOwn(config.tasks, name)) {
      const allNames = [...Object.keys(config.tasks), ...Object.keys(config.groups)].sort();
      const suggestions = getCloseMatches(name, allNames, 3, 0.45);
      const hint = suggestions.length > 0 ? ` Did you mean: ${suggestions.join(", ")}?` : "";
      const available = allNames.length > 0 ? allNames.join(", ") : "none";
      const err = new Error(
        `Unknown task or group '${name}'.${hint} Available: ${available}. Run \`devup list\` to inspect discovered targets.`
      );
      err.code = "ERR_UNKNOWN_TARGET";
      throw err;
    }
    if (!seen.has(name)) {
      seen.add(name);
      expanded.push(name);
    }
  }

  for (const target of targets) {
    expandOne(target, new Set());
  }

  return expanded;
}
